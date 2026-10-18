"""Conexión del Drive del cliente en el primer uso de las facturas: token de un solo uso, aviso firmado del conector, subcarpetas y raíz guardada."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import config
import mobile_facturas
import mobile_routes
from services import drive_cliente, facturas, puente_plataforma


class Doc:
    def __init__(self, a, k): self.a, self.k = a, k
    def get(self):
        d = self.a.get(self.k)
        return type("S", (), {"exists": d is not None, "to_dict": lambda s: dict(d) if d else {}})()
    def set(self, datos): self.a[self.k] = dict(datos)


class Col:
    def __init__(self, a): self.a = a
    def document(self, k): return Doc(self.a, k)


class FakeDB:
    def __init__(self): self.colecciones = {}
    def collection(self, n): return Col(self.colecciones.setdefault(n, {}))


CARPETA = "1AbCdEfGhIjKlMnOpQrStUvWx"


class Conexion(unittest.TestCase):
    PIN, PIN_ADMIN = "1111", "9999"

    def setUp(self):
        self.db = FakeDB()
        self.llamadas = []
        for objeto, nombre, valor in (
                (config, "db", self.db), (config, "PUENTE_SECRETO", "secreto-puente"),
                (mobile_facturas, "CONECTOR_URL", "https://script.google.com/macros/s/ABC/exec"),
                (mobile_routes, "MOBILE_APP_PIN", self.PIN), (mobile_routes, "MOBILE_APP_PIN_ADMIN", self.PIN_ADMIN),
                (puente_plataforma, "configurado", lambda: True),
                (puente_plataforma, "ping", self._ping), (puente_plataforma, "estructura", self._estructura),
                (drive_cliente.time, "sleep", lambda s: None),
                (facturas.CLIENTES["alc-homes"], "__getitem__", None)):
            if nombre == "__getitem__":
                continue
            p = mock.patch.object(objeto, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(facturas.CLIENTES["alc-homes"], {"drive_raiz": ""})
        p.start()
        self.addCleanup(p.stop)
        drive_cliente.olvidar_cache()
        self.addCleanup(drive_cliente.olvidar_cache)
        self.ping_falla = 0
        app = Flask(__name__)
        app.register_blueprint(mobile_facturas.facturas_bp)
        self.cliente = app.test_client()

    def _ping(self, raiz_id):
        self.llamadas.append(("ping", raiz_id))
        if self.ping_falla > 0:
            self.ping_falla -= 1
            raise puente_plataforma.PuenteError("sin acceso todavía")
        return "HostalFácil-Gestión"

    def _estructura(self, raiz_id, rutas):
        self.llamadas.append(("estructura", raiz_id, rutas))
        return len(rutas)

    def _conectar(self, pin=None):
        return self.cliente.post("/mobile/facturas/conectar", json={}, headers={"x-app-pin": pin} if pin else {})

    def _token(self):
        url = self._conectar(self.PIN_ADMIN).get_json()["url"]
        return url.split("t=")[1]

    def _avisar(self, **extra):
        cuerpo = {"token": self._token(), "folder_id": CARPETA, "email": "alchomes2025@gmail.com", "secret": "secreto-puente", **extra}
        return self.cliente.post("/mobile/facturas/conectado", json=cuerpo)

    def _config(self, pin):
        return self.cliente.get("/mobile/facturas/config", headers={"x-app-pin": pin}).get_json()

    # ── iniciar la conexión ──
    def test_solo_el_admin_puede_pedir_el_enlace(self):
        for pin in (None, self.PIN, "0000"):
            self.assertEqual(self._conectar(pin).status_code, 403)
        r = self._conectar(self.PIN_ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["url"].startswith("https://script.google.com/macros/s/ABC/exec?t="))

    def test_sin_conector_configurado(self):
        with mock.patch.object(mobile_facturas, "CONECTOR_URL", ""):
            self.assertEqual(self._conectar(self.PIN_ADMIN).status_code, 503)
            self.assertFalse(self._config(self.PIN_ADMIN)["conector"])

    def test_config_antes_y_despues(self):
        c = self._config(self.PIN_ADMIN)
        self.assertEqual((c["habilitado"], c["conector"], c["puede_conectar"]), (False, True, True))
        self.assertFalse(self._config(self.PIN)["puede_conectar"])         # el PIN normal no puede conectar
        self.assertEqual(self._avisar().status_code, 200)
        c = self._config(self.PIN)
        self.assertTrue(c["habilitado"])                                    # ya conectado: se ve también con el PIN normal

    # ── aviso del conector ──
    def test_aviso_correcto_guarda_la_raiz_y_crea_las_subcarpetas(self):
        r = self._avisar()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db.colecciones["facturas_config"]["alc-homes"]["drive_raiz"], CARPETA)
        self.assertEqual(self.db.colecciones["facturas_config"]["alc-homes"]["conectado_por"], "alchomes2025@gmail.com")
        self.assertEqual(facturas.raiz_drive("alc-homes"), CARPETA)         # y es la que usa guardar()
        _, raiz, rutas = [x for x in self.llamadas if x[0] == "estructura"][0]
        self.assertEqual(raiz, CARPETA)
        self.assertIn(["Facturas", "Hostal ALC Homes", str(facturas.date.today().year), "1T", "Gastos"], rutas)
        self.assertIn(["Facturas", "La Casa de la Primavera", str(facturas.date.today().year), "4T", "Ingresos"], rutas)
        self.assertEqual(len(rutas), 2 * 4 * 2)                              # 2 propiedades × 4 trimestres × gastos/ingresos

    def test_el_token_es_de_un_solo_uso(self):
        token = self._token()
        cuerpo = {"token": token, "folder_id": CARPETA, "email": "a@b.com", "secret": "secreto-puente"}
        self.assertEqual(self.cliente.post("/mobile/facturas/conectado", json=cuerpo).status_code, 200)
        self.assertEqual(self.cliente.post("/mobile/facturas/conectado", json=cuerpo).status_code, 409)

    def test_token_caducado_o_desconocido(self):
        token = self._token()
        self.db.colecciones["facturas_conexiones"][token]["creado"] = time.time() - 21 * 60
        cuerpo = {"token": token, "folder_id": CARPETA, "secret": "secreto-puente"}
        self.assertEqual(self.cliente.post("/mobile/facturas/conectado", json=cuerpo).status_code, 410)
        self.assertEqual(self.cliente.post("/mobile/facturas/conectado", json={**cuerpo, "token": "inventado"}).status_code, 404)
        self.assertNotIn("facturas_config", self.db.colecciones)

    def test_firma_incorrecta_no_conecta_nada(self):
        for secreto in ("", None, "otro"):
            self.assertEqual(self._avisar(secret=secreto).status_code, 401)
        self.assertNotIn("facturas_config", self.db.colecciones)
        self.assertEqual(self.llamadas, [])                                  # ni siquiera se toca el Drive

    def test_id_de_carpeta_no_valido(self):
        self.assertEqual(self._avisar(folder_id="../x").status_code, 400)
        self.assertEqual(self._avisar(folder_id="").status_code, 400)

    def test_espera_a_que_drive_de_acceso(self):
        self.ping_falla = 2                                                   # las dos primeras veces aún no ve la carpeta compartida
        self.assertEqual(self._avisar().status_code, 200)
        self.assertEqual(len([x for x in self.llamadas if x[0] == "ping"]), 3)

    def test_si_nunca_llega_el_acceso_no_se_conecta_y_el_enlace_sigue_valido(self):
        self.ping_falla = 99
        token = self._token()
        cuerpo = {"token": token, "folder_id": CARPETA, "email": "a@b.com", "secret": "secreto-puente"}
        self.assertEqual(self.cliente.post("/mobile/facturas/conectado", json=cuerpo).status_code, 502)
        self.assertNotIn("facturas_config", self.db.colecciones)
        self.ping_falla = 0
        self.assertEqual(self.cliente.post("/mobile/facturas/conectado", json=cuerpo).status_code, 200)   # se puede reintentar con el mismo enlace

    def test_variable_de_entorno_antigua_sigue_valiendo_como_respaldo(self):
        with mock.patch.dict(facturas.CLIENTES["alc-homes"], {"drive_raiz": "RAIZ_ENTORNO"}):
            drive_cliente.olvidar_cache()
            self.assertEqual(facturas.raiz_drive("alc-homes"), "RAIZ_ENTORNO")
            self._avisar()
            self.assertEqual(facturas.raiz_drive("alc-homes"), CARPETA)       # lo conectado manda sobre la variable


if __name__ == "__main__":
    unittest.main()

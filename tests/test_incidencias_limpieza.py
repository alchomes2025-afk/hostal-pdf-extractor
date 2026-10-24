"""Incidencias de limpieza de La Casa de la Primavera: PIN de la limpiadora, calendario ocupado/libre, marcar y quitar días, aviso a Ione."""
import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_limpieza
import mobile_routes
from services import incidencias_limpieza as inc

HOY = date(2026, 10, 24)


class Doc:
    def __init__(self, a, k): self.a, self.id = a, k
    def get(self):
        d = self.a.get(self.id)
        return type("S", (), {"exists": d is not None, "to_dict": lambda s: dict(d) if d else {}})()
    def set(self, datos): self.a[self.id] = dict(datos)
    def delete(self): self.a.pop(self.id, None)


class Col:
    def __init__(self, a): self.a = a
    def document(self, k): return Doc(self.a, k)
    def stream(self): return [Doc(self.a, k) for k in sorted(self.a)]


class Db:
    def __init__(self): self.a = {}
    def collection(self, n): return Col(self.a)


CASA = 720841
RESERVAS = [
    {"id": 1, "roomId": CASA, "arrival": "2026-10-25", "departure": "2026-10-27", "status": "confirmed", "guestName": "Nombre Secreto", "price": 999},
    {"id": 2, "roomId": CASA, "arrival": "2026-10-30", "departure": "2026-11-01", "status": "confirmed", "email": "bloqueo@bloqueo.com"},   # un bloqueo ocupa
    {"id": 3, "roomId": CASA, "arrival": "2026-11-05", "departure": "2026-11-07", "status": "cancelled"},                                     # cancelada: libre
    {"id": 4, "roomId": 702399, "arrival": "2026-10-25", "departure": "2026-10-27", "status": "confirmed"},                                    # otra propiedad
]


class Base(unittest.TestCase):
    def setUp(self):
        self.db = Db()
        self.avisos = []
        inc._cache["hasta"] = 0
        app = Flask(__name__)
        app.register_blueprint(mobile_limpieza.limpieza_bp)
        app.register_blueprint(mobile_routes.mobile_bp)
        self.c = app.test_client()
        for o, n, v in ((inc.config, "db", self.db), (mobile_routes, "MOBILE_APP_PIN", "1111"), (mobile_routes, "MOBILE_APP_PIN_ADMIN", "1379"),
                        (mobile_limpieza, "hoy_madrid", lambda: HOY), (mobile_limpieza, "_reservas", lambda: RESERVAS),
                        (mobile_limpieza, "alerta", lambda t, d="", nivel="critico": self.avisos.append((t, d, nivel)))):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("config.PIN_LIMPIEZA", "4268")
        p.start()
        self.addCleanup(p.stop)

    def get(self, ruta, pin):
        return self.c.get(ruta, headers={"x-app-pin": pin})

    def post(self, fecha, accion="marcar", pin="4268"):
        return self.c.post("/mobile/limpieza/incidencia", json={"fecha": fecha, "accion": accion}, headers={"x-app-pin": pin})


class Acceso(Base):
    def test_login_de_la_limpiadora_da_rol_limpieza(self):
        d = self.c.post("/mobile/login", json={"pin": "4268"}).get_json()
        self.assertEqual((d["ok"], d["rol"]), (True, "limpieza"))

    def test_el_pin_de_limpieza_no_abre_nada_mas(self):
        for ruta in ("/mobile/all-data", "/mobile/rooms", "/mobile/finance?propertyId=349341&month=2026-10", "/mobile/check-changes", "/mobile/limpieza/incidencias"):
            self.assertEqual(self.get(ruta, "4268").status_code, 403, ruta)     # la limpiadora es un perfil válido, pero sin permiso para nada más

    def test_pin_incorrecto(self):
        self.assertEqual(self.get("/mobile/limpieza/calendario", "0000").status_code, 401)
        self.assertEqual(self.post("2026-10-26", pin="0000").status_code, 401)

    def test_el_pin_normal_no_marca_incidencias(self):
        self.assertEqual(self.post("2026-10-26", pin="1111").status_code, 403)


class Calendario(Base):
    def test_solo_ocupado_o_libre_de_la_casa(self):
        d = self.get("/mobile/limpieza/calendario", "4268").get_json()
        self.assertEqual(d["ocupados"], ["2026-10-25", "2026-10-26", "2026-10-30", "2026-10-31"])
        texto = str(d)
        for secreto in ("Nombre Secreto", "999", "bloqueo@bloqueo.com", "702399"):
            self.assertNotIn(secreto, texto)


class Marcar(Base):
    def test_marca_avisa_y_aparece_en_el_calendario_de_gestion(self):
        r = self.post("2026-10-27")
        self.assertEqual((r.status_code, r.get_json()["incidencias"]), (200, ["2026-10-27"]))
        self.assertEqual(len(self.avisos), 1)
        self.assertIn("27/10/2026", self.avisos[0][1])
        self.assertIn("SALIDA", self.avisos[0][1])           # el 27 hay salida de huéspedes
        d = self.get("/mobile/limpieza/incidencias", "1111").get_json()
        self.assertEqual(d["dias"], ["2026-10-27"])

    def test_un_segundo_toque_no_vuelve_a_avisar(self):
        self.post("2026-10-27")
        self.post("2026-10-27")
        self.assertEqual(len(self.avisos), 1)

    def test_dias_seguidos_forman_un_rango(self):
        for f in ("2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"):
            self.post(f)
        d = self.get("/mobile/limpieza/incidencias", "1111").get_json()
        self.assertEqual(d["rangos"], [{"desde": "2026-10-28", "hasta": "2026-10-30", "dias": 3}, {"desde": "2026-11-02", "hasta": "2026-11-02", "dias": 1}])
        self.assertIn("del 28/10/2026 al 29/10/2026 (2 días)", self.avisos[1][1])

    def test_quitar_avisa_y_borra(self):
        self.post("2026-10-28")
        r = self.post("2026-10-28", accion="quitar")
        self.assertEqual(r.get_json()["incidencias"], [])
        self.assertEqual(len(self.avisos), 2)
        self.assertIn("retirada", self.avisos[1][0])

    def test_quitar_un_dia_sin_incidencia_no_avisa(self):
        self.post("2026-10-28", accion="quitar")
        self.assertEqual(self.avisos, [])

    def test_fechas_no_validas(self):
        self.assertEqual(self.post("2026-10-23").status_code, 400)      # ayer
        self.assertEqual(self.post("2028-01-01").status_code, 400)      # demasiado lejos
        self.assertEqual(self.post("mañana").status_code, 400)
        self.assertEqual(self.post("2026-10-28", accion="otra").status_code, 400)

    def test_sin_firestore_avisa_sin_romper(self):
        with mock.patch.object(inc.config, "db", None):
            self.assertEqual(self.post("2026-10-28").status_code, 503)
        self.assertEqual(self.avisos, [])

    def test_los_dias_pasados_no_se_enseñan(self):
        self.db.a["2026-10-01"] = {"fecha": "2026-10-01"}
        self.assertEqual(inc.dias(HOY, forzar=True), [])


if __name__ == "__main__":
    unittest.main()

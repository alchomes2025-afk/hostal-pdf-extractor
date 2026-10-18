"""Extracto bancario: se guarda en Facturas/<Propiedad>/<Año>/Extractos del Drive del cliente (para el cotejo con las facturas)."""
import base64
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_facturas
import mobile_routes
from services import drive_cliente, facturas, puente_plataforma


def b64(texto):
    return base64.b64encode(texto if isinstance(texto, bytes) else texto.encode()).decode()


class Extracto(unittest.TestCase):
    PIN, PIN_ADMIN = "1111", "9999"

    def setUp(self):
        self.guardados = []
        for objeto, nombre, valor in ((mobile_routes, "MOBILE_APP_PIN", self.PIN), (mobile_routes, "MOBILE_APP_PIN_ADMIN", self.PIN_ADMIN),
                                      (puente_plataforma, "configurado", lambda: True), (puente_plataforma, "guardar", self._guardar)):
            p = mock.patch.object(objeto, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(facturas.CLIENTES["alc-homes"], {"drive_raiz": "RAIZ"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(drive_cliente, "raiz", lambda c, por_defecto="": por_defecto)
        p.start()
        self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(mobile_facturas.facturas_bp)
        self.cliente = app.test_client()

    def _guardar(self, raiz, ruta, nombre, mime, contenido, registro=None, descripcion=None):
        self.guardados.append({"raiz": raiz, "ruta": ruta, "nombre": nombre, "mime": mime, "n": len(contenido), "registro": registro})
        return {"file_id": "F1", "url": "https://drive/f1", "nombre": nombre, "hoja_url": None}

    def _post(self, pin="1111", propiedad="349341", **archivo):
        cuerpo = {"propiedad_id": propiedad, "usuario": "Mónica",
                  "archivo": {"nombre": "movimientos octubre.csv", "mime": "text/csv", "base64": b64("fecha;concepto;importe\n01/10/2026;LUZ;-45,30\n"), **archivo}}
        return self.cliente.post("/mobile/facturas/extracto", json=cuerpo, headers={"x-app-pin": pin} if pin else {})

    def test_se_guarda_en_la_carpeta_extractos_de_la_propiedad_y_el_año(self):
        r = self._post()
        self.assertEqual(r.status_code, 200)
        g = self.guardados[0]
        self.assertEqual(g["ruta"], ["Facturas", "La Casa de la Primavera", str(datetime.now().year), "Extractos"])
        self.assertEqual(g["raiz"], "RAIZ")
        self.assertTrue(g["nombre"].endswith("_Extracto_movimientos octubre.csv"))
        self.assertIsNone(g["registro"])                                  # no lleva fila en la hoja de facturas
        self.assertEqual(r.get_json()["nombre"], g["nombre"])

    def test_formatos_admitidos_y_no(self):
        for mime in ("application/pdf", "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
            self.assertEqual(self._post(mime=mime, nombre="x").status_code, 200)
        for mime in ("application/zip", "image/png", "text/html", ""):
            self.assertEqual(self._post(mime=mime).status_code, 400)

    def test_tamano_y_vacio(self):
        self.assertEqual(self._post(base64="").status_code, 400)
        self.assertEqual(self._post(base64=b64(b"x" * (10 * 1024 * 1024 + 1))).status_code, 400)

    def test_permisos(self):
        self.assertEqual(self._post(pin=None).status_code, 401)
        self.assertEqual(self._post(pin=self.PIN, propiedad="339751").status_code, 403)     # el hostal solo con el PIN de administrador
        self.assertEqual(self._post(pin=self.PIN_ADMIN, propiedad="339751").status_code, 200)
        self.assertEqual(self._post(propiedad="1").status_code, 400)

    def test_error_del_drive(self):
        def falla(*a, **k):
            raise puente_plataforma.PuenteError("caído")
        with mock.patch.object(puente_plataforma, "guardar", falla):
            self.assertEqual(self._post().status_code, 502)

    def test_sin_drive_conectado(self):
        with mock.patch.dict(facturas.CLIENTES["alc-homes"], {"drive_raiz": ""}):
            self.assertEqual(self._post().status_code, 503)


if __name__ == "__main__":
    unittest.main()

"""Volver a subir un ZIP entero: se reconocen por la huella del archivo los que ya están guardados y solo se leen los nuevos."""
import base64
import hashlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_facturas
import mobile_routes
from services import facturas


class Doc:
    def __init__(self, a, k): self.a, self.k = a, k
    def get(self):
        d = self.a.get(self.k)
        return type("S", (), {"exists": d is not None, "to_dict": lambda s: dict(d) if d else {}})()


class Col:
    def __init__(self, a): self.a = a
    def document(self, k): return Doc(self.a, k)


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Conocidas(unittest.TestCase):
    def setUp(self):
        self.guardadas = {}
        for objeto, nombre, valor in ((facturas, "_coleccion", lambda: Col(self.guardadas)), (mobile_routes, "MOBILE_APP_PIN", "1111")):
            p = mock.patch.object(objeto, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(mobile_facturas.facturas_bp)
        self.cliente = app.test_client()

    def test_devuelve_solo_las_ya_guardadas(self):
        a, b, c = sha(b"factura a"), sha(b"factura b"), sha(b"factura c")
        self.guardadas[facturas._doc_id("alc-homes", a)] = {"x": 1}
        self.guardadas[facturas._doc_id("alc-homes", c)] = {"x": 1}
        self.assertEqual(facturas.conocidas("alc-homes", [a, b, c]), [a, c])

    def test_la_huella_es_la_misma_que_usa_el_guardado(self):
        contenido = b"%PDF-1.4 una factura"
        self.guardadas[facturas._doc_id("alc-homes", facturas.huella(contenido))] = {"x": 1}
        self.assertEqual(facturas.conocidas("alc-homes", [sha(contenido)]), [sha(contenido)])      # SHA-256 del contenido, como calcula el navegador

    def test_otro_cliente_huellas_malas_y_duplicadas(self):
        a = sha(b"a")
        self.guardadas[facturas._doc_id("alc-homes", a)] = {"x": 1}
        self.assertEqual(facturas.conocidas("otro", [a]), [])
        self.assertEqual(facturas.conocidas("alc-homes", ["../x", "", None, "ZZ" * 32, a, a]), [a])

    def test_sin_base_de_datos(self):
        with mock.patch.object(facturas, "_coleccion", lambda: None):
            self.assertEqual(facturas.conocidas("alc-homes", [sha(b"a")]), [])

    def test_endpoint(self):
        a = sha(b"a")
        self.guardadas[facturas._doc_id("alc-homes", a)] = {"x": 1}
        r = self.cliente.post("/mobile/facturas/conocidas", json={"huellas": [a, sha(b"b")]}, headers={"x-app-pin": "1379"})
        self.assertEqual(r.get_json(), {"ok": True, "conocidas": [a]})
        self.assertEqual(self.cliente.post("/mobile/facturas/conocidas", json={"huellas": [a]}).status_code, 401)

    def test_maximo_200_huellas_por_consulta(self):
        huellas = [sha(str(i).encode()) for i in range(250)]
        for h in huellas:
            self.guardadas[facturas._doc_id("alc-homes", h)] = {"x": 1}
        self.assertEqual(len(facturas.conocidas("alc-homes", huellas)), 200)


if __name__ == "__main__":
    unittest.main()

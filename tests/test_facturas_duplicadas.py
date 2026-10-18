"""La misma factura en otro archivo (mismo número y total) se avisa como posible duplicado al leerla."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import facturas


class Doc:
    def __init__(self, d): self.d, self.id = d, "x"
    def to_dict(self): return dict(self.d)


class Col:
    def __init__(self, docs): self.docs, self.filtros = docs, ()
    def where(self, campo, op, valor):
        c = Col(self.docs)
        c.filtros = self.filtros + ((campo, valor),)
        return c
    def stream(self):
        return [Doc(d) for d in self.docs if all(d.get(c) == v for c, v in self.filtros)]


GUARDADA = {"cliente": "alc-homes", "numero": "9559/26", "total": 95.59, "fecha": "2026-07-29", "proveedor": "ASESORIA AUTONOMOS ONLINE SLU", "url": "https://drive/a"}


class Duplicadas(unittest.TestCase):
    def _col(self, docs):
        p = mock.patch.object(facturas, "_coleccion", lambda: Col(docs))
        p.start()
        self.addCleanup(p.stop)

    def test_mismo_numero_y_total_es_posible_duplicada(self):
        self._col([GUARDADA])
        r = facturas.misma_factura("alc-homes", {"numero": "9559/26", "total": 95.59})
        self.assertEqual(r["proveedor"], "ASESORIA AUTONOMOS ONLINE SLU")

    def test_otro_total_otro_numero_o_otro_cliente_no(self):
        self._col([GUARDADA])
        self.assertIsNone(facturas.misma_factura("alc-homes", {"numero": "9559/26", "total": 100.0}))
        self.assertIsNone(facturas.misma_factura("alc-homes", {"numero": "1", "total": 95.59}))
        self.assertIsNone(facturas.misma_factura("otro", {"numero": "9559/26", "total": 95.59}))

    def test_sin_numero_o_sin_total_no_se_compara(self):
        self._col([GUARDADA])
        self.assertIsNone(facturas.misma_factura("alc-homes", {"numero": "", "total": 95.59}))
        self.assertIsNone(facturas.misma_factura("alc-homes", {"numero": "9559/26", "total": None}))

    def test_sin_base_de_datos(self):
        with mock.patch.object(facturas, "_coleccion", lambda: None):
            self.assertIsNone(facturas.misma_factura("alc-homes", {"numero": "9559/26", "total": 95.59}))

    def test_leer_avisa(self):
        self._col([GUARDADA])
        with mock.patch.object(facturas, "comprobar_propiedad", lambda *a: "349341"), mock.patch.object(facturas, "_archivo", lambda a: (b"x", "application/pdf")), \
             mock.patch.object(facturas, "ya_guardada", lambda *a: None), mock.patch.object(facturas, "texto_de", lambda c, m: "FACTURA EXTRACTO 29/07/2026 total 95,59"), \
             mock.patch.object(facturas, "_preguntar_ia", lambda p: {"tipo": "gasto", "proveedor": "Asesoria", "numero": "9559/26", "fecha": "2026-08-19", "total": 95.59}):
            r = facturas.leer("alc-homes", "349341", {}, True)
        self.assertIsNotNone(r["duplicado"])
        self.assertIn("mismo número y total", r["avisos"][0])


if __name__ == "__main__":
    unittest.main()

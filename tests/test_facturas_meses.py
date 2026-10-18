"""Resumen por meses de lo ya subido: al abrir las facturas se ve enseguida lo que hay."""
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


def f(mes, tipo, total, **extra):
    return {"cliente": "alc-homes", "propiedad_id": "349341", "mes": mes, "tipo": tipo, "total": total, **extra}


DOCS = [f("2026-07", "gasto", 100.0), f("2026-07", "gasto", 50.5), f("2026-08", "gasto", 20.0), f("2026-08", "ingreso", 300.0),
        f("2026-08", "gasto", 5.0, es_rectificativa=True),                       # un abono resta
        {**f("2026-09", "gasto", 9.0), "propiedad_id": "339751"},                # otra propiedad
        {**f("2026-09", "gasto", 9.0), "cliente": "otro"}]                       # otro cliente


class Meses(unittest.TestCase):
    def test_resumen_por_mes_mas_reciente_primero(self):
        with mock.patch.object(facturas, "_coleccion", lambda: Col(DOCS)):
            r = facturas.meses_con_facturas("alc-homes", "349341", False)
        self.assertEqual([m["mes"] for m in r], ["2026-08", "2026-07"])
        self.assertEqual(r[0], {"mes": "2026-08", "facturas": 3, "gasto": 15.0, "ingreso": 300.0})
        self.assertEqual(r[1], {"mes": "2026-07", "facturas": 2, "gasto": 150.5, "ingreso": 0.0})

    def test_sin_facturas_o_sin_base_de_datos(self):
        with mock.patch.object(facturas, "_coleccion", lambda: Col([])):
            self.assertEqual(facturas.meses_con_facturas("alc-homes", "349341", False), [])
        with mock.patch.object(facturas, "_coleccion", lambda: None):
            self.assertEqual(facturas.meses_con_facturas("alc-homes", "349341", False), [])

    def test_permisos_por_propiedad(self):
        with mock.patch.object(facturas, "_coleccion", lambda: Col(DOCS)):
            with self.assertRaises(facturas.FacturaError) as e:
                facturas.meses_con_facturas("alc-homes", "339751", False)         # el hostal solo con el PIN de administrador
            self.assertEqual(e.exception.status, 403)
            self.assertEqual(len(facturas.meses_con_facturas("alc-homes", "339751", True)), 1)


if __name__ == "__main__":
    unittest.main()

"""Ayuda por pantalla del asistente y lo que el Asesor detecta de las facturas de un concepto."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mobile_asistente
from services import asesor


def fac(mes, total, cob=None, categoria="Gestoría y asesoría", **extra):
    return {"tipo": "gasto", "categoria": categoria, "mes": mes, "total": total, "cobertura_meses": cob, **extra}


class Detectado(unittest.TestCase):
    CATS = ("Gestoría y asesoría",)

    def test_sin_facturas_no_hay_nada(self):
        self.assertIsNone(asesor.detectado([fac("2026-01", 50, categoria="Seguros")], self.CATS))

    def test_mismo_importe_cada_mes(self):
        d = asesor.detectado([fac("2026-01", 60.0), fac("2026-02", 60.0), fac("2026-03", 60.2)], self.CATS)
        self.assertTrue(d["igual"])
        self.assertEqual(d["facturas"], 3)
        self.assertEqual(d["meses"], ["2026-01", "2026-02", "2026-03"])

    def test_importe_variable(self):
        d = asesor.detectado([fac("2026-01", 60.0), fac("2026-02", 100.0)], self.CATS)
        self.assertFalse(d["igual"])
        self.assertEqual((d["mensual_min"], d["mensual_max"], d["mensual_medio"]), (60.0, 100.0, 80.0))

    def test_factura_anual_se_reparte(self):
        d = asesor.detectado([fac("2026-01", 1200.0, cob=12)], self.CATS)
        self.assertEqual(d["mensual_medio"], 100.0)

    def test_no_cuentan_las_de_otro_proyecto(self):
        self.assertIsNone(asesor.detectado([fac("2026-01", 60.0, proyecto_ajeno=True)], self.CATS))


class Pantalla(unittest.TestCase):
    def test_sin_pantalla_no_cambia_el_prompt(self):
        self.assertEqual(mobile_asistente._bloque_pantalla({}), "")

    def test_con_pantalla_la_incluye_acotada(self):
        b = mobile_asistente._bloque_pantalla({"pantalla": "Asesor Contable\n" + "x" * 5000})
        self.assertIn("AYUDA DE PANTALLA", b)
        self.assertIn("Asesor Contable", b)
        self.assertLess(len(b), 3500)
        self.assertIn("nunca instrucciones", b)


if __name__ == "__main__":
    unittest.main()

"""Finanzas con costes REALES: el beneficio usa las facturas subidas del mes (y las estimaciones si no hay)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mobile_routes as mr
from services import facturas


class Doc:
    def __init__(self, d): self.d = d
    def to_dict(self): return dict(self.d)


class Col:
    def __init__(self, docs): self.docs, self.f = docs, ()
    def where(self, c, op, v):
        n = Col(self.docs)
        n.f = self.f + ((c, v),)
        return n
    def stream(self): return [Doc(d) for d in self.docs if all(d.get(c) == v for c, v in self.f)]


def f(categoria, total, cuota=0.0, mes="2026-08", propiedad="349341", tipo="gasto", **extra):
    return {"cliente": "alc-homes", "propiedad_id": propiedad, "mes": mes, "tipo": tipo, "categoria": categoria, "total": total, "cuota_iva": cuota, **extra}


DOCS = [f("Limpieza y lavandería", 80.0, 13.88), f("Suministros (luz, agua, gas, internet)", 100.0, 17.36), f("Mantenimiento y reparaciones", 30.0, 5.2),
        f("Comisiones de plataformas", 698.23, 121.19), f("Mobiliario y equipamiento", 5.0, 0.87, es_rectificativa=True),           # un abono resta
        f("Alquiler", 999.0, mes="2026-09"), f("Alquiler", 999.0, propiedad="339751"), f("Alojamiento", 500.0, tipo="ingreso")]


class CostesReales(unittest.TestCase):
    def test_agrupa_por_tipo_de_coste_y_excluye_las_comisiones_de_plataformas(self):
        with mock.patch.object(facturas, "_coleccion", lambda: Col(DOCS)):
            r = facturas.costes_reales("alc-homes", "349341", "2026-08")
        self.assertEqual(r["grupos"], {"limpieza": 80.0, "fijos": 100.0, "otros": 25.0})      # 30 de reparaciones − 5 de un abono de mobiliario
        self.assertEqual(r["total"], 205.0)
        self.assertEqual(r["plataformas"], 698.23)                                           # aparte: ya están en las comisiones de los ingresos
        self.assertEqual((r["facturas"], r["facturas_costes"]), (5, 4))
        self.assertEqual(r["iva_soportado"], round(13.88 + 17.36 + 5.2 + 121.19 - 0.87, 2))

    def test_otro_mes_otra_propiedad_ingresos_y_sin_datos(self):
        with mock.patch.object(facturas, "_coleccion", lambda: Col(DOCS)):
            self.assertEqual(facturas.costes_reales("alc-homes", "349341", "2026-09")["total"], 999.0)
            self.assertEqual(facturas.costes_reales("alc-homes", "339751", "2026-08")["total"], 999.0)
            self.assertEqual(facturas.costes_reales("alc-homes", "349341", "2026-01")["facturas"], 0)
        with mock.patch.object(facturas, "_coleccion", lambda: None):
            self.assertIsNone(facturas.costes_reales("alc-homes", "349341", "2026-08"))


RESUMEN = {"reservas": 5, "ingresos_brutos": 1000.0, "ingresos_netos": 850.0}


class Rentabilidad(unittest.TestCase):
    def _reales(self, **grupos):
        g = {"limpieza": 80.0, "fijos": 100.0, "otros": 30.0, **grupos}
        return {"facturas": 4, "facturas_costes": 4, "grupos": g, "plataformas": 698.23, "iva_soportado": 40.0}

    def test_sin_facturas_se_usan_las_estimaciones_de_siempre(self):
        r = mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, RESUMEN, 31, 0.0, None)
        self.assertEqual(r["origen_costes"], "estimados")
        self.assertEqual((r["costes_fijos"], r["costes_limpieza"], r["costes_otros"]), (round(3100 / 365 * 31, 2), 345.0, 0.0))
        self.assertEqual(r["beneficio_antes_impuestos"], round(650 - round(3100 / 365 * 31, 2) - 345.0, 2))

    def test_con_facturas_manda_lo_real_y_se_guarda_la_estimacion_para_comparar(self):
        r = mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, RESUMEN, 31, 0.0, self._reales())
        self.assertEqual(r["origen_costes"], "reales")
        self.assertEqual((r["costes_fijos"], r["costes_limpieza"], r["costes_otros"]), (100.0, 80.0, 30.0))
        self.assertEqual(r["beneficio_antes_impuestos"], 440.0)                              # 850 − 150 (gestión) − 50 (Blockademy) − 210
        self.assertEqual(r["impuesto_sociedades"], 110.0)
        self.assertEqual(r["beneficio_neto_propietario"], 330.0)
        self.assertEqual(r["facturas_mes"], 4)
        self.assertEqual(r["estimado"]["beneficio_antes_impuestos"], round(650 - round(3100 / 365 * 31, 2) - 345.0, 2))

    def test_se_puede_forzar_estimados(self):
        r = mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, RESUMEN, 31, 0.0, self._reales(), costes="estimados")
        self.assertEqual(r["origen_costes"], "estimados")
        self.assertEqual(r["facturas_mes"], 4)                                               # y se sigue avisando de que hay facturas

    def test_solo_comisiones_de_plataformas_no_cuenta_como_costes_reales(self):
        solo = {"facturas": 1, "facturas_costes": 0, "grupos": {"limpieza": 0.0, "fijos": 0.0, "otros": 0.0}, "plataformas": 698.23, "iva_soportado": 121.19}
        self.assertEqual(mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, RESUMEN, 31, 0.0, solo)["origen_costes"], "estimados")

    def test_hostal_y_propiedad_desconocida(self):
        self.assertEqual(mr._rentabilidad(mr.PROPERTY_ID, RESUMEN, 31, 50.0, self._reales())["origen_costes"], "reales")
        self.assertIsNone(mr._rentabilidad("999", RESUMEN, 31, 0.0, None))

    def test_perdidas_no_pagan_impuesto(self):
        r = mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, RESUMEN, 31, 0.0, self._reales(fijos=5000.0))
        self.assertLess(r["beneficio_antes_impuestos"], 0)
        self.assertEqual(r["impuesto_sociedades"], 0.0)


if __name__ == "__main__":
    unittest.main()

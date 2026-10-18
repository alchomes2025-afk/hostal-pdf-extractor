"""Fechas de facturas: formatos habituales y los de un talonario escrito a mano (casillas DÍA / MES / AÑO)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import facturas


class Fechas(unittest.TestCase):
    def test_formatos_de_siempre(self):
        for texto, esperado in (("2026-10-14", "2026-10-14"), ("14/10/2026", "2026-10-14"), ("14-10-2026", "2026-10-14"), ("14-10-26", "2026-10-14"),
                                ("14.10.2026", "2026-10-14"), ("14/10/26", "2026-10-14"), ("2026-10-14T12:00:00", "2026-10-14")):
            self.assertEqual(facturas.a_fecha(texto), esperado, texto)

    def test_talonario_casillas_dia_mes_anio(self):
        for texto in ("19 8 26", "19/8/26", "19-8-26", "19 / 8 / 26", "19.8.26", "19 - 08 - 2026", " 19  8  26 ", "2026-8-19", "19 de agosto de 2026", "19 agosto 26"):
            self.assertEqual(facturas.a_fecha(texto), "2026-08-19", texto)

    def test_casillas_en_un_diccionario(self):
        self.assertEqual(facturas.a_fecha({"dia": 19, "mes": 8, "anio": 26}), "2026-08-19")
        self.assertEqual(facturas.a_fecha({"dia": "19", "mes": "08", "año": "2026"}), "2026-08-19")

    def test_lo_que_no_es_fecha_es_none(self):
        for texto in (None, "", "   ", "sin fecha", "32/13/2026", "00/00/00", "30/02/2026", 1073, "19 8"):
            self.assertIsNone(facturas.a_fecha(texto), texto)

    def test_normalizar_la_usa(self):
        datos, avisos = facturas.normalizar({"tipo": "gasto", "emisor": "Haiteng Liu", "fecha": "19 8 26", "total": 60})
        self.assertEqual(datos["fecha"], "2026-08-19")
        self.assertNotIn("No se ha leído la fecha.", avisos)


class IvaNoDesglosado(unittest.TestCase):
    def test_talonario_con_iva_21_y_cuota_0_no_se_da_por_bueno(self):
        datos, avisos = facturas.normalizar({"tipo": "gasto", "emisor": "Haiteng Liu", "fecha": "19/8/26", "base_imponible": 60, "iva_pct": 21, "cuota_iva": 0, "total": 60})
        self.assertIsNone(datos["iva_pct"])
        self.assertIsNone(datos["cuota_iva"])
        self.assertFalse(datos["cuadra"])                                   # no sale como «lista para guardar»
        self.assertTrue(any("no desglosa el IVA" in a for a in avisos))

    def test_factura_normal_sin_cambios(self):
        datos, avisos = facturas.normalizar({"tipo": "gasto", "emisor": "X", "fecha": "2026-10-14", "base_imponible": 100, "iva_pct": 21, "cuota_iva": 21, "total": 121})
        self.assertEqual((datos["iva_pct"], datos["cuota_iva"], datos["cuadra"]), (21.0, 21.0, True))
        self.assertEqual(avisos, [])

    def test_exenta_de_verdad_iva_0_no_se_toca(self):
        datos, avisos = facturas.normalizar({"tipo": "gasto", "emisor": "X", "fecha": "2026-10-14", "base_imponible": 100, "iva_pct": 0, "cuota_iva": 0, "total": 100})
        self.assertEqual((datos["iva_pct"], datos["cuota_iva"]), (0.0, 0.0))
        self.assertTrue(datos["cuadra"])

    def test_solo_total_sin_nada_mas_se_completa_como_siempre(self):
        datos, _ = facturas.normalizar({"tipo": "gasto", "emisor": "X", "fecha": "2026-10-14", "total": 60})
        self.assertIsNone(datos["cuota_iva"])

    def test_el_prompt_de_vision_pide_el_nif(self):
        self.assertIn("nif_emisor", facturas._prompt(facturas.CLIENTES["alc-homes"], None, en_imagen=True))
        self.assertIn("IMAGEN", facturas._prompt(facturas.CLIENTES["alc-homes"], None, en_imagen=True))


if __name__ == "__main__":
    unittest.main()

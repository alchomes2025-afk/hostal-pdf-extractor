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


if __name__ == "__main__":
    unittest.main()

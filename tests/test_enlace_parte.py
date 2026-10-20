"""Mensaje de emergencia con el enlace de RPV de la habitación (el huésped hace el parte antes de recibir los códigos)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mobile_asistente as asistente
from services import enlace_parte

BOOKING = {"id": 94352000, "roomId": 702399, "arrival": "2026-10-21", "departure": "2026-10-23", "firstName": "Ana Maria", "lastName": "Perez"}


class Redactar(unittest.TestCase):
    def test_en_espanol_lleva_enlace_fechas_y_referencia(self):
        texto, habitacion, enlace = enlace_parte.redactar_enlace_parte(BOOKING, "es")
        self.assertIn("https://app.registroparteviajeros.com/propiedad/d1ydBdUSOr", texto)
        self.assertIn("llegada 21/10/2026 y salida 23/10/2026", texto)
        self.assertIn("94352000", texto)
        self.assertTrue(texto.startswith("Hola Ana,"))
        self.assertEqual(habitacion, "Cala del Moraig")
        self.assertEqual(enlace, "https://app.registroparteviajeros.com/propiedad/d1ydBdUSOr")

    def test_en_ingles_por_defecto(self):
        texto, _, _ = enlace_parte.redactar_enlace_parte(BOOKING, None)
        self.assertIn("arrival 21/10/2026 and departure 23/10/2026", texto)

    def test_no_lleva_codigos_de_acceso(self):
        texto, _, _ = enlace_parte.redactar_enlace_parte(BOOKING, "es")
        self.assertNotIn("130773", texto)

    def test_primavera_usa_su_enlace(self):
        _, habitacion, enlace = enlace_parte.redactar_enlace_parte({**BOOKING, "roomId": 720841}, "es")
        self.assertTrue(enlace.endswith("pLXSG3umvq"))
        self.assertEqual(habitacion, "La Casa de la Primavera")

    def test_habitacion_sin_enlace_o_sin_fechas_da_error(self):
        with self.assertRaises(enlace_parte.ErrorEnlace):
            enlace_parte.redactar_enlace_parte({**BOOKING, "roomId": 1}, "es")
        with self.assertRaises(enlace_parte.ErrorEnlace):
            enlace_parte.redactar_enlace_parte({"roomId": 702399}, "es")


class Herramienta(unittest.TestCase):
    def _usar(self, ctx, **args):
        with mock.patch.object(asistente, "buscar_booking_por_id", return_value=BOOKING):
            return asistente.ejecutar_herramienta("enlace_parte_emergencia", json.dumps({"reserva": "94352000", "idioma": "es", **args}), ctx)

    def test_funciona_con_cualquier_usuario_y_no_envia(self):
        for ctx in ({"es_admin": True, "borradores": []}, {"es_admin": False, "borradores": []}):
            res = self._usar(ctx)
            self.assertIn("propiedad/d1ydBdUSOr", res["mensaje"])
            self.assertIn("NO se ha enviado", res["nota"])
            self.assertEqual(ctx["borradores"], [])   # no deja ningún borrador de correo

    def test_reserva_inexistente(self):
        with mock.patch.object(asistente, "buscar_booking_por_id", return_value=None):
            res = asistente.ejecutar_herramienta("enlace_parte_emergencia", json.dumps({"reserva": "1"}), {"es_admin": False, "borradores": []})
        self.assertIn("error", res)

    def test_la_ven_todos_los_usuarios(self):
        nombres = [t["function"]["name"] for t in asistente.herramientas_para({"es_admin": False})]
        self.assertIn("enlace_parte_emergencia", nombres)

    def test_el_prompt_explica_el_orden_de_la_emergencia(self):
        self.assertIn("enlace_parte_emergencia", asistente._system({"es_admin": False}))


if __name__ == "__main__":
    unittest.main()

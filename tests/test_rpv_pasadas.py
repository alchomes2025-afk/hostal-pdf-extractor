"""Pruebas de parte_comprobado_estancia_iniciada: la copia es por cuenta+fecha, el resultado por habitación."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import rpv

FECHA = "2026-10-10"


def _parte(prop, estado, completado):
    return {"propiedad_id": prop, "fecha_entrada": FECHA, "estado": estado, "parte_completado": completado}


class PartePasadaPorHabitacion(unittest.TestCase):
    def setUp(self):
        rpv._pasadas.clear()
        for nombre, valor in (("RPV_PROPERTY_MAP", {"A": 1, "B": 2}),
                              ("RPV_API_KEY_MAP", {}),
                              ("RPV_API_KEY", "clave")):
            p = mock.patch.object(rpv, nombre, valor)
            p.start()
            self.addCleanup(p.stop)

    def test_dos_habitaciones_misma_fecha_no_se_mezclan(self):
        partes = [_parte(1, "comunicado", True), _parte(2, "pendiente", False)]
        with mock.patch.object(rpv, "_pedir_partes", return_value=partes) as pedir:
            self.assertEqual(rpv.parte_comprobado_estancia_iniciada("A", FECHA), "completado")
            # B sale de la copia de la misma cuenta y fecha, sin heredar el «completado» de A
            self.assertEqual(rpv.parte_comprobado_estancia_iniciada("B", FECHA), "pendiente")
            self.assertEqual(pedir.call_count, 1)

    def test_orden_inverso(self):
        partes = [_parte(1, "comunicado", True), _parte(2, "pendiente", False)]
        with mock.patch.object(rpv, "_pedir_partes", return_value=partes):
            self.assertEqual(rpv.parte_comprobado_estancia_iniciada("B", FECHA), "pendiente")
            self.assertEqual(rpv.parte_comprobado_estancia_iniciada("A", FECHA), "completado")

    def test_habitacion_sin_parte_es_none(self):
        with mock.patch.object(rpv, "_pedir_partes", return_value=[_parte(1, "comunicado", True)]):
            self.assertEqual(rpv.parte_comprobado_estancia_iniciada("A", FECHA), "completado")
            self.assertIsNone(rpv.parte_comprobado_estancia_iniciada("B", FECHA))

    def test_solo_completado_es_permanente(self):
        partes = [_parte(1, "comunicado", True), _parte(2, "pendiente", False)]
        with mock.patch.object(rpv, "_pedir_partes", return_value=partes) as pedir:
            rpv.parte_comprobado_estancia_iniciada("A", FECHA)
            rpv._pasadas[("clave", FECHA)]["t"] -= 3600   # la copia caduca
            # A (completado) sigue válido sin llamar; B (pendiente) se vuelve a comprobar
            self.assertEqual(rpv.parte_comprobado_estancia_iniciada("A", FECHA), "completado")
            self.assertEqual(pedir.call_count, 1)
            pedir.return_value = [_parte(2, "comunicado", True)]
            self.assertEqual(rpv.parte_comprobado_estancia_iniciada("B", FECHA), "completado")
            self.assertEqual(pedir.call_count, 2)
            # el completado de A no se pierde al renovar la copia
            self.assertEqual(rpv.parte_comprobado_estancia_iniciada("A", FECHA), "completado")


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import solapes
from services.solapes import hallar_solapes


def reserva(id, room, llegada, salida, nombre="Ana", unit=1, canal="Booking.com"):
    return {"id": id, "roomId": room, "unitId": unit, "arrival": llegada, "departure": salida, "firstName": nombre,
            "lastName": "X", "apiSource": canal, "apiReference": f"R{id}", "status": "confirmed"}


HOY = "2026-10-22"


class HallarSolapes(unittest.TestCase):
    def ids(self, reservas):
        return [(a["id"], b["id"]) for a, b in hallar_solapes(reservas, HOY)]

    def test_sobreventa_misma_habitacion(self):
        self.assertEqual(self.ids([reserva(1, 702399, "2026-10-23", "2026-10-25"),
                                   reserva(2, 702399, "2026-10-24", "2026-10-26")]), [(1, 2)])

    def test_mismas_fechas_exactas(self):
        self.assertEqual(self.ids([reserva(1, 1, "2026-10-23", "2026-10-25"), reserva(2, 1, "2026-10-23", "2026-10-25")]), [(1, 2)])

    def test_una_dentro_de_otra(self):
        self.assertEqual(self.ids([reserva(1, 1, "2026-10-20", "2026-11-05"), reserva(2, 1, "2026-10-25", "2026-10-26")]), [(1, 2)])

    def test_salir_el_dia_que_entra_otro_no_es_solape(self):
        self.assertEqual(self.ids([reserva(1, 1, "2026-10-23", "2026-10-25"), reserva(2, 1, "2026-10-25", "2026-10-27")]), [])

    def test_habitaciones_distintas_no_se_pisan(self):
        self.assertEqual(self.ids([reserva(1, 1, "2026-10-23", "2026-10-25"), reserva(2, 2, "2026-10-23", "2026-10-25")]), [])

    def test_unidades_distintas_de_un_mismo_tipo_no_se_pisan(self):
        self.assertEqual(self.ids([reserva(1, 1, "2026-10-23", "2026-10-25", unit=1), reserva(2, 1, "2026-10-23", "2026-10-25", unit=2)]), [])

    def test_un_solape_ya_terminado_se_ignora(self):
        self.assertEqual(self.ids([reserva(1, 1, "2026-10-01", "2026-10-05"), reserva(2, 1, "2026-10-03", "2026-10-06")]), [])

    def test_tres_reservas_encadenadas_sin_solape(self):
        self.assertEqual(self.ids([reserva(1, 1, "2026-10-22", "2026-10-23"), reserva(2, 1, "2026-10-23", "2026-10-24"),
                                   reserva(3, 1, "2026-10-24", "2026-10-25")]), [])


class AvisoDeSolapes(unittest.TestCase):
    def setUp(self):
        solapes._ultima_pasada = 0.0
        solapes._avisados_en_memoria.clear()
        for nombre, valor in (("alerta", mock.Mock()), ("_doc_ref", mock.Mock(return_value=None)),
                              ("hoy_madrid", mock.Mock(return_value=__import__("datetime").date(2026, 10, 22)))):
            p = mock.patch.object(solapes, nombre, valor)
            setattr(self, nombre, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(solapes, "_leer_reservas", return_value=[reserva(1, 702399, "2026-10-23", "2026-10-25", "Ana"),
                                                                       reserva(2, 702399, "2026-10-24", "2026-10-26", "Luis", canal="Expedia")])
        self.leer = p.start()
        self.addCleanup(p.stop)

    def test_avisa_con_los_datos_de_las_dos_y_una_sola_vez(self):
        solapes.avisar_solapes(ahora=10_000)
        self.alerta.assert_called_once()
        titulo, detalle = self.alerta.call_args.args[:2]
        self.assertIn("sobreventa", titulo)
        for dato in ("Ana", "Luis", "Booking.com", "Expedia", "R1", "R2", "2026-10-23", "2026-10-26"):
            self.assertIn(dato, detalle)
        self.assertEqual(self.alerta.call_args.kwargs["nivel"], "critico")
        solapes.avisar_solapes(ahora=10_000 + 3 * 3600)   # otra pasada, mismo par: no repite
        self.alerta.assert_called_once()

    def test_como_mucho_una_comprobacion_por_hora(self):
        solapes.avisar_solapes(ahora=10_000)
        solapes.avisar_solapes(ahora=10_000 + 600)
        self.assertEqual(self.leer.call_count, 1)

    def test_si_beds24_falla_no_lanza_ni_avisa(self):
        self.leer.side_effect = RuntimeError("timeout")
        solapes.avisar_solapes(ahora=10_000)
        self.alerta.assert_not_called()

    def test_sin_solapes_no_avisa(self):
        self.leer.return_value = [reserva(1, 1, "2026-10-23", "2026-10-25")]
        solapes.avisar_solapes(ahora=10_000)
        self.alerta.assert_not_called()


if __name__ == "__main__":
    unittest.main()

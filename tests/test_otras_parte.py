import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.checkin as checkin

HOY = date(2026, 10, 18)


class ParteDeLaOtraReserva(unittest.TestCase):
    def _llamar(self, booking, recibido):
        with mock.patch.object(checkin, "parte_recibido_para", return_value=recibido) as rpv:
            return checkin._parte_pendiente_de_otra(booking, HOY), rpv

    def test_pendiente_si_el_parte_no_esta(self):
        r, rpv = self._llamar({"roomId": 702399, "arrival": "2026-10-19"}, False)
        self.assertIs(r, True)
        rpv.assert_called_once_with("702399", "2026-10-19")

    def test_no_pendiente_si_ya_esta(self):
        self.assertIs(self._llamar({"roomId": 702399, "arrival": "2026-10-19"}, True)[0], False)

    def test_ya_empezada_no_se_sabe(self):
        r, rpv = self._llamar({"roomId": 702399, "arrival": "2026-10-17"}, False)
        self.assertIsNone(r)
        rpv.assert_not_called()

    def test_un_fallo_de_rpv_no_rompe_nada(self):
        with mock.patch.object(checkin, "parte_recibido_para", side_effect=RuntimeError("boom")):
            self.assertIsNone(checkin._parte_pendiente_de_otra({"roomId": 1, "arrival": "2026-10-19"}, HOY))

    def test_no_toca_el_reloj_de_intentos(self):
        with mock.patch.object(checkin, "parte_recibido_para", return_value=False), \
             mock.patch("services.parte_intentos.aviso_parte_no_disponible") as aviso:
            checkin._parte_pendiente_de_otra({"roomId": 1, "arrival": "2026-10-19"}, HOY)
        aviso.assert_not_called()


if __name__ == "__main__":
    unittest.main()

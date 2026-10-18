import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.sin_mensajeria_avisos import _sin_mensajeria, texto_aviso

EXPEDIA_EAN = {
    "referer": "Expedia Affiliate Network", "canal": "Expedia", "book_id": 94390525, "api_reference": "2577279377",
    "nombre_huesped": {"nombre": "María de los Ángeles", "apellidos": "Gómez Manzanero"},
    "telefono": "11  111111111", "movil": "", "email": "brhsadq1q6@m.expediapartnercentral.com",
    "nombre_habitacion": "Deluxe", "arrival": "2026-10-17", "departure": "2026-10-18",
}


class SinMensajeria(unittest.TestCase):
    def test_afiliados_de_expedia_se_detecta(self):
        self.assertIsNotNone(_sin_mensajeria(EXPEDIA_EAN))

    def test_reservas_normales_no(self):
        for referer in ("Expedia", "Booking.com", "", None):
            self.assertIsNone(_sin_mensajeria({"referer": referer}))

    def test_texto_lleva_todos_los_datos(self):
        t = texto_aviso(EXPEDIA_EAN, _sin_mensajeria(EXPEDIA_EAN))
        for dato in ("María de los Ángeles Gómez Manzanero", "Expedia (Expedia Affiliate Network)", "2577279377",
                     "94390525", "brhsadq1q6@m.expediapartnercentral.com", "2026-10-17"):
            self.assertIn(dato, t)
        self.assertIn("ficticio", t)   # el teléfono 11 111111111 no se presenta como real


if __name__ == "__main__":
    unittest.main()

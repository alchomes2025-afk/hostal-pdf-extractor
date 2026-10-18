import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.beds24 import _ref_en_booking

# Reserva real de Beds24 (datos de la de Yuka Tanaka, con el correo recortado)
RESERVA = {"id": 94362309, "masterId": None, "apiReference": "5716229703", "apiSource": "Booking.com", "channel": "booking",
           "status": "new", "firstName": "Yuka", "lastName": "Tanaka", "country": "jp", "roomId": 702399, "propertyId": 339751,
           "email": "ytanak.574351@guest.booking.com", "phone": "", "referer": "Booking.com", "reference": "", "voucher": ""}


class BusquedaPorReferencia(unittest.TestCase):
    def test_el_numero_de_confirmacion_vale(self):
        self.assertEqual(_ref_en_booking(RESERVA, "5716229703"), "apiReference")

    def test_con_la_referencia_en_otro_campo_de_referencia(self):
        self.assertEqual(_ref_en_booking({"guest": {"reference": "ABC123XYZ"}}, "abc123xyz"), "guest.reference")

    def test_los_datos_personales_y_descriptivos_no_valen(self):
        for ref in ("yuka", "tanaka", "new", "booking", "booking.com", "jp", "702399", "339751", "94362309",
                    "ytanak.574351@guest.booking.com"):
            self.assertIsNone(_ref_en_booking(RESERVA, ref), ref)


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import routes.acceso as acceso
from services.acceso_correos import correo_codigos


class EnviarCodigos(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(acceso.acceso_bp)
        self.cliente = app.test_client()
        for nombre, valor in (("API_TOKEN", "secreto"), ("TEST_TOKEN", ""), ("CODIGO_PORTAL", "130773"), ("CODIGO_URBANIZACION", "2308"),
                              ("ROOM_CONFIG", {"702399": {"nombre": "Cala del Moraig", "pin": "213456"},
                                               "720841": {"nombre": "La Casa de la Primavera", "pin": "2486"},
                                               "1": {"nombre": "Sin codigo", "pin": ""}})):
            p = mock.patch.object(acceso, nombre, valor)
            p.start()
            self.addCleanup(p.stop)

    def _post(self, booking, **extra):
        cuerpo = {"token": "secreto", "book_id": 1, "email": "a@b.com", "idioma": "en", **extra}
        with mock.patch.object(acceso, "buscar_booking_por_id", return_value=booking), \
             mock.patch.object(acceso, "enviar_email") as enviar:
            r = self.cliente.post("/enviar-codigos", json=cuerpo)
        return r, enviar

    def test_envia_portal_y_habitacion(self):
        r, enviar = self._post({"id": 5, "roomId": 702399})
        self.assertEqual(r.status_code, 200)
        kw = enviar.call_args.kwargs
        self.assertEqual(kw["to"], "a@b.com")
        self.assertIn("130773#", kw["body"])
        self.assertIn("213456", kw["body"])
        self.assertIn("Cala del Moraig", kw["body"])

    def test_primavera_sin_portal(self):
        _, enviar = self._post({"id": 5, "roomId": 720841})
        cuerpo = enviar.call_args.kwargs["body"]
        self.assertNotIn("130773", cuerpo)
        self.assertIn("2308", cuerpo)    # urbanización
        self.assertIn("2486", cuerpo)    # cajetín de llaves

    def test_token_incorrecto(self):
        r, enviar = self._post({"id": 5, "roomId": 702399}, token="mal")
        self.assertEqual(r.status_code, 401)
        enviar.assert_not_called()

    def test_reserva_inexistente_o_habitacion_sin_codigo(self):
        r, enviar = self._post(None)
        self.assertEqual(r.status_code, 404)
        r, _ = self._post({"id": 5, "roomId": 1})
        self.assertEqual(r.status_code, 409)
        enviar.assert_not_called()

    def test_sin_el_codigo_del_portal_no_se_envia_nada(self):
        with mock.patch.object(acceso, "CODIGO_PORTAL", ""):
            r, enviar = self._post({"id": 5, "roomId": 702399})
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "falta_codigo_portal"))
        enviar.assert_not_called()

    def test_sin_el_codigo_de_la_urbanizacion_no_se_envia_nada_a_la_casa(self):
        with mock.patch.object(acceso, "CODIGO_URBANIZACION", ""):
            r, enviar = self._post({"id": 5, "roomId": 720841})
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "falta_codigo_urbanizacion"))
        enviar.assert_not_called()

    def test_el_codigo_del_portal_no_hace_falta_para_la_casa_ni_el_de_la_urbanizacion_para_el_hostal(self):
        with mock.patch.object(acceso, "CODIGO_PORTAL", ""):
            self.assertEqual(self._post({"id": 5, "roomId": 720841})[0].status_code, 200)
        with mock.patch.object(acceso, "CODIGO_URBANIZACION", ""):
            self.assertEqual(self._post({"id": 5, "roomId": 702399})[0].status_code, 200)

    def test_espanol(self):
        asunto, cuerpo = correo_codigos("es", "URL", "X", "999", "130773")
        self.assertIn("Puerta de la calle", cuerpo)


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import routes.debug_diag as dd
import mobile_routes


def respuesta(datos, ok=True, codigo=200):
    r = mock.Mock()
    r.ok = ok
    r.status_code = codigo
    r.text = "x"
    r.json.return_value = datos
    return r


class DiagCalendario(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(dd.debug_bp)
        self.c = app.test_client()
        for obj, nombre, valor in ((dd, "API_TOKEN", "t"), (dd, "TEST_TOKEN", ""),
                                   (mobile_routes, "get_access_token", mock.Mock(return_value="tok"))):
            p = mock.patch.object(obj, nombre, valor)
            p.start()
            self.addCleanup(p.stop)

    def test_devuelve_rangos_compactos(self):
        datos = {"data": [{"roomId": 720841, "calendar": [{"from": "2026-11-14", "to": "2027-05-31", "numAvail": 0, "price1": 195, "otro": 1},
                                                           {"from": "2027-06-01", "numAvail": 1, "price1": 300}]}]}
        with mock.patch.object(mobile_routes, "b24_get", return_value=respuesta(datos)) as get:
            r = self.c.get("/diag-calendario?token=t&habitacion=720841&desde=2026-11-01&hasta=2027-06-30")
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(j["rangos"][0], {"desde": "2026-11-14", "hasta": "2027-05-31", "numAvail": 0, "price1": 195})
        self.assertEqual(j["rangos"][1]["hasta"], "2027-06-01")
        self.assertEqual(get.call_args.kwargs["params"]["roomId"], "720841")

    def test_sin_token_no_autoriza(self):
        self.assertEqual(self.c.get("/diag-calendario?token=mal").status_code, 401)

    def test_error_de_beds24(self):
        with mock.patch.object(mobile_routes, "b24_get", return_value=respuesta({}, ok=False, codigo=403)):
            self.assertEqual(self.c.get("/diag-calendario?token=t").status_code, 502)


if __name__ == "__main__":
    unittest.main()

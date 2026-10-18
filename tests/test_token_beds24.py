import os
import sys
import threading
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import beds24


def respuesta(codigo=200, token="T1", expira=86400, cabeceras=None):
    r = mock.Mock()
    r.status_code = codigo
    r.headers = cabeceras or {}
    r.json.return_value = {"token": token, "expiresIn": expira}
    r.raise_for_status.side_effect = None if codigo < 400 else requests.HTTPError(f"{codigo}")
    return r


class TokenBeds24(unittest.TestCase):
    def setUp(self):
        beds24._token_cache.update({"token": None, "expira": 0.0})
        for nombre, valor in (("BEDS24_REFRESH_TOKEN", "refresco"),):
            p = mock.patch.object(beds24, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(beds24.time, "sleep")
        self.dormir = p.start()
        self.addCleanup(p.stop)

    def test_se_pide_una_sola_vez_y_luego_sale_de_la_cache(self):
        with mock.patch.object(beds24.requests, "get", return_value=respuesta()) as get:
            self.assertEqual([beds24.get_beds24_access_token() for _ in range(10)], ["T1"] * 10)
        self.assertEqual(get.call_count, 1)

    def test_caduca_y_se_renueva(self):
        with mock.patch.object(beds24.requests, "get", side_effect=[respuesta(token="A"), respuesta(token="B")]) as get:
            self.assertEqual(beds24.get_beds24_access_token(), "A")
            beds24._token_cache["expira"] = 0.0
            self.assertEqual(beds24.get_beds24_access_token(), "B")
        self.assertEqual(get.call_count, 2)

    def test_un_429_se_reintenta_respetando_retry_after(self):
        with mock.patch.object(beds24.requests, "get", side_effect=[respuesta(429, cabeceras={"Retry-After": "2"}), respuesta()]) as get:
            self.assertEqual(beds24.get_beds24_access_token(), "T1")
        self.assertEqual(get.call_count, 2)
        self.dormir.assert_called_once_with(2.0)

    def test_retry_after_enorme_se_acota(self):
        with mock.patch.object(beds24.requests, "get", side_effect=[respuesta(429, cabeceras={"Retry-After": "600"}), respuesta()]):
            beds24.get_beds24_access_token()
        self.dormir.assert_called_once_with(beds24.ESPERA_MAX_429)

    def test_si_siempre_falla_lanza_el_error_y_no_guarda_nada(self):
        with mock.patch.object(beds24.requests, "get", return_value=respuesta(429)) as get:
            with self.assertRaises(requests.HTTPError):
                beds24.get_beds24_access_token()
        self.assertEqual(get.call_count, beds24.REINTENTOS_TOKEN)
        self.assertIsNone(beds24._token_cache["token"])

    def test_peticiones_simultaneas_con_la_cache_vacia_piden_un_solo_token(self):
        with mock.patch.object(beds24.requests, "get", return_value=respuesta()) as get:
            hilos = [threading.Thread(target=beds24.get_beds24_access_token) for _ in range(12)]
            [h.start() for h in hilos]
            [h.join() for h in hilos]
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()

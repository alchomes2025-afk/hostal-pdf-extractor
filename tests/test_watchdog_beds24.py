import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.watchdog as wd


def http_error(codigo):
    r = mock.Mock()
    r.status_code = codigo
    return requests.HTTPError(f"{codigo}", response=r)


def respuesta_ok(n=2):
    r = mock.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"data": [{}] * n}
    return r


class ChequeoBeds24(unittest.TestCase):
    def setUp(self):
        for obj, nombre, valor in ((wd.time, "sleep", mock.Mock()),
                                   (wd, "get_beds24_access_token", mock.Mock(return_value="tok"))):
            p = mock.patch.object(obj, nombre, valor)
            self.__dict__.setdefault("mocks", {})[nombre] = p.start()
            self.addCleanup(p.stop)

    def test_funciona_a_la_primera(self):
        with mock.patch.object(wd.requests, "get", return_value=respuesta_ok(3)) as get:
            res, problema = wd._comprobar_beds24()
        self.assertEqual((res["ok"], res["reservas_hoy_mañana"], res["intentos"], problema), (True, 3, 1, None))
        self.assertEqual(get.call_count, 1)

    def test_un_timeout_puntual_se_reintenta_y_no_avisa(self):
        # el caso del 2026-10-21 12:30: Beds24 no contestó a tiempo una vez
        with mock.patch.object(wd.requests, "get", side_effect=[requests.ConnectTimeout("timed out"), respuesta_ok()]):
            res, problema = wd._comprobar_beds24()
        self.assertTrue(res["ok"])
        self.assertIsNone(problema)
        self.assertEqual(res["intentos"], 2)

    def test_si_sigue_sin_responder_es_aviso_fijo_no_critico_ni_con_el_error(self):
        errores = [requests.ConnectTimeout("Connection ... at 0x77ec5260d6d0"), requests.ConnectTimeout("Connection ... at 0x1234")]
        textos = set()
        for _ in range(2):
            with mock.patch.object(wd.requests, "get", side_effect=list(errores)):
                res, problema = wd._comprobar_beds24()
            self.assertFalse(res["ok"])
            nivel, descripcion, accion = problema
            self.assertEqual(nivel, "warning")
            self.assertNotIn("0x", descripcion)
            self.assertNotIn("refresh", descripcion.lower())
            textos.add(descripcion)
        self.assertEqual(len(textos), 1)   # misma descripción → misma firma de deduplicación

    def test_error_de_servidor_5xx_tambien_es_sin_respuesta(self):
        r = mock.Mock()
        r.raise_for_status.side_effect = http_error(503)
        with mock.patch.object(wd.requests, "get", return_value=r):
            _, problema = wd._comprobar_beds24()
        self.assertEqual(problema, wd._BEDS24_SIN_RESPUESTA)

    def test_credenciales_rechazadas_es_critico_y_descarta_el_token_guardado(self):
        r = mock.Mock()
        r.raise_for_status.side_effect = http_error(401)
        wd._token_cache_beds24.update({"token": "viejo", "expira": 1e12})
        with mock.patch.object(wd.requests, "get", return_value=r):
            res, problema = wd._comprobar_beds24()
        self.assertEqual(problema, wd._BEDS24_CREDENCIAL)
        self.assertEqual(problema[0], "critico")
        self.assertIsNone(wd._token_cache_beds24["token"])

    def test_un_token_guardado_anulado_se_recupera_con_el_reintento(self):
        r401 = mock.Mock()
        r401.raise_for_status.side_effect = http_error(401)
        with mock.patch.object(wd.requests, "get", side_effect=[r401, respuesta_ok()]):
            res, problema = wd._comprobar_beds24()
        self.assertTrue(res["ok"])
        self.assertIsNone(problema)

    def test_limite_429(self):
        r = mock.Mock()
        r.raise_for_status.side_effect = http_error(429)
        with mock.patch.object(wd.requests, "get", return_value=r):
            _, problema = wd._comprobar_beds24()
        self.assertEqual(problema, wd._BEDS24_LIMITADO)


if __name__ == "__main__":
    unittest.main()

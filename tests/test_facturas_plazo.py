"""Leer una factura nunca pasa del presupuesto de tiempo (gunicorn mata a los 60 s): la app no se queda «colgada»."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import facturas, groq_claves, puente_plataforma


class Plazo(unittest.TestCase):
    def setUp(self):
        for objeto, nombre, valor in ((groq_claves, "GROQ_API_KEY_GESTION", "k"), (facturas, "clave_gestion", lambda: "k"), (facturas, "_modelo_vision", lambda: "")):
            p = mock.patch.object(objeto, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: setattr(facturas._plazo, "fin", None))

    def test_sin_tiempo_no_se_llama_a_la_ia(self):
        facturas._plazo.fin = time.time() + 3
        with mock.patch.object(facturas.requests, "post") as post:
            with self.assertRaises(facturas.FacturaError) as e:
                facturas._preguntar_ia("x")
        self.assertEqual(e.exception.status, 504)
        post.assert_not_called()

    def test_el_tiempo_de_espera_a_groq_se_ajusta_a_lo_que_queda(self):
        facturas._plazo.fin = time.time() + 20
        visto = {}

        def post(url, headers=None, json=None, timeout=None):
            visto["timeout"] = timeout
            visto["max_tokens"] = json["max_tokens"]
            return mock.Mock(status_code=200, raise_for_status=lambda: None,
                             json=lambda: {"choices": [{"message": {"content": '{"tipo": "gasto"}'}}]})
        with mock.patch.object(facturas.requests, "post", post):
            self.assertEqual(facturas._preguntar_ia("x"), {"tipo": "gasto"})
        self.assertLessEqual(visto["timeout"], 18)             # nunca más de lo que queda (menos un margen)
        self.assertGreaterEqual(visto["max_tokens"], 2000)           # margen para el razonamiento de gpt-oss

    def test_ocr_con_poco_tiempo_falla_rapido_y_con_tiempo_lleva_su_limite(self):
        facturas._plazo.fin = time.time() + 8
        with mock.patch.object(puente_plataforma, "ocr") as ocr:
            with self.assertRaises(facturas.FacturaError) as e:
                facturas.texto_de(b"x", "image/jpeg")
        self.assertEqual(e.exception.status, 504)
        ocr.assert_not_called()
        facturas._plazo.fin = time.time() + 50
        with mock.patch.object(puente_plataforma, "ocr", return_value="texto") as ocr:
            self.assertEqual(facturas.texto_de(b"x", "image/jpeg"), "texto")
        self.assertLessEqual(ocr.call_args.kwargs["timeout"], 30)

    def test_leer_fija_y_limpia_el_plazo(self):
        visto = {}

        def texto(contenido, mime):
            visto["plazo"] = facturas._restante()
            return "FACTURA 14/10/2026 total 10"
        with mock.patch.object(facturas, "texto_de", texto),              mock.patch.object(facturas, "comprobar_propiedad", lambda *a: "349341"),              mock.patch.object(facturas, "_archivo", lambda a: (b"x", "image/jpeg")),              mock.patch.object(facturas, "ya_guardada", lambda *a: None),              mock.patch.object(facturas, "_preguntar_ia", lambda p: {"tipo": "gasto", "proveedor": "X", "fecha": "2026-10-14", "total": 10}):
            facturas.leer("alc-homes", "349341", {}, True)
        self.assertTrue(40 < visto["plazo"] <= facturas.PRESUPUESTO_LEER_SEG)
        self.assertIsNone(facturas._plazo.fin)                             # no queda un plazo viejo para la siguiente petición


if __name__ == "__main__":
    unittest.main()

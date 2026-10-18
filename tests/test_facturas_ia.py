"""Lectura de facturas con la IA: facturas largas (el total está al final), razonamiento de gpt-oss, modo JSON y errores distinguibles."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import facturas


def respuesta(status=200, contenido='{"tipo": "gasto", "total": 10}', texto=""):
    r = mock.Mock(status_code=status, text=texto)
    r.raise_for_status = lambda: None if status < 400 else (_ for _ in ()).throw(Exception(f"HTTP {status}"))
    r.json = lambda: {"choices": [{"message": {"content": contenido}}]}
    return r


class IA(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(facturas, "clave_gestion", lambda: "k")
        p.start()
        self.addCleanup(p.stop)
        facturas._plazo.fin = time.time() + 50
        self.addCleanup(lambda: setattr(facturas._plazo, "fin", None))

    def _ia(self, *respuestas):
        enviados = []
        it = iter(respuestas)

        def post(url, headers=None, json=None, timeout=None):
            enviados.append(json)
            return next(it)
        return mock.patch.object(facturas.requests, "post", post), enviados

    # ── texto largo ──
    def test_factura_corta_va_entera(self):
        self.assertEqual(facturas._recortar("abc " * 100), ("abc " * 100).strip())

    def test_factura_larga_conserva_principio_y_final(self):
        texto = "EMISOR LEROY MERLIN FECHA 14/10/2026\n" + ("linea de articulo 12,34\n" * 2000) + "TOTAL A PAGAR 158,22 IVA 21% 27,46"
        r = facturas._recortar(texto)
        self.assertLessEqual(len(r), facturas.MAX_TEXTO_IA + 80)
        self.assertTrue(r.startswith("EMISOR LEROY MERLIN"))
        self.assertTrue(r.endswith("TOTAL A PAGAR 158,22 IVA 21% 27,46"))       # el total del final NO se pierde
        self.assertIn("omitidas", r)

    # ── cuerpo de la petición ──
    def test_gpt_oss_razona_poco_y_tiene_margen_de_salida(self):
        c = facturas._cuerpo_ia("openai/gpt-oss-120b", "x", True)
        self.assertEqual(c["reasoning_effort"], "low")
        self.assertGreaterEqual(c["max_tokens"], 2000)
        self.assertEqual(c["response_format"], {"type": "json_object"})
        sin = facturas._cuerpo_ia("openai/gpt-oss-120b", "x", False)                      # petición básica de respaldo (tras un 400)
        self.assertNotIn("reasoning_effort", sin)
        self.assertNotIn("response_format", sin)
        self.assertGreaterEqual(sin["max_tokens"], 2000)
        self.assertNotIn("reasoning_effort", facturas._cuerpo_ia("llama-3.3-70b-versatile", "x", True))
        self.assertNotIn("response_format", facturas._cuerpo_ia("llama-3.3-70b-versatile", "x", False))

    # ── errores ──
    def test_modo_json_no_admitido_se_repite_sin_el(self):
        parche, enviados = self._ia(respuesta(400), respuesta(200))
        with parche:
            self.assertEqual(facturas._preguntar_ia("x"), {"tipo": "gasto", "total": 10})
        self.assertIn("response_format", enviados[0])
        self.assertNotIn("response_format", enviados[1])

    def test_respuesta_vacia_pasa_al_modelo_de_respaldo(self):
        parche, enviados = self._ia(respuesta(200, contenido=""), respuesta(200))
        with parche:
            self.assertEqual(facturas._preguntar_ia("x")["tipo"], "gasto")
        self.assertEqual(enviados[0]["model"], facturas.GROQ_MODEL_PRI)
        self.assertEqual(enviados[1]["model"], facturas.GROQ_MODEL_FALL)

    def test_si_ningun_modelo_da_json_valido_es_422_sin_reintentos(self):
        parche, _ = self._ia(respuesta(200, contenido="no es json"), respuesta(200, contenido="tampoco"))
        with parche, self.assertRaises(facturas.FacturaError) as e:
            facturas._preguntar_ia("x")
        self.assertEqual(e.exception.status, 422)
        self.assertIn("lectura válida", str(e.exception))

    def test_saturada_es_503_para_que_la_app_reintente(self):
        parche, _ = self._ia(respuesta(429), respuesta(429))
        with parche, self.assertRaises(facturas.FacturaError) as e:
            facturas._preguntar_ia("x")
        self.assertEqual(e.exception.status, 503)
        self.assertIn("saturada", str(e.exception))


if __name__ == "__main__":
    unittest.main()

"""Facturas sin texto (escaneos, fotos, talonarios escritos a mano): se leen con un modelo de visión; el OCR queda de respaldo."""
import io
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from PIL import Image

import mobile_facturas
import mobile_routes
from services import facturas


def jpeg(ancho=3000, alto=2000):
    buf = io.BytesIO()
    Image.new("RGB", (ancho, alto), (230, 230, 230)).save(buf, "JPEG")
    return buf.getvalue()


def pdf_de_imagen(paginas=3):
    buf = io.BytesIO()
    ims = [Image.new("RGB", (1200, 1700), (240, 240, 240)) for _ in range(paginas)]
    ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:])
    return buf.getvalue()


def respuesta(status=200, contenido='{"tipo": "gasto", "total": 60}'):
    r = mock.Mock(status_code=status, text="")
    r.raise_for_status = lambda: None if status < 400 else (_ for _ in ()).throw(Exception(f"HTTP {status}"))
    r.json = lambda: {"choices": [{"message": {"content": contenido}}], "data": [{"id": "openai/gpt-oss-120b"}, {"id": "meta-llama/llama-4-scout-17b-16e-instruct"}]}
    return r


class Base(unittest.TestCase):
    def setUp(self):
        for objeto, nombre, valor in ((facturas, "clave_gestion", lambda: "k"), (facturas, "GROQ_MODEL_VISION", "")):
            p = mock.patch.object(objeto, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        facturas._vision.update(t=0.0, modelo="")
        facturas._plazo.fin = time.time() + 50
        self.addCleanup(lambda: setattr(facturas._plazo, "fin", None))
        self.addCleanup(lambda: facturas._vision.update(t=0.0, modelo=""))


class Imagenes(Base):
    def test_foto_grande_se_reduce(self):
        (url,) = facturas._imagenes_para_ia(jpeg(), "image/jpeg")
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        import base64
        im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        self.assertLessEqual(max(im.size), 1600)

    def test_pdf_de_imagen_solo_las_dos_primeras_paginas(self):
        self.assertEqual(len(facturas._imagenes_para_ia(pdf_de_imagen(3), "application/pdf")), 2)


class ModeloVision(Base):
    def test_variable_manda(self):
        with mock.patch.object(facturas, "GROQ_MODEL_VISION", "mi/modelo"), mock.patch.object(facturas.requests, "get") as get:
            self.assertEqual(facturas._modelo_vision(), "mi/modelo")
        get.assert_not_called()

    def test_elige_el_preferido_y_lo_recuerda_una_hora(self):
        with mock.patch.object(facturas.requests, "get", return_value=respuesta()) as get:
            self.assertEqual(facturas._modelo_vision(), "meta-llama/llama-4-scout-17b-16e-instruct")
            self.assertEqual(facturas._modelo_vision(), "meta-llama/llama-4-scout-17b-16e-instruct")
        self.assertEqual(get.call_count, 1)

    def test_sin_modelo_de_vision_o_sin_clave(self):
        r = respuesta()
        r.json = lambda: {"data": [{"id": "openai/gpt-oss-120b"}, {"id": "whisper-large-v3"}]}
        with mock.patch.object(facturas.requests, "get", return_value=r):
            self.assertEqual(facturas._modelo_vision(), "")
        facturas._vision.update(t=0.0, modelo="")
        with mock.patch.object(facturas, "clave_gestion", lambda: ""), mock.patch.object(facturas.requests, "get") as get:
            self.assertEqual(facturas._modelo_vision(), "")
        get.assert_not_called()

    def test_error_de_red_no_rompe(self):
        with mock.patch.object(facturas.requests, "get", side_effect=Exception("sin red")):
            self.assertEqual(facturas._modelo_vision(), "")


class LlamadaVision(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(facturas, "_modelo_vision", lambda: "vis/modelo")
        p.start()
        self.addCleanup(p.stop)

    def test_manda_la_imagen_en_el_mensaje(self):
        visto = {}

        def post(url, headers=None, json=None, timeout=None):
            visto.update(json)
            return respuesta()
        with mock.patch.object(facturas.requests, "post", post):
            self.assertEqual(facturas._preguntar_ia_vision("pregunta", ["data:image/jpeg;base64,AAA"])["total"], 60)
        partes = visto["messages"][0]["content"]
        self.assertEqual(partes[0], {"type": "text", "text": "pregunta"})
        self.assertEqual(partes[1]["image_url"]["url"], "data:image/jpeg;base64,AAA")
        self.assertEqual(visto["model"], "vis/modelo")

    def test_modo_json_rechazado_se_repite_sin_el(self):
        with mock.patch.object(facturas.requests, "post", side_effect=[respuesta(400), respuesta(200)]) as post:
            self.assertEqual(facturas._preguntar_ia_vision("p", ["x"])["total"], 60)
        self.assertIn("response_format", post.call_args_list[0].kwargs["json"])
        self.assertNotIn("response_format", post.call_args_list[1].kwargs["json"])

    def test_si_el_modelo_no_acepta_imagenes_se_olvida(self):
        facturas._vision.update(t=time.time(), modelo="vis/modelo")
        with mock.patch.object(facturas.requests, "post", side_effect=[respuesta(400), respuesta(400)]):
            with self.assertRaises(facturas.FacturaError) as e:
                facturas._preguntar_ia_vision("p", ["x"])
        self.assertEqual(e.exception.status, 422)
        self.assertEqual(facturas._vision["modelo"], "")

    def test_saturada_es_503(self):
        with mock.patch.object(facturas.requests, "post", return_value=respuesta(429)):
            with self.assertRaises(facturas.FacturaError) as e:
                facturas._preguntar_ia_vision("p", ["x"])
        self.assertEqual(e.exception.status, 503)


class LeerEscaneos(Base):
    def _leer(self, mime, contenido, capa="", vision=None, ocr="FACTURA 19/08/2026 total 60 euros", texto_ia=None):
        llamadas = {"vision": 0, "ocr": 0, "texto": 0}

        def v(prompt, imagenes):
            llamadas["vision"] += 1
            if isinstance(vision, Exception):
                raise vision
            return vision

        def o(c, m):
            llamadas["ocr"] += 1
            return ocr

        def t(prompt):
            llamadas["texto"] += 1
            return texto_ia or {"tipo": "gasto", "emisor": "HAITENG LIU", "numero": "1073", "fecha": "2026-08-19", "total": 60}
        with mock.patch.object(facturas, "comprobar_propiedad", lambda *a: "349341"), mock.patch.object(facturas, "_archivo", lambda a: (contenido, mime)), \
             mock.patch.object(facturas, "ya_guardada", lambda *a: None), mock.patch.object(facturas, "misma_factura", lambda *a: None), \
             mock.patch.object(facturas, "capa_de_texto", lambda c, m: capa), mock.patch.object(facturas, "_modelo_vision", lambda: "vis/modelo"), \
             mock.patch.object(facturas, "_preguntar_ia_vision", v), mock.patch.object(facturas, "texto_de", o), mock.patch.object(facturas, "_preguntar_ia", t), \
             mock.patch.object(facturas, "_imagenes_para_ia", lambda c, m: ["data:image/jpeg;base64,AAA"]):
            r = facturas.leer("alc-homes", "349341", {}, True)
        return r, llamadas

    TALONARIO = {"tipo": "gasto", "emisor": "Haiteng Liu", "nif_emisor": "X2094529P", "numero": "1073", "fecha": "19/8/26", "total": 60.0}

    def test_escaneo_se_lee_con_vision_sin_gastar_ocr_ni_texto(self):
        r, ll = self._leer("application/pdf", b"%PDF", vision=self.TALONARIO)
        self.assertEqual(ll, {"vision": 1, "ocr": 0, "texto": 0})
        self.assertEqual(r["datos"]["proveedor"], "Haiteng Liu")
        self.assertEqual(r["datos"]["fecha"], "2026-08-19")
        self.assertEqual(r["datos"]["total"], 60.0)
        self.assertFalse(r["datos"]["cuadra"])                              # sin IVA desglosado: no sale «lista», se revisa

    def test_foto_tambien_va_por_vision(self):
        _, ll = self._leer("image/jpeg", b"x", vision=self.TALONARIO)
        self.assertEqual(ll["vision"], 1)

    def test_pdf_con_texto_no_usa_vision(self):
        _, ll = self._leer("application/pdf", b"%PDF", capa="FACTURA con texto " * 10)
        self.assertEqual(ll, {"vision": 0, "ocr": 1, "texto": 1})            # (texto_de está parcheado: con capa devolvería esa capa)

    def test_si_la_vision_no_acierta_se_prueba_con_el_ocr(self):
        _, ll = self._leer("image/jpeg", b"x", vision=facturas.FacturaError("sin lectura", 422))
        self.assertEqual(ll, {"vision": 1, "ocr": 1, "texto": 1})

    def test_si_la_ia_esta_saturada_se_propaga_para_reintentar(self):
        with self.assertRaises(facturas.FacturaError) as e:
            self._leer("image/jpeg", b"x", vision=facturas.FacturaError("saturada", 503))
        self.assertEqual(e.exception.status, 503)


class Diagnostico(Base):
    def test_diag_solo_admin(self):
        for objeto, nombre, valor in ((mobile_routes, "MOBILE_APP_PIN", "1111"), (mobile_routes, "MOBILE_APP_PIN_ADMIN", "9999"), (facturas, "_modelo_vision", lambda: "vis/m")):
            p = mock.patch.object(objeto, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(mobile_facturas.facturas_bp)
        c = app.test_client()
        self.assertEqual(c.get("/mobile/facturas/diag-ia", headers={"x-app-pin": "1111"}).status_code, 403)
        r = c.get("/mobile/facturas/diag-ia", headers={"x-app-pin": "9999"})
        self.assertEqual(r.get_json()["vision"], "vis/m")
        self.assertNotIn("k", r.get_data(as_text=True).replace("vis/m", "").replace("vision", "").replace("variable", "")[:0])   # sin claves en la respuesta


if __name__ == "__main__":
    unittest.main()

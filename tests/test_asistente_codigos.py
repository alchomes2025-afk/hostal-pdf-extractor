"""Asistente de la app: correo de emergencia con los códigos de acceso (borrador + botón de confirmar, solo PIN admin)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_asistente as asistente
import mobile_routes
import routes.acceso as acceso
from services import groq_claves, limites

HABITACIONES = {"702399": {"nombre": "Cala del Moraig", "pin": "213456"},
                "720841": {"nombre": "La Casa de la Primavera", "pin": "2486"},
                "1": {"nombre": "Sin codigo", "pin": ""}}
BOOKING = {"id": 5, "roomId": 702399, "firstName": "Ana", "lastName": "Pérez", "arrival": "2026-10-17", "departure": "2026-10-19"}
ADMIN, NORMAL = {"es_admin": True, "finanzas": True}, {"es_admin": False, "finanzas": True}


def _ctx(base):
    return {**base, "graficos": [], "borradores": []}


class Base(unittest.TestCase):
    def setUp(self):
        for objeto, nombre, valor in ((acceso, "ROOM_CONFIG", HABITACIONES),
                                      (asistente, "buscar_booking_por_id", mock.Mock(return_value=dict(BOOKING)))):
            p = mock.patch.object(objeto, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        limites._eventos.clear()


class Herramienta(Base):
    def _preparar(self, base=ADMIN, **args):
        ctx = _ctx(base)
        args = {"reserva": "5", "email": "huesped@gmail.com", "idioma": "es", **args}
        return asistente.ejecutar_herramienta("preparar_correo_codigos", json.dumps(args), ctx), ctx

    def test_admin_prepara_borrador_sin_enviar(self):
        with mock.patch("services.email_send.enviar_email") as red, mock.patch("services.email_send.requests.post") as red2:
            res, ctx = self._preparar()
        red.assert_not_called()                                   # preparar nunca envía
        red2.assert_not_called()
        self.assertTrue(res["borrador_preparado"])
        self.assertIn("NO se ha enviado", res["nota"])
        (b,) = ctx["borradores"]
        self.assertEqual((b["email"], b["reserva"], b["idioma"]), ("huesped@gmail.com", "5", "es"))
        self.assertIn("130773#", b["cuerpo"])
        self.assertIn("213456", b["cuerpo"])
        self.assertEqual(b["huesped"], "Ana Pérez")

    def test_primavera_lleva_urbanizacion_y_sin_portal(self):
        asistente.buscar_booking_por_id.return_value = {**BOOKING, "roomId": 720841}
        _, ctx = self._preparar(idioma="en")
        cuerpo = ctx["borradores"][0]["cuerpo"]
        self.assertIn("2308", cuerpo)
        self.assertNotIn("130773", cuerpo)

    def test_sin_pin_admin_primero_se_pide_el_pin_y_no_se_elabora_borrador(self):
        res, ctx = self._preparar(NORMAL)
        self.assertTrue(res["pin_solicitado"])
        self.assertNotIn("borrador_preparado", res)
        (b,) = ctx["borradores"]
        self.assertEqual(b, {"solicitud_pin": True, "reserva": "5", "email": "huesped@gmail.com", "idioma": "es"})
        asistente.buscar_booking_por_id.assert_not_called()       # ni siquiera se consulta la reserva antes del PIN
        self.assertNotIn("213456", json.dumps(res))               # ni el modelo ni la respuesta llevan los códigos
        self.assertNotIn("1432", json.dumps(res))                 # y el modelo no recibe el PIN
        self.assertIn("Introduce el PIN de seguridad", res["nota"])

    def test_sin_pin_admin_tambien_se_validan_los_datos(self):
        res, ctx = self._preparar(NORMAL, email="mal")
        self.assertIn("no es válido", res["error"])
        self.assertEqual(ctx["borradores"], [])

    def test_el_admin_obtiene_el_pin_de_envio_y_el_resto_no(self):
        res = asistente.ejecutar_herramienta("pin_envio_codigos", "{}", _ctx(ADMIN))
        self.assertEqual(res["pin_envio_codigos"], "1432")
        res = asistente.ejecutar_herramienta("pin_envio_codigos", "{}", _ctx(NORMAL))
        self.assertIn("error", res)
        self.assertNotIn("1432", json.dumps(res))

    def test_validaciones(self):
        for args, texto in (({"reserva": "abc"}, "número de reserva"), ({"email": "no-es-un-correo"}, "no es válido"),
                            ({"email": "x@guest.booking.com"}, "Booking/Airbnb")):
            res, ctx = self._preparar(**args)
            self.assertIn(texto, res["error"])
            self.assertEqual(ctx["borradores"], [])
        asistente.buscar_booking_por_id.return_value = None
        self.assertIn("No existe", self._preparar()[0]["error"])
        asistente.buscar_booking_por_id.return_value = {**BOOKING, "roomId": 1}
        self.assertIn("no tiene código", self._preparar()[0]["error"])

    def test_maximo_de_borradores(self):
        ctx = _ctx(ADMIN)
        args = json.dumps({"reserva": "5", "email": "a@b.com"})
        for _ in range(asistente.MAX_BORRADORES):
            self.assertTrue(asistente.ejecutar_herramienta("preparar_correo_codigos", args, ctx)["borrador_preparado"])
        self.assertIn("borradores", asistente.ejecutar_herramienta("preparar_correo_codigos", args, ctx)["error"])

    def test_herramientas_segun_el_pin(self):
        nombres = lambda ctx: [t["function"]["name"] for t in asistente.herramientas_para(ctx)]
        self.assertIn("preparar_correo_codigos", nombres(ADMIN))
        self.assertIn("preparar_correo_codigos", nombres(NORMAL))       # el borrador lo puede preparar cualquiera...
        self.assertIn("pin_envio_codigos", nombres(ADMIN))              # ...pero solo el administrador puede pedir el PIN de envío
        self.assertNotIn("pin_envio_codigos", nombres(NORMAL))


class ReglaSiete(Base):
    def _system(self, base):
        with mock.patch.object(asistente, "_habitaciones", return_value=[]), mock.patch.object(asistente, "_mr"):
            return asistente._system(_ctx(base))

    def test_admin_ve_la_excepcion(self):
        texto = self._system(ADMIN)
        self.assertIn("7. Solo lees datos", texto)
        self.assertIn("preparar_correo_codigos", texto)
        self.assertIn("nunca digas que lo has enviado", texto)

    def test_resto_envia_con_pin_de_seguridad(self):
        texto = self._system(NORMAL)
        self.assertIn("7. Solo lees datos", texto)
        self.assertIn("preparar_correo_codigos", texto)
        self.assertIn("«Introduce el PIN de seguridad.»", texto)
        self.assertNotIn("pin_envio_codigos", texto)        # no sabe cómo conseguir el PIN
        self.assertNotIn("1432", texto)

    def test_admin_puede_dar_el_pin(self):
        texto = self._system(ADMIN)
        self.assertIn("pin_envio_codigos", texto)
        self.assertNotIn("1432", texto)                     # el PIN no va en el prompt: sale de la herramienta


class Conversacion(Base):
    def _groq(self, mensajes):
        respuestas = iter(mensajes)
        enviados = []

        def post(url, headers=None, json=None, timeout=None):
            enviados.append(json)
            r = mock.Mock(status_code=200, text="")
            r.json.return_value = {"choices": [{"message": next(respuestas)}]}
            return r
        return post, enviados

    def _responder(self, base):
        llamada = {"content": "", "tool_calls": [{"id": "1", "function": {"name": "preparar_correo_codigos",
                   "arguments": json.dumps({"reserva": "5", "email": "huesped@gmail.com", "idioma": "es"})}}]}
        post, enviados = self._groq([llamada, {"content": "Borrador listo: revísalo y pulsa Enviar."}])
        with mock.patch.object(asistente.requests, "post", post), mock.patch.object(asistente, "_habitaciones", return_value=[]), \
             mock.patch.object(asistente, "_mr"), mock.patch("services.email_send.enviar_email") as red:
            res, status = asistente.responder([{"role": "user", "content": "manda los códigos de la reserva 5 a huesped@gmail.com"}], base)
        return res, status, enviados, red

    def test_admin_recibe_borrador_y_no_se_envia_nada(self):
        res, status, enviados, red = self._responder(ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(len(res["borradores"]), 1)
        red.assert_not_called()
        self.assertIn("preparar_correo_codigos", [t["function"]["name"] for t in enviados[0]["tools"]])

    def test_sin_pin_admin_llega_la_solicitud_del_pin_y_no_un_borrador(self):
        res, status, enviados, red = self._responder(NORMAL)
        self.assertEqual(status, 200)
        (b,) = res["borradores"]
        self.assertTrue(b["solicitud_pin"])
        self.assertNotIn("cuerpo", b)
        nombres = [t["function"]["name"] for t in enviados[0]["tools"]]
        self.assertIn("preparar_correo_codigos", nombres)
        self.assertNotIn("pin_envio_codigos", nombres)
        for peticion in enviados:                            # lo que se manda a Groq no lleva ni los códigos ni el PIN de envío
            self.assertNotIn("213456", json.dumps(peticion))
            self.assertNotIn("1432", json.dumps(peticion))
        red.assert_not_called()


class ClaveGroq(unittest.TestCase):
    """Cada parte usa su cuenta de Groq: gestión (asistente y facturas) ≠ huéspedes ≠ interna de HostalFácil."""
    def _claves(self, gestion="", interna="", general=""):
        return (mock.patch.object(groq_claves, "GROQ_API_KEY_GESTION", gestion), mock.patch.object(groq_claves, "GROQ_API_KEY_INTERNA", interna),
                mock.patch.object(groq_claves, "GROQ_API_KEY", general))

    def _con(self, **claves):
        ps = self._claves(**claves)
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

    def test_gestion_manda(self):
        self._con(gestion="gestion", interna="interna", general="huespedes")
        self.assertEqual(asistente.clave_groq(), "gestion")

    def test_respaldo_temporal_sin_gestion_usa_la_interna_y_nunca_antes_la_de_huespedes(self):
        self._con(interna="interna", general="huespedes")
        self.assertEqual(asistente.clave_groq(), "interna")

    def test_ultimo_recurso_la_general(self):
        self._con(general="huespedes")
        self.assertEqual(asistente.clave_groq(), "huespedes")

    def test_la_llamada_a_groq_lleva_la_clave_de_gestion(self):
        self._con(gestion="gestion", interna="interna", general="huespedes")
        visto = {}

        def post(url, headers=None, json=None, timeout=None):
            visto.update(headers)
            return mock.Mock(status_code=200, text="", json=lambda: {"choices": [{"message": {"content": "hola"}}]})
        with mock.patch.object(asistente.requests, "post", post),              mock.patch.object(asistente, "_habitaciones", return_value=[]), mock.patch.object(asistente, "_mr"):
            res, _ = asistente.responder([{"role": "user", "content": "hola"}], NORMAL)
        self.assertTrue(res["ok"])
        self.assertEqual(visto["Authorization"], "Bearer gestion")


class BotonEnviar(Base):
    PIN, PIN_ADMIN = "1111", "9999"

    def setUp(self):
        super().setUp()
        app = Flask(__name__)
        app.register_blueprint(mobile_routes.mobile_bp, url_prefix="/mobile")
        self.cliente = app.test_client()
        for objeto, nombre, valor in ((mobile_routes, "MOBILE_APP_PIN", self.PIN), (mobile_routes, "MOBILE_APP_PIN_ADMIN", self.PIN_ADMIN),
                                      (mobile_routes, "log_action", mock.Mock())):
            p = mock.patch.object(objeto, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("services.email_send.enviar_email")
        self.enviar = p.start()
        self.addCleanup(p.stop)

    def _post(self, pin, **extra):
        cuerpo = {"reserva": "5", "email": "huesped@gmail.com", "idioma": "es", **extra}
        return self.cliente.post("/mobile/assistant/enviar-codigos", json=cuerpo, headers={"x-app-pin": pin} if pin else {})

    def test_con_pin_admin_envia_lo_mismo_que_el_borrador(self):
        r = self._post(self.PIN_ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        kw = self.enviar.call_args.kwargs
        _, ctx = Herramienta._preparar(self)
        borrador = ctx["borradores"][0]
        self.assertEqual((kw["to"], kw["subject"], kw["body"]), (borrador["email"], borrador["asunto"], borrador["cuerpo"]))
        mobile_routes.log_action.assert_called_once()

    def test_sin_pin_de_la_app_no_envia(self):
        for pin in ("0000", None):
            self.assertEqual(self._post(pin, pin_envio="1432").status_code, 401)
        self.enviar.assert_not_called()

    def test_pin_normal_exige_el_pin_de_envio(self):
        self.assertEqual(self._post(self.PIN).status_code, 403)                       # sin PIN de envío
        self.assertEqual(self._post(self.PIN, pin_envio="0000").status_code, 403)      # PIN de envío incorrecto
        self.assertEqual(self._post(self.PIN, pin_envio=self.PIN_ADMIN).status_code, 403)   # el PIN de administrador no vale como PIN de envío
        self.enviar.assert_not_called()
        r = self._post(self.PIN, pin_envio="1432")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.enviar.call_count, 1)
        self.assertEqual(mobile_routes.log_action.call_args.args[3]["con"], "pin_envio")

    def _preparar_codigos(self, pin, **extra):
        cuerpo = {"reserva": "5", "email": "huesped@gmail.com", "idioma": "es", **extra}
        return self.cliente.post("/mobile/assistant/preparar-codigos", json=cuerpo, headers={"x-app-pin": pin} if pin else {})

    def test_preparar_codigos_solo_devuelve_el_borrador_con_el_pin_de_seguridad(self):
        self.assertEqual(self._preparar_codigos(None, pin_envio="1432").status_code, 401)
        for extra in ({}, {"pin_envio": "0000"}, {"pin_envio": self.PIN_ADMIN}):
            r = self._preparar_codigos(self.PIN, **extra)
            self.assertEqual(r.status_code, 403)
            self.assertNotIn("213456", r.get_data(as_text=True))                       # nada de códigos con un PIN malo
        asistente.buscar_booking_por_id.assert_not_called()
        r = self._preparar_codigos(self.PIN, pin_envio="1432")
        self.assertEqual(r.status_code, 200)
        b = r.get_json()["borrador"]
        self.assertIn("213456", b["cuerpo"])
        self.assertEqual(b["email"], "huesped@gmail.com")
        self.enviar.assert_not_called()                                                # preparar nunca envía

    def test_preparar_codigos_el_admin_no_necesita_pin_de_seguridad(self):
        self.assertEqual(self._preparar_codigos(self.PIN_ADMIN).status_code, 200)

    def test_preparar_codigos_datos_malos(self):
        self.assertEqual(self._preparar_codigos(self.PIN, pin_envio="1432", email="mal").status_code, 400)

    def test_los_fallos_de_preparar_y_de_enviar_cuentan_juntos(self):
        for _ in range(3):
            self._preparar_codigos(self.PIN, pin_envio="0000")
        for _ in range(2):
            self._post(self.PIN, pin_envio="0000")
        self.assertEqual(self._preparar_codigos(self.PIN, pin_envio="1432").status_code, 429)

    def test_cinco_pin_de_envio_incorrectos_bloquean(self):
        for _ in range(5):
            self.assertEqual(self._post(self.PIN, pin_envio="0000").status_code, 403)
        self.assertEqual(self._post(self.PIN, pin_envio="1432").status_code, 429)     # ni el correcto pasa durante el bloqueo
        self.enviar.assert_not_called()
        self.assertEqual(self._post(self.PIN_ADMIN).status_code, 200)                  # el administrador no se ve afectado

    def test_datos_malos_no_envian(self):
        self.assertEqual(self._post(self.PIN_ADMIN, email="mal").status_code, 400)
        self.assertEqual(self._post(self.PIN_ADMIN, reserva="x").status_code, 400)
        self.enviar.assert_not_called()

    def test_error_de_envio(self):
        self.enviar.side_effect = Exception("Apps Script caído")
        r = self._post(self.PIN_ADMIN)
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["ok"])

    def test_tope_diario(self):
        for _ in range(10):
            self.assertEqual(self._post(self.PIN_ADMIN).status_code, 200)
        self.assertEqual(self._post(self.PIN_ADMIN).status_code, 429)
        self.assertEqual(self.enviar.call_count, 10)


if __name__ == "__main__":
    unittest.main()

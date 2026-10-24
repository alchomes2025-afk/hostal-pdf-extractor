import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import reservas_correo as rc

AHORA = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


class Doc:
    def __init__(self, col, id):
        self.col, self.id = col, id

    @property
    def exists(self):
        return self.id in self.col.datos

    def get(self):
        return self

    def to_dict(self):
        return dict(self.col.datos.get(self.id, {}))

    def set(self, d):
        self.col.datos[self.id] = dict(d)

    def update(self, d):
        self.col.datos[self.id].update(d)


class Coleccion:
    def __init__(self):
        self.datos = {}

    def document(self, id):
        return Doc(self, id)

    def where(self, campo, op, valores):
        c = Coleccion()
        c.datos = {k: v for k, v in self.datos.items() if v.get(campo) in valores}
        c.padre = self
        return c

    def stream(self):
        return [Doc(self, k) for k in self.datos]


def reserva(id, ref, llegada="2026-11-02", salida="2026-11-04", status="confirmed", room=702399):
    return {"id": id, "roomId": room, "arrival": llegada, "departure": salida, "status": status, "apiReference": ref,
            "firstName": "Ana", "lastName": "X", "apiSource": "Booking.com"}


class Analizar(unittest.TestCase):
    def test_booking_nueva_con_numero_y_fechas(self):
        a = rc.analizar_aviso("New booking! Ana Pérez (Confirmation number: 4521896307)", "noreply@booking.com",
                              "Check-in: 2026-11-02\nCheck-out: 2026-11-04")
        self.assertEqual((a["plataforma"], a["tipo"], a["ref"], a["llegada"], a["salida"], a["entendido"]),
                         ("Booking.com", "nueva", "4521896307", "2026-11-02", "2026-11-04", True))

    def test_fechas_en_espanol_con_mes_en_letra(self):
        a = rc.analizar_aviso("Nueva reserva", "automated@airbnb.com", "Código de confirmación: HMAB12CD34\nLlegada: 2 nov 2026\nSalida: 4 de noviembre de 2026")
        self.assertEqual((a["plataforma"], a["ref"], a["llegada"], a["salida"]), ("Airbnb", "HMAB12CD34", "2026-11-02", "2026-11-04"))

    def test_fecha_dia_mes_anio(self):
        a = rc.analizar_aviso("Reserva confirmada", "x@hostelworld.com", "Reserva nº 77123456\nEntrada: 02/11/2026\nSalida: 04/11/2026")
        self.assertEqual((a["llegada"], a["salida"], a["ref"]), ("2026-11-02", "2026-11-04", "77123456"))

    def test_cancelacion_gana_a_nueva(self):
        a = rc.analizar_aviso("Booking cancelled: new booking 4521896307 was cancelled", "noreply@booking.com", "")
        self.assertEqual(a["tipo"], "cancelada")

    def test_modificacion(self):
        self.assertEqual(rc.analizar_aviso("Reservation change 4521896307", "noreply@booking.com", "")["tipo"], "modificada")

    def test_fechas_incoherentes_no_se_comparan(self):
        a = rc.analizar_aviso("New booking 4521896307", "noreply@booking.com", "Check-in: 2026-11-04\nCheck-out: 2026-11-02")
        self.assertEqual((a["llegada"], a["salida"]), (None, None))

    def test_sin_numero_o_sin_tipo_no_se_entiende(self):
        self.assertFalse(rc.analizar_aviso("Newsletter de Booking", "noreply@booking.com", "ofertas")["entendido"])
        self.assertFalse(rc.analizar_aviso("New booking!", "noreply@booking.com", "sin número")["entendido"])

    def test_nunca_falla_con_basura(self):
        self.assertFalse(rc.analizar_aviso(None, None, None)["entendido"])


class FormatosReales(unittest.TestCase):
    """Asuntos vistos en el buzón real (datos de ejemplo inventados)."""

    def test_el_aviso_de_beds24_se_ignora(self):
        for asunto in ("Booking: Alc Homes SelfCheck in: Double Room - Wed 28 Oct 2026: 95179058 - Perez Lopez - Booking.com",
                       "Booking Cancelled: Alc Homes SelfCheck in: Queen Room - Fri 5 Mar 2027: 92643265 - Ejemplo - Booking.com",
                       "Booking: La Casa de la Primavera: La Casa de la Primavera - Sun 11 Oct 2026: 94475726 - BLOCKED by Holidu - iCal import 1"):
            self.assertTrue(rc.analizar_aviso(asunto, "bookings", "New Booking Notification").get("ignorar"), asunto)

    def test_hostelworld_confirmed_booking(self):
        a = rc.analizar_aviso("Hostelworld Confirmed Booking - ALC HOMES Self Check In, Alicante", "Market Support",
                              "This booking was made on 19th Oct 2026 by the customer (ref: 341220-580685291). Arrival: 2026-11-02")
        self.assertEqual((a["plataforma"], a["tipo"], a["ref"], a["llegada"]), ("Hostelworld", "nueva", "341220-580685291", "2026-11-02"))

    def test_holidu_nueva_reserva(self):
        a = rc.analizar_aviso("Confirmación de reserva Holidu (7771763)", "Holidu <host@holidu.com>",
                              "¡Hola! Tienes una nueva reserva.\nID DE LA RESERVA: 7771763\nPROPIEDAD: La Casa De La Primavera\nFECHAS: 17 sept 2026 - 20 sept 2026")
        self.assertEqual((a["plataforma"], a["tipo"], a["ref"], a["llegada"], a["salida"]), ("Holidu", "nueva", "7771763", "2026-09-17", "2026-09-20"))

    def test_trip_aceptada_y_cancelacion(self):
        a = rc.analizar_aviso("Booking no. #1321665408558772# accepted#1321665408558772# - Print", "noreply_htl", "")
        self.assertEqual((a["plataforma"], a["tipo"], a["ref"]), ("Trip.com", "nueva", "1321665408558772"))
        c = rc.analizar_aviso("Cancellation request accepted (booking no. #1598742168515769#)#1598742168515769#", "noreply_htl", "")
        self.assertEqual((c["tipo"], c["ref"]), ("cancelada", "1598742168515769"))

    def test_mensajes_de_huespedes_y_errores_no_son_avisos_de_reserva(self):
        for asunto in ("Hemos recibido este mensaje de Lauri Maran", "Booking.com returned an error message for Alc Homes SelfCheck in - roomId 702395"):
            self.assertFalse(rc.analizar_aviso(asunto, "noreply", "Número de reserva: 6162178509")["entendido"], asunto)


class Cotejar(unittest.TestCase):
    def aviso(self, **kw):
        return {"tipo": "nueva", "ref": "4521896307", "llegada": "2026-11-02", "salida": "2026-11-04", **kw}

    def test_cuadra(self):
        self.assertEqual(rc.cotejar(self.aviso(), [reserva(1, "4521896307")])[0], "ok")

    def test_no_existe(self):
        self.assertEqual(rc.cotejar(self.aviso(), [reserva(1, "999999999")])[0], "no_existe")

    def test_fechas_distintas(self):
        self.assertEqual(rc.cotejar(self.aviso(), [reserva(1, "4521896307", llegada="2026-11-03")])[0], "fechas")

    def test_sin_fechas_en_el_correo_basta_con_que_exista(self):
        self.assertEqual(rc.cotejar(self.aviso(llegada=None, salida=None), [reserva(1, "4521896307", llegada="2026-12-01", salida="2026-12-02")])[0], "ok")

    def test_cancelada_en_beds24(self):
        self.assertEqual(rc.cotejar(self.aviso(), [reserva(1, "4521896307", status="cancelled")])[0], "cancelada_en_beds24")

    def test_cancelacion_que_sigue_activa(self):
        self.assertEqual(rc.cotejar(self.aviso(tipo="cancelada"), [reserva(1, "4521896307")])[0], "sigue_activa")

    def test_cancelacion_ya_reflejada(self):
        self.assertEqual(rc.cotejar(self.aviso(tipo="cancelada"), [reserva(1, "4521896307", status="cancelled")])[0], "ok")
        self.assertEqual(rc.cotejar(self.aviso(tipo="cancelada"), [])[0], "ok")

    def test_el_id_interno_de_beds24_no_vale_como_referencia(self):
        self.assertEqual(rc.cotejar(self.aviso(ref="1234567890"), [reserva(1234567890, "otra")])[0], "no_existe")


class Flujo(unittest.TestCase):
    def setUp(self):
        self.col = Coleccion()
        self.envios = []
        for nombre, valor in (("_coleccion", mock.Mock(return_value=self.col)),
                              ("enviar_whatsapp_callmebot", mock.Mock(side_effect=self.envios.append))):
            p = mock.patch.object(rc, nombre, valor)
            p.start()
            self.addCleanup(p.stop)

    def mensaje(self, id="m1", asunto="New booking! Ana (Confirmation number: 4521896307)", cuerpo="Check-in: 2026-11-02\nCheck-out: 2026-11-04"):
        return {"id": id, "fecha": "2026-10-24", "de": "noreply@booking.com", "asunto": asunto, "cuerpo": cuerpo}

    def recibir(self, minutos_atras, **kw):
        r = rc.recibir_avisos([self.mensaje(**kw)])
        self.col.datos[kw.get("id", "m1")]["recibido"] = (AHORA - timedelta(minutes=minutos_atras)).isoformat()
        return r

    def pasada(self, reservas, ahora=AHORA):
        return rc.verificar_pendientes(ahora=ahora, leer_beds24=lambda d, h: reservas)

    def test_recibir_guarda_pendiente_sin_el_cuerpo_y_no_repite(self):
        self.assertEqual(rc.recibir_avisos([self.mensaje()]), {"nuevos": 1, "sin_entender": 0, "repetidos": 0, "ignorados": 0})
        self.assertEqual(rc.recibir_avisos([self.mensaje()])["repetidos"], 1)
        d = self.col.datos["m1"]
        self.assertEqual((d["estado"], d["ref"]), ("pendiente", "4521896307"))
        self.assertNotIn("cuerpo", d)

    def test_aviso_sin_entender_se_anota_y_no_da_alarma(self):
        rc.recibir_avisos([self.mensaje(id="x", asunto="Oferta especial", cuerpo="hola")])
        self.assertEqual(self.col.datos["x"]["estado"], "sin_entender")
        self.assertEqual(self.pasada([]), {"verificadas": 0, "avisadas": 0, "resueltas": 0, "esperando": 0})
        self.assertEqual(self.envios, [])

    def test_si_cuadra_se_verifica_sin_avisar(self):
        self.recibir(5)
        self.assertEqual(self.pasada([reserva(1, "4521896307")])["verificadas"], 1)
        self.assertEqual((self.col.datos["m1"]["estado"], self.envios), ("verificada", []))

    def test_dentro_de_la_espera_no_se_avisa(self):
        self.recibir(rc.ESPERA_MINUTOS - 5)
        self.assertEqual(self.pasada([])["esperando"], 1)
        self.assertEqual((self.col.datos["m1"]["estado"], self.envios), ("pendiente", []))

    def test_pasada_la_espera_se_avisa_una_sola_vez(self):
        self.recibir(rc.ESPERA_MINUTOS + 1)
        self.assertEqual(self.pasada([])["avisadas"], 1)
        self.assertEqual(len(self.envios), 1)
        self.assertIn("NO está en Beds24", self.envios[0])
        self.assertIn("4521896307", self.envios[0])
        self.pasada([])
        self.assertEqual(len(self.envios), 1)                      # no se repite
        self.assertEqual(self.col.datos["m1"]["estado"], "avisada")

    def test_cuando_cuadra_despues_llega_el_aviso_de_resuelto(self):
        self.recibir(rc.ESPERA_MINUTOS + 1)
        self.pasada([])
        self.pasada([reserva(1, "4521896307")])
        self.assertEqual(len(self.envios), 2)
        self.assertIn("Ya cuadra", self.envios[1])
        self.assertEqual(self.col.datos["m1"]["estado"], "verificada")
        self.pasada([reserva(1, "4521896307")])
        self.assertEqual(len(self.envios), 2)

    def test_si_beds24_no_responde_no_se_avisa(self):
        self.recibir(300)
        def falla(d, h):
            raise RuntimeError("Credit limit exceeded")
        r = rc.verificar_pendientes(ahora=AHORA, leer_beds24=falla)
        self.assertEqual((r["avisadas"], self.envios, self.col.datos["m1"]["estado"]), (0, [], "pendiente"))

    def test_sin_pendientes_no_se_consulta_beds24(self):
        llamadas = []
        rc.verificar_pendientes(ahora=AHORA, leer_beds24=lambda d, h: llamadas.append(1) or [])
        self.assertEqual(llamadas, [])

    def test_varios_problemas_van_en_un_solo_mensaje(self):
        self.recibir(30, id="a")
        rc.recibir_avisos([self.mensaje(id="b", asunto="New booking! Luis (Confirmation number: 7777777777)")])
        self.col.datos["b"]["recibido"] = (AHORA - timedelta(minutes=40)).isoformat()
        self.assertEqual(self.pasada([])["avisadas"], 2)
        self.assertEqual(len(self.envios), 1)

    def test_un_aviso_que_nunca_cuadra_caduca(self):
        self.recibir(rc.ESPERA_MINUTOS + 1)
        self.pasada([])
        self.col.datos["m1"]["recibido"] = (AHORA - timedelta(hours=rc.VIGILAR_HORAS + 1)).isoformat()
        self.pasada([])
        self.assertEqual(self.col.datos["m1"]["estado"], "caducada")


class Endpoint(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from routes import rebotes
        self.rebotes = rebotes
        app = Flask(__name__)
        app.register_blueprint(rebotes.rebotes_bp)
        self.c = app.test_client()
        p = mock.patch.object(rebotes, "APPS_SCRIPT_EMAIL_SECRET", "s3")
        p.start()
        self.addCleanup(p.stop)

    def test_exige_el_secreto(self):
        self.assertEqual(self.c.post("/reserva-correo", json={"secret": "mal", "mensajes": []}).status_code, 401)
        self.assertEqual(self.c.post("/reserva-correo", json={"mensajes": []}).status_code, 401)

    def test_exige_la_lista(self):
        self.assertEqual(self.c.post("/reserva-correo", json={"secret": "s3"}).status_code, 400)

    def test_registra(self):
        with mock.patch.object(self.rebotes, "recibir_avisos", return_value={"nuevos": 1, "sin_entender": 0, "repetidos": 0}) as m:
            d = self.c.post("/reserva-correo", json={"secret": "s3", "mensajes": [{"id": "1"}]}).get_json()
        self.assertEqual((d["ok"], d["nuevos"]), (True, 1))
        m.assert_called_once()

    def test_la_consulta_pide_token(self):
        self.assertEqual(self.c.get("/reservas-correo").status_code, 401)


if __name__ == "__main__":
    unittest.main()

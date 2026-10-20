"""Mejoras tras la reserva de última hora del 2026-10-18: registro de avisos y aviso de CallMeBot caído, reloj de /check-changes
ante un fallo parcial de Beds24 y comprobación en vivo de disponibilidad al crear una reserva."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_routes
from services import avisos_registro, whatsapp


class Doc:
    def __init__(self, a): self.a = a
    def get(self):
        return type("S", (), {"exists": bool(self.a), "to_dict": lambda s: dict(self.a)})()
    def set(self, d): self.a.clear(); self.a.update(d)


class Col:
    def __init__(self, a): self.a = a
    def document(self, k): return Doc(self.a)


class Db:
    def __init__(self): self.a = {}
    def collection(self, n): return Col(self.a)


class Registro(unittest.TestCase):
    def setUp(self):
        self.db = Db()
        p = mock.patch.object(avisos_registro.config, "db", self.db)
        p.start()
        self.addCleanup(p.stop)

    def test_guarda_hora_asunto_y_canales(self):
        n = avisos_registro.registrar("Titulo del aviso\nmas texto", None, [("Telegram", None)])
        self.assertEqual(n, 0)
        e = self.db.a["envios"][0]
        self.assertEqual((e["asunto"], e["whatsapp"], e["respaldos"]), ("Titulo del aviso", "ok", {"Telegram": "ok"}))

    def test_cuenta_fallos_seguidos_y_se_reinicia(self):
        for esperado in (1, 2, 3):
            self.assertEqual(avisos_registro.registrar("a", "CallMeBot no confirmó", [("Telegram", None)]), esperado)
        self.assertEqual(avisos_registro.registrar("a", None, []), 0)

    def test_solo_conserva_los_ultimos(self):
        for _ in range(avisos_registro.MAX_ENVIOS + 5):
            avisos_registro.registrar("a", None, [])
        self.assertEqual(len(self.db.a["envios"]), avisos_registro.MAX_ENVIOS)

    def test_sin_firestore_no_rompe(self):
        with mock.patch.object(avisos_registro.config, "db", None):
            self.assertIsNone(avisos_registro.registrar("a", None, []))


class CallMeBotCaido(unittest.TestCase):
    def test_avisa_por_respaldo_una_sola_vez_al_tercer_fallo(self):
        seguidos = iter([1, 2, 3, 4])
        enviados = []
        with mock.patch.object(whatsapp, "_enviar_callmebot", side_effect=Exception("no confirmó")), \
             mock.patch.object(whatsapp, "_enviar_respaldos", side_effect=lambda m: enviados.append(m) or [("Telegram", None)]), \
             mock.patch.object(whatsapp.avisos_registro, "registrar", side_effect=lambda *a: next(seguidos)):
            for _ in range(4):
                whatsapp.enviar_whatsapp_callmebot("aviso")
        avisos_cm = [m for m in enviados if "CallMeBot no está entregando" in m]
        self.assertEqual(len(avisos_cm), 1)
        self.assertEqual(len(enviados), 5)   # 4 avisos + 1 aviso de CallMeBot caído

    def test_si_no_llega_por_ningun_canal_sigue_lanzando(self):
        with mock.patch.object(whatsapp, "_enviar_callmebot", side_effect=Exception("no confirmó")), \
             mock.patch.object(whatsapp, "_enviar_respaldos", return_value=[]), \
             mock.patch.object(whatsapp.avisos_registro, "registrar", return_value=1):
            with self.assertRaises(Exception):
                whatsapp.enviar_whatsapp_callmebot("aviso")


class Resp:
    def __init__(self, data, status=200): self._d, self.status_code, self.ok = data, status, status < 400
    def json(self): return {"data": self._d}


class Base(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(mobile_routes.mobile_bp)
        self.c = app.test_client()
        for o, n, v in ((mobile_routes, "MOBILE_APP_PIN", "1111"), (mobile_routes, "MOBILE_APP_PIN_ADMIN", "9999"),
                        (mobile_routes, "get_access_token", lambda: "tok"), (mobile_routes, "PROPERTY_IDS", ["1", "2"])):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)


class CheckChanges(Base):
    def _get(self, respuestas):
        def b24(token, path, params=None, timeout=20):
            return respuestas(params)
        with mock.patch.object(mobile_routes, "b24_get", side_effect=b24):
            return self.c.get("/mobile/check-changes?since=2026-10-18T17:00:00", headers={"x-app-pin": "1111"}).get_json()

    def test_si_una_propiedad_falla_el_reloj_no_avanza(self):
        def r(params):
            return Resp([], 429) if params["propertyId"] == "2" else Resp([])
        d = self._get(r)
        self.assertEqual(d["checked_at"], "2026-10-18T17:00:00")
        self.assertTrue(d["incompleto"])

    def test_sin_fallos_el_reloj_avanza(self):
        d = self._get(lambda p: Resp([]))
        self.assertNotEqual(d["checked_at"], "2026-10-18T17:00:00")
        self.assertFalse(d["incompleto"])


class CrearReserva(Base):
    CUERPO = {"name": "Ana Ruiz", "phone": "+34600111222", "email": "a@b.com", "roomId": 720841, "arrival": "2026-10-18", "departure": "2026-10-19", "price": 90}

    def _crear(self, existentes, status=200):
        with mock.patch.object(mobile_routes, "b24_get", return_value=Resp(existentes, status)), \
             mock.patch.object(mobile_routes, "b24_post") as post:
            post.return_value = mock.Mock(ok=True, json=lambda: [{"new": {"id": 77}}])
            r = self.c.post("/mobile/create-booking", json=self.CUERPO, headers={"x-app-pin": "1111"})
        return r, post

    def test_rechaza_si_otra_reserva_ocupa_la_noche(self):
        r, post = self._crear([{"id": 5, "status": "confirmed", "arrival": "2026-10-18", "departure": "2026-10-19"}])
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["ocupada"])
        post.assert_not_called()

    def test_un_bloqueo_tambien_ocupa(self):
        r, post = self._crear([{"id": 6, "status": "black", "arrival": "2026-10-17", "departure": "2026-10-20"}])
        self.assertEqual(r.status_code, 409)

    def test_cancelada_o_misma_fecha_de_salida_no_ocupa(self):
        r, post = self._crear([{"id": 7, "status": "cancelled", "arrival": "2026-10-18", "departure": "2026-10-19"},
                               {"id": 8, "status": "confirmed", "arrival": "2026-10-17", "departure": "2026-10-18"},
                               {"id": 9, "status": "confirmed", "arrival": "2026-10-19", "departure": "2026-10-21"}])
        self.assertEqual(r.status_code, 200)
        post.assert_called_once()

    def test_si_beds24_no_responde_no_crea(self):
        r, post = self._crear([], status=429)
        self.assertEqual(r.status_code, 503)
        post.assert_not_called()


class BloquearYEditar(Base):
    def _post(self, ruta, cuerpo, por_id, solapan):
        def b24(token, path, params=None, timeout=20):
            if params and "id" in params:
                return Resp(por_id)
            return Resp(solapan)
        with mock.patch.object(mobile_routes, "b24_get", side_effect=b24), mock.patch.object(mobile_routes, "b24_post") as post:
            post.return_value = mock.Mock(ok=True, json=lambda: [{"new": {"id": 77}}])
            r = self.c.post("/mobile/" + ruta, json=cuerpo, headers={"x-app-pin": "1111"})
        return r, post

    def test_bloquear_sobre_una_reserva_se_rechaza(self):
        r, post = self._post("block-dates", {"roomId": 720841, "from": "2026-10-18", "to": "2026-10-20"}, [],
                             [{"id": 5, "status": "confirmed", "arrival": "2026-10-19", "departure": "2026-10-21"}])
        self.assertEqual(r.status_code, 409)
        post.assert_not_called()

    def test_bloquear_sitio_libre_funciona(self):
        r, post = self._post("block-dates", {"roomId": 720841, "from": "2026-10-18", "to": "2026-10-20"}, [], [])
        self.assertEqual(r.status_code, 200)
        post.assert_called_once()

    def test_editar_fechas_a_un_hueco_ocupado_se_rechaza(self):
        actual = [{"id": 10, "roomId": 720841, "arrival": "2026-10-18", "departure": "2026-10-19"}]
        r, post = self._post("edit-booking", {"bookingId": 10, "departure": "2026-10-21"}, actual,
                             [actual[0], {"id": 11, "status": "confirmed", "arrival": "2026-10-20", "departure": "2026-10-22"}])
        self.assertEqual(r.status_code, 409)
        post.assert_not_called()

    def test_editar_no_choca_consigo_misma(self):
        actual = [{"id": 10, "roomId": 720841, "arrival": "2026-10-18", "departure": "2026-10-19"}]
        r, post = self._post("edit-booking", {"bookingId": 10, "departure": "2026-10-20"}, actual, [actual[0]])
        self.assertEqual(r.status_code, 200)
        post.assert_called_once()

    def test_editar_solo_el_nombre_no_consulta_disponibilidad(self):
        llamadas = []
        with mock.patch.object(mobile_routes, "b24_get", side_effect=lambda *a, **k: llamadas.append(1) or Resp([])),              mock.patch.object(mobile_routes, "b24_post") as post:
            post.return_value = mock.Mock(ok=True, json=lambda: [{}])
            r = self.c.post("/mobile/edit-booking", json={"bookingId": 10, "name": "Ana Ruiz"}, headers={"x-app-pin": "1111"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(llamadas, [])


if __name__ == "__main__":
    unittest.main()

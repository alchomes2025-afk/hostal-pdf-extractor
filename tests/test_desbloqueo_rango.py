"""Desbloqueo de un RANGO de noches de un bloqueo y bloqueos importados de Holidu."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_routes
from mobile_routes import planificar_desbloqueo
from services import limites

HABITACION = 702399
CASA = 720841


class Planificar(unittest.TestCase):
    """Bloqueo del 10 al 20 (noches 10..19, salida el 20)."""
    L, S = "2026-11-10", "2026-11-20"

    def plan(self, desde, hasta):
        return planificar_desbloqueo(self.L, self.S, desde, hasta)

    def test_todo_el_bloqueo_se_cancela(self):
        self.assertEqual(self.plan("2026-11-10", "2026-11-19")["accion"], "cancelar")

    def test_un_rango_mayor_que_el_bloqueo_se_recorta_y_cancela(self):
        p = self.plan("2026-11-01", "2026-11-30")
        self.assertEqual((p["accion"], p["liberadas"]), ("cancelar", ("2026-11-10", "2026-11-19")))

    def test_el_principio_acorta_la_llegada(self):
        p = self.plan("2026-11-10", "2026-11-12")
        self.assertEqual((p["accion"], p["llegada"], p["salida"]), ("acortar_inicio", "2026-11-13", "2026-11-20"))

    def test_el_final_acorta_la_salida(self):
        p = self.plan("2026-11-15", "2026-11-19")
        self.assertEqual((p["accion"], p["llegada"], p["salida"]), ("acortar_final", "2026-11-10", "2026-11-15"))

    def test_un_trozo_del_medio_parte_el_bloqueo_en_dos(self):
        p = self.plan("2026-11-13", "2026-11-14")
        self.assertEqual((p["accion"], p["llegada"], p["salida"], p["resto"]), ("partir", "2026-11-10", "2026-11-13", ("2026-11-15", "2026-11-20")))

    def test_una_sola_noche_en_el_medio(self):
        p = self.plan("2026-11-14", "2026-11-14")
        self.assertEqual((p["salida"], p["resto"]), ("2026-11-14", ("2026-11-15", "2026-11-20")))

    def test_la_primera_y_la_ultima_noche(self):
        self.assertEqual(self.plan("2026-11-10", "2026-11-10")["llegada"], "2026-11-11")
        self.assertEqual(self.plan("2026-11-19", "2026-11-19")["salida"], "2026-11-19")

    def test_rangos_sin_sentido(self):
        for desde, hasta in (("2026-11-14", "2026-11-12"), ("2026-11-01", "2026-11-09"), ("2026-11-20", "2026-11-25")):
            with self.assertRaises(ValueError):
                self.plan(desde, hasta)

    def test_la_noche_de_salida_no_es_del_bloqueo(self):
        with self.assertRaises(ValueError):
            self.plan("2026-11-20", "2026-11-20")


def respuesta(datos, ok=True, codigo=200):
    r = mock.Mock()
    r.ok = ok
    r.status_code = codigo
    r.json.return_value = datos
    return r


class Ruta(unittest.TestCase):
    def setUp(self):
        limites._eventos.clear()
        app = Flask(__name__)
        app.register_blueprint(mobile_routes.mobile_bp)
        self.c = app.test_client()
        self.cab = {"x-app-pin": "1379"}
        self.estado = {"bookings": [
            {"id": 1, "roomId": HABITACION, "arrival": "2026-11-10", "departure": "2026-11-20", "guestName": "Fecha bloqueada - Reparación",
             "email": "bloqueo@bloqueo.com", "phone": "x", "status": "confirmed"},
            {"id": 2, "roomId": CASA, "arrival": "2026-11-14", "departure": "2027-06-01", "guestName": "BLOCKED by Holidu", "email": "", "phone": "", "status": "new"},
            {"id": 3, "roomId": HABITACION, "arrival": "2026-12-01", "departure": "2026-12-03", "guestName": "Ana García", "email": "a@b.com", "phone": "1", "status": "confirmed"},
        ], "overrides": {}, "loaded": True}
        self.llamadas = []

        def b24_post(token, ruta, json_body=None, timeout=20):
            self.llamadas.append((ruta, json_body))
            return respuesta([{"success": True, "new": {"id": 99}}] if ruta == "/bookings" and "id" not in json_body[0] else [{"success": True}])

        for obj, nombre, valor in ((mobile_routes, "ACCESO_PINS_ANTIGUOS", False), (mobile_routes, "_state", self.estado),
                                   (mobile_routes, "get_access_token", mock.Mock(return_value="tok")), (mobile_routes, "b24_post", b24_post),
                                   (mobile_routes, "_set_override", mock.Mock()), (mobile_routes, "log_action", mock.Mock())):
            p = mock.patch.object(obj, nombre, valor)
            p.start()
            self.addCleanup(p.stop)

    def post(self, cuerpo, cab=None):
        return self.c.post("/mobile/unblock-dates", json=cuerpo, headers=cab or self.cab)

    def test_trozo_del_medio_crea_el_resto_antes_de_acortar_y_restaura_la_disponibilidad(self):
        r = self.post({"bookingId": 1, "from": "2026-11-13", "to": "2026-11-14"})
        j = r.get_json()
        self.assertEqual((r.status_code, j["accion"]), (200, "partir"))
        bookings = [c for c in self.llamadas if c[0] == "/bookings"]
        self.assertEqual(len(bookings), 2)
        self.assertNotIn("id", bookings[0][1][0])                                    # primero el trozo nuevo
        self.assertEqual((bookings[0][1][0]["arrival"], bookings[0][1][0]["departure"]), ("2026-11-15", "2026-11-20"))
        self.assertEqual(bookings[1][1][0], {"id": 1, "arrival": "2026-11-10", "departure": "2026-11-13"})   # luego se acorta el original
        calendario = [c for c in self.llamadas if c[0] == "/inventory/rooms/calendar"][0][1][0]
        self.assertEqual(calendario["calendar"][0], {"from": "2026-11-13", "to": "2026-11-14", "numAvail": 1})
        self.assertEqual(j["nuevoBloqueo"]["arrival"], "2026-11-15")
        self.assertEqual(len([b for b in self.estado["bookings"] if b["guestName"].startswith("Fecha bloqueada")]), 2)

    def test_todo_el_bloqueo_se_cancela(self):
        j = self.post({"bookingId": 1, "from": "2026-11-10", "to": "2026-11-19"}).get_json()
        self.assertEqual(j["accion"], "cancelar")
        self.assertEqual(self.llamadas[0], ("/bookings", [{"id": 1, "status": "cancelled"}]))
        self.assertFalse([b for b in self.estado["bookings"] if b["id"] == 1])

    def test_el_extremo_solo_acorta(self):
        j = self.post({"bookingId": 1, "from": "2026-11-10", "to": "2026-11-12"}).get_json()
        self.assertEqual((j["accion"], j["bloqueo"]["arrival"], j["nuevoBloqueo"]), ("acortar_inicio", "2026-11-13", None))

    def test_un_bloqueo_de_holidu_no_se_toca(self):
        r = self.post({"bookingId": 2, "from": "2026-11-14", "to": "2026-11-20"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["canal"], "Holidu")
        self.assertIn("desbloquearlo en Holidu", r.get_json()["error"])
        self.assertEqual(self.llamadas, [])

    def test_la_reserva_de_un_huesped_no_se_toca(self):
        r = self.post({"bookingId": 3, "from": "2026-12-01", "to": "2026-12-02"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.llamadas, [])

    def test_si_falla_acortar_se_deshace_el_trozo_nuevo(self):
        original = mobile_routes.b24_post

        def falla_al_acortar(token, ruta, json_body=None, timeout=20):
            if ruta == "/bookings" and "id" in json_body[0] and json_body[0].get("status") != "cancelled":
                return respuesta({"error": "x"}, ok=False, codigo=500)
            return original(token, ruta, json_body, timeout)

        with mock.patch.object(mobile_routes, "b24_post", falla_al_acortar):
            r = self.post({"bookingId": 1, "from": "2026-11-13", "to": "2026-11-14"})
        self.assertEqual(r.status_code, 500)
        self.assertEqual(len([b for b in self.estado["bookings"] if b["arrival"] == "2026-11-15"]), 0)

    def test_rango_fuera_del_bloqueo(self):
        self.assertEqual(self.post({"bookingId": 1, "from": "2026-12-05", "to": "2026-12-06"}).status_code, 400)

    def test_faltan_datos_y_sin_permiso(self):
        self.assertEqual(self.post({"bookingId": 1}).status_code, 400)
        self.assertIn(self.post({"bookingId": 1, "from": "2026-11-13", "to": "2026-11-14"}, cab={"x-app-pin": "0000"}).status_code, (401, 403))

    def test_un_gestor_no_puede_tocar_bloqueos_de_la_casa(self):
        r = self.post({"bookingId": 2, "from": "2026-11-14", "to": "2026-11-20"}, cab={"x-app-pin": "2486"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()

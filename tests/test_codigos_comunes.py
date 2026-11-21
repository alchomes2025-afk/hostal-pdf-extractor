"""`codigos_comunes` de /check-in: la puerta de la calle del hostal y la urbanización de La Casa de la Primavera solo para el huésped alojado."""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import routes.checkin as checkin
from config import ROOM_CONFIG
from services import acceso_enlace, grupos_acceso

MADRID = ZoneInfo("Europe/Madrid")
HOSTAL, CASA = 702399, 720841


def reserva(room, llegada, salida, id=1):
    return {"id": id, "roomId": room, "arrival": llegada, "departure": salida, "firstName": "Ana", "lastName": "García"}


class EstadoReserva(unittest.TestCase):
    """Con la función real: solo `staying` lleva los códigos comunes."""

    def setUp(self):
        for obj, nombre, valor in ((checkin, "CODIGO_PORTAL", "111111"), (checkin, "CODIGO_URBANIZACION", "2222"),
                                   (checkin, "parte_comprobado_estancia_iniciada", mock.Mock(return_value="completado")),
                                   (checkin, "parte_recibido_para", mock.Mock(return_value=True)),
                                   (checkin, "aviso_parte_no_disponible", mock.Mock(return_value=None))):
            p = mock.patch.object(obj, nombre, valor)
            p.start()
            self.addCleanup(p.stop)

    def estado(self, room, llegada, salida, ahora):
        return checkin._estado_reserva(reserva(room, llegada, salida), ahora)

    def test_hostal_alojado_dias_despues_de_llegar(self):
        e = self.estado(HOSTAL, "2026-11-18", "2026-11-22", datetime(2026, 11, 20, 12, 0, tzinfo=MADRID))
        self.assertEqual((e["estado"], e["codigos_comunes"]), ("staying", {"portal": "111111"}))

    def test_hostal_el_dia_de_llegada_a_partir_de_las_15(self):
        e = self.estado(HOSTAL, "2026-11-20", "2026-11-22", datetime(2026, 11, 20, 16, 0, tzinfo=MADRID))
        self.assertEqual((e["estado"], e["codigos_comunes"]), ("staying", {"portal": "111111"}))

    def test_casa_de_la_primavera_lleva_la_urbanizacion_y_no_el_portal(self):
        e = self.estado(CASA, "2026-11-18", "2026-11-22", datetime(2026, 11, 20, 12, 0, tzinfo=MADRID))
        self.assertEqual(e["codigos_comunes"], {"urbanizacion": "2222"})

    def test_todas_las_habitaciones_del_hostal_llevan_el_portal(self):
        for room in (r for r in ROOM_CONFIG if r != str(CASA)):
            e = self.estado(int(room), "2026-11-18", "2026-11-22", datetime(2026, 11, 20, 12, 0, tzinfo=MADRID))
            self.assertEqual(e["codigos_comunes"], {"portal": "111111"}, room)

    def test_antes_de_las_15_no_hay_codigos(self):
        e = self.estado(HOSTAL, "2026-11-20", "2026-11-22", datetime(2026, 11, 20, 10, 0, tzinfo=MADRID))
        self.assertNotIn("codigos_comunes", e)
        self.assertFalse(e["pin_available"])

    def test_antes_del_dia_de_llegada_no_hay_codigos(self):
        self.assertNotIn("codigos_comunes", self.estado(HOSTAL, "2026-11-25", "2026-11-27", datetime(2026, 11, 20, 12, 0, tzinfo=MADRID)))

    def test_con_el_parte_pendiente_no_hay_codigos(self):
        with mock.patch.object(checkin, "parte_recibido_para", return_value=False):
            e = self.estado(HOSTAL, "2026-11-20", "2026-11-22", datetime(2026, 11, 20, 16, 0, tzinfo=MADRID))
        self.assertEqual(e["estado"], "checkin_pending")
        self.assertNotIn("codigos_comunes", e)
        with mock.patch.object(checkin, "parte_comprobado_estancia_iniciada", return_value="pendiente"):
            e = self.estado(HOSTAL, "2026-11-18", "2026-11-22", datetime(2026, 11, 20, 12, 0, tzinfo=MADRID))
        self.assertNotIn("codigos_comunes", e)

    def test_estancia_terminada_no_hay_codigos(self):
        e = self.estado(HOSTAL, "2026-11-10", "2026-11-12", datetime(2026, 11, 20, 12, 0, tzinfo=MADRID))
        self.assertEqual(e["estado"], "expired")
        self.assertNotIn("codigos_comunes", e)

    def test_solo_cifras_sin_almohadilla(self):
        e = self.estado(HOSTAL, "2026-11-18", "2026-11-22", datetime(2026, 11, 20, 12, 0, tzinfo=MADRID))
        self.assertNotIn("#", e["codigos_comunes"]["portal"])
        self.assertTrue(e["codigos_comunes"]["portal"].isdigit())

    def test_sin_el_codigo_configurado_el_campo_no_aparece(self):
        with mock.patch.object(checkin, "CODIGO_PORTAL", ""):
            e = self.estado(HOSTAL, "2026-11-18", "2026-11-22", datetime(2026, 11, 20, 12, 0, tzinfo=MADRID))
        self.assertNotIn("codigos_comunes", e)


class Doc:
    def __init__(self, a, k): self.a, self.k = a, k
    def get(self):
        d = self.a.get(self.k)
        return type("S", (), {"exists": d is not None, "to_dict": lambda s: dict(d) if d else {}})()
    def set(self, datos): self.a[self.k] = dict(datos)


class Col:
    def __init__(self, a): self.a = a
    def document(self, k): return Doc(self.a, k)


class Db:
    def __init__(self): self.a = {}
    def collection(self, n): return Col(self.a)


class Grupos(unittest.TestCase):
    """Reserva de varias habitaciones: los códigos comunes siguen al `pin` (ocultos si está pendiente o «separados»)."""

    def setUp(self):
        self.db = Db()
        self.grupo = [{"id": 100, "roomId": 702399, "arrival": "2026-11-18", "departure": "2026-11-22", "firstName": "Empresa", "lastName": "SL", "_alojado": True},
                      {"id": 101, "roomId": 702398, "arrival": "2026-11-18", "departure": "2026-11-22", "masterId": 100, "_alojado": True},
                      {"id": 102, "roomId": 702397, "arrival": "2026-11-18", "departure": "2026-11-22", "masterId": 100, "_alojado": True}]
        app = Flask(__name__)
        app.register_blueprint(checkin.checkin_bp)
        self.c = app.test_client()

        def estado(b, ahora):
            if b.get("_alojado"):
                return checkin._alojado(str(b["roomId"]), {"pin": "123456"})
            return {"estado": "checkin_pending", "parte_submitted": False, "pin_available": False, "pin": None, "rpv_link": None}

        for o, n, v in ((acceso_enlace, "CHECKIN_LINK_SECRET", "clave-de-prueba"), (grupos_acceso.config, "db", self.db),
                        (checkin, "CODIGO_PORTAL", "111111"),
                        (checkin, "buscar_booking_por_id", lambda i: next((b for b in self.grupo if str(b["id"]) == str(i)), None)),
                        (checkin, "reservas_del_grupo", lambda b: list(self.grupo)),
                        (checkin, "reservas_encadenadas", lambda b: []),
                        (checkin, "_estado_reserva", estado)):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def get(self):
        return self.c.get("/check-in?t=" + acceso_enlace.crear_token("100")).get_json()

    def test_juntos_los_lleva_cada_habitacion_igual_que_el_pin(self):
        grupos_acceso.guardar("100", "juntos")
        d = self.get()
        self.assertEqual(d["codigos_comunes"], {"portal": "111111"})
        self.assertTrue(all(h["pin"] == "123456" and h["codigos_comunes"] == {"portal": "111111"} for h in d["habitaciones"]))

    def test_separados_no_los_lleva_ni_arriba_ni_en_las_habitaciones(self):
        grupos_acceso.guardar("100", "separados")
        d = self.get()
        self.assertNotIn("codigos_comunes", d)
        self.assertTrue(all("codigos_comunes" not in h and h["pin"] is None for h in d["habitaciones"]))

    def test_pendiente_de_elegir_no_los_lleva(self):
        for b in self.grupo:
            b["_alojado"] = False   # nadie alojado y sin elección: se pregunta y no se entrega nada
        d = self.get()
        self.assertEqual(d["eleccion_grupo"], "pendiente")
        self.assertNotIn("codigos_comunes", d)
        self.assertTrue(all("codigos_comunes" not in h for h in d["habitaciones"]))

    def test_con_el_enlace_de_una_habitacion_sale_solo_el_suyo(self):
        grupos_acceso.guardar("100", "juntos")
        d = self.c.get("/check-in?t=" + acceso_enlace.crear_token_habitacion("101")).get_json()
        self.assertEqual(d["codigos_comunes"], {"portal": "111111"})
        self.assertNotIn("habitaciones", d)


class ReservasDePrueba(unittest.TestCase):
    def test_una_reserva_ficticia_alojada_lleva_los_codigos_y_la_que_no_lo_esta_no(self):
        app = Flask(__name__)
        app.register_blueprint(checkin.checkin_bp)
        c = app.test_client()
        con, sin = {}, {}
        for ref, t in checkin.TEST_BOOKINGS.items():
            d = c.get(f"/check-in?ref={ref}").get_json()
            (con if t.get("estado") == "staying" else sin)[ref] = d
        self.assertTrue(con and sin)
        for d in con.values():
            self.assertIn("codigos_comunes", d)
        for d in sin.values():
            self.assertNotIn("codigos_comunes", d)


if __name__ == "__main__":
    unittest.main()

"""Reserva de varias habitaciones: quien reserva elige si ve todos los códigos («juntos») o reparte un enlace por habitación («separados»)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import routes.checkin as checkin
from services import acceso_enlace, grupos_acceso

H1 = {"id": 100, "roomId": 702399, "arrival": "2026-10-25", "departure": "2026-10-27", "firstName": "Empresa", "lastName": "SL"}
H2 = {"id": 101, "roomId": 702398, "arrival": "2026-10-25", "departure": "2026-10-27", "masterId": 100}
H3 = {"id": 102, "roomId": 702397, "arrival": "2026-10-25", "departure": "2026-10-27", "masterId": 100}
GRUPO = [H1, H2, H3]
POR_ID = {str(b["id"]): b for b in GRUPO}


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


def estado(b, ahora):
    return {"estado": "staying" if b.get("_alojado") else "checkin_pending", "parte_submitted": bool(b.get("_alojado")),
            "pin_available": bool(b.get("_alojado")), "pin": "123456" if b.get("_alojado") else None, "rpv_link": None}


class Base(unittest.TestCase):
    def setUp(self):
        self.db = Db()
        self.grupo = [dict(b) for b in GRUPO]
        app = Flask(__name__)
        app.register_blueprint(checkin.checkin_bp)
        self.c = app.test_client()
        for o, n, v in ((acceso_enlace, "CHECKIN_LINK_SECRET", "clave-de-prueba"), (grupos_acceso.config, "db", self.db),
                        (checkin, "buscar_booking_por_id", lambda i: next((b for b in self.grupo if str(b["id"]) == str(i)), None)),
                        (checkin, "reservas_del_grupo", lambda b: list(self.grupo)),
                        (checkin, "reservas_encadenadas", lambda b: []),
                        (checkin, "_estado_reserva", estado)):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def get(self, token):
        return self.c.get("/check-in?t=" + token).get_json()

    def alojar(self):
        for b in self.grupo:
            b["_alojado"] = True


class Tokens(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(acceso_enlace, "CHECKIN_LINK_SECRET", "clave-de-prueba")
        p.start()
        self.addCleanup(p.stop)

    def test_el_token_de_reserva_y_el_de_habitacion_no_se_confunden(self):
        reserva, hab = acceso_enlace.crear_token("100"), acceso_enlace.crear_token_habitacion("100")
        self.assertEqual(acceso_enlace.interpretar_token(reserva), ("100", False))
        self.assertEqual(acceso_enlace.interpretar_token(hab), ("100", True))
        self.assertIsNone(acceso_enlace.verificar_token(hab))          # el de habitación no vale como el de la reserva

    def test_manipulados_se_rechazan(self):
        hab = acceso_enlace.crear_token_habitacion("100")
        self.assertEqual(acceso_enlace.interpretar_token(hab[:-2] + "AA"), (None, False))
        otro = acceso_enlace.crear_token_habitacion("101")
        self.assertEqual(acceso_enlace.interpretar_token(otro.split(".")[0] + "." + hab.split(".")[1]), (None, False))   # firma de otra habitación
        self.assertEqual(acceso_enlace.interpretar_token("basura"), (None, False))

    def test_sin_clave_no_hay_tokens_de_habitacion(self):
        with mock.patch.object(acceso_enlace, "CHECKIN_LINK_SECRET", ""):
            self.assertIsNone(acceso_enlace.crear_token_habitacion("100"))


class Eleccion(Base):
    def test_sin_elegir_pregunta_y_no_entrega_codigos(self):
        d = self.get(acceso_enlace.crear_token("100"))
        self.assertEqual(d["eleccion_grupo"], "pendiente")
        self.assertIsNone(d["pin"])
        self.assertTrue(all(h["pin"] is None and not h["pin_available"] for h in d["habitaciones"]))
        self.assertEqual(len(d["habitaciones"]), 3)

    def test_juntos_es_como_siempre(self):
        self.alojar()
        for h in self.grupo:
            pass
        grupos_acceso.guardar("100", "juntos")
        d = self.get(acceso_enlace.crear_token("100"))
        self.assertEqual(d["eleccion_grupo"], "juntos")
        self.assertTrue(all(h["pin"] == "123456" for h in d["habitaciones"]))

    def test_separados_no_da_codigos_y_reparte_un_enlace_por_habitacion(self):
        self.alojar()
        grupos_acceso.guardar("100", "separados")
        d = self.get(acceso_enlace.crear_token("100"))
        self.assertEqual(d["eleccion_grupo"], "separados")
        self.assertIsNone(d["pin"])
        self.assertTrue(all(h["pin"] is None for h in d["habitaciones"]))
        enlaces = {h["book_id"]: h["acceso_hab"] for h in d["habitaciones"]}
        self.assertEqual({acceso_enlace.interpretar_token(t) for t in enlaces.values()}, {("100", True), ("101", True), ("102", True)})

    def test_si_ya_estan_alojados_no_se_les_pregunta(self):
        self.alojar()
        d = self.get(acceso_enlace.crear_token("100"))
        self.assertEqual(d["eleccion_grupo"], "juntos")
        self.assertTrue(all(h["pin"] == "123456" for h in d["habitaciones"]))

    def test_sin_firestore_todo_va_como_antes(self):
        self.alojar()
        with mock.patch.object(grupos_acceso.config, "db", None):
            d = self.get(acceso_enlace.crear_token("100"))
        self.assertNotIn("eleccion_grupo", d)
        self.assertTrue(all(h["pin"] == "123456" for h in d["habitaciones"]))


class EnlaceDeHabitacion(Base):
    def test_solo_ve_su_habitacion_y_su_codigo(self):
        self.alojar()
        grupos_acceso.guardar("100", "separados")
        d = self.get(acceso_enlace.crear_token_habitacion("101"))
        self.assertTrue(d["solo_habitacion"])
        self.assertNotIn("habitaciones", d)
        self.assertNotIn("otras_reservas", d)
        self.assertEqual((d["book_id"], d["room_id"], d["pin"]), (101, "702398", "123456"))
        self.assertEqual(acceso_enlace.interpretar_token(d["acceso"]), ("101", True))   # la web recordará el de la habitación, no el del grupo

    def test_no_revela_las_otras_habitaciones(self):
        self.alojar()
        txt = str(self.get(acceso_enlace.crear_token_habitacion("101")))
        self.assertNotIn("702399", txt)
        self.assertNotIn("702397", txt)

    def test_el_token_de_habitacion_no_sirve_para_elegir(self):
        r = self.c.post("/grupo-acceso", json={"t": acceso_enlace.crear_token_habitacion("100"), "modo": "separados"})
        self.assertEqual(r.status_code, 404)


class Endpoint(Base):
    def _post(self, **cuerpo):
        return self.c.post("/grupo-acceso", json=cuerpo)

    def test_guarda_la_eleccion(self):
        r = self._post(t=acceso_enlace.crear_token("101"), modo="separados")   # con el token de una hija se guarda en la maestra
        self.assertEqual((r.status_code, r.get_json()["modo"]), (200, "separados"))
        self.assertEqual(grupos_acceso.obtener("100"), "separados")

    def test_se_puede_cambiar_de_opinion(self):
        self._post(t=acceso_enlace.crear_token("100"), modo="separados")
        self._post(t=acceso_enlace.crear_token("100"), modo="juntos")
        self.assertEqual(grupos_acceso.obtener("100"), "juntos")

    def test_modo_no_valido_y_token_falso(self):
        self.assertEqual(self._post(t=acceso_enlace.crear_token("100"), modo="otro").status_code, 400)
        self.assertEqual(self._post(t="falso.falso", modo="juntos").status_code, 404)

    def test_una_reserva_de_una_sola_habitacion_no_es_un_grupo(self):
        self.grupo = [self.grupo[0]]
        self.assertEqual(self._post(t=acceso_enlace.crear_token("100"), modo="separados").status_code, 400)

    def test_si_no_se_puede_guardar_avisa(self):
        with mock.patch.object(grupos_acceso.config, "db", None):
            self.assertEqual(self._post(t=acceso_enlace.crear_token("100"), modo="juntos").status_code, 503)


if __name__ == "__main__":
    unittest.main()

"""Historial de sustitución: todo lo que hace el 2589 queda registrado por el servidor, con su nombre de sesión; solo lo lee el 1379."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_entrenamiento
import mobile_historial
import mobile_operativa
import mobile_routes
from services import accesos, entrevista, historial_ia, limites
from services import historial_sustitucion as hs
from services import operativa_kb as kb

CASA = 720841


class Doc:
    def __init__(self, a, k): self.a, self.id = a, k
    def get(self):
        d = self.a.get(self.id)
        return type("S", (), {"exists": d is not None, "to_dict": lambda s: dict(d) if d else {}})()
    def set(self, d): self.a[self.id] = dict(d)
    def delete(self): self.a.pop(self.id, None)
    def to_dict(self): return dict(self.a[self.id])


class Col:
    def __init__(self, a): self.a = a
    def document(self, k): return Doc(self.a, k)
    def stream(self): return [Doc(self.a, k) for k in list(self.a)]


class Db:
    def __init__(self): self.c = {}
    def collection(self, n): return Col(self.c.setdefault(n, {}))


def cab(perfil, nombre="Ana"):
    if perfil in ("sustituto", "propietario"):
        return {"Authorization": "Bearer " + accesos.crear_sesion(perfil, nombre)}
    return {"x-app-pin": {"gestor": "2486", "gestor_primavera": "9999"}[perfil]}


RESERVAS = [{"id": 10, "roomId": 702399, "arrival": "2026-11-01", "departure": "2026-11-03", "guestName": "Luis Gómez", "price": 120, "phone": "600111222", "email": "luis@x.com"}]


class Base(unittest.TestCase):
    def setUp(self):
        limites._eventos.clear()
        kb.olvidar_cache()
        hs._ultima_limpieza["t"] = 0
        self.db = Db()
        app = Flask(__name__)
        for bp in (mobile_routes.mobile_bp, mobile_operativa.operativa_bp, mobile_entrenamiento.entrenamiento_bp, mobile_historial.sustitucion_bp):
            app.register_blueprint(bp)
        for ep in ("mobile.create_booking", "mobile.edit_booking", "mobile.cancel_booking", "mobile.block_dates", "mobile.all_data", "mobile.reload_data",
                   "mobile.mobile_assistant_preparar_codigos", "mobile.mobile_assistant_enviar_codigos"):
            app.view_functions[ep] = lambda: ({"ok": True}, 200)
        self.c = app.test_client()
        self.respuesta_asistente = {"ok": True, "respuesta": "Hay 2 llegadas hoy."}
        for o, n, v in ((hs.config, "db", self.db), (kb.config, "db", self.db), (mobile_routes, "ACCESO_PINS_ANTIGUOS", False),
                        (mobile_routes, "_state", {"bookings": [dict(b) for b in RESERVAS]}),
                        (mobile_routes, "ROOMS", [{"id": 702399, "name": "Cala del Moraig"}, {"id": CASA, "name": "La Casa de la Primavera"}]),
                        (mobile_routes, "asistente_responder", lambda mensajes, ctx: (self.respuesta_asistente, 200)), (mobile_routes, "clave_groq", lambda: "k")):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def eventos(self):
        return sorted(self.db.c.get("historial_sustitucion", {}).values(), key=lambda e: e["ts"])

    def tipos(self):
        return [e["tipo"] for e in self.eventos()]


class Registro(Base):
    def test_el_acceso_del_sustituto_queda_registrado_con_su_nombre(self):
        self.c.post("/mobile/login", json={"pin": "2589", "nombre": "ana lópez", "deviceId": "dispositivo-1234"})
        (e,) = self.eventos()
        self.assertEqual((e["tipo"], e["sustituto"], e["perfil"]), ("acceso", "Ana López", "sustituto"))
        self.assertIn("caduca", e)

    def test_el_login_sin_nombre_no_registra_nada(self):
        self.c.post("/mobile/login", json={"pin": "2589"})
        self.assertEqual(self.eventos(), [])

    def test_los_demas_perfiles_no_se_registran(self):
        self.c.post("/mobile/login", json={"pin": "1379"})
        self.c.get("/mobile/all-data", headers=cab("propietario"))
        self.c.post("/mobile/assistant", json={"mensajes": [{"role": "user", "content": "hola"}]}, headers=cab("propietario"))
        self.c.get("/mobile/all-data", headers=cab("gestor"))
        self.assertEqual(self.eventos(), [])

    def test_el_calendario(self):
        self.c.get("/mobile/all-data", headers=cab("sustituto"))
        self.assertEqual(self.tipos(), ["calendario"])

    def test_pregunta_y_respuesta_del_asistente(self):
        self.c.post("/mobile/assistant", json={"mensajes": [{"role": "user", "content": "¿Quién llega hoy?"}]}, headers=cab("sustituto", "Marta"))
        (e,) = self.eventos()
        self.assertEqual((e["tipo"], e["sustituto"]), ("consulta_asistente", "Marta"))
        self.assertEqual((e["detalle"]["pregunta"], e["detalle"]["respuesta"]), ("¿Quién llega hoy?", "Hay 2 llegadas hoy."))

    def test_si_el_asistente_le_manda_escalar_queda_como_escalado(self):
        self.respuesta_asistente = {"ok": True, "respuesta": "Eso no está disponible para tu perfil: consúltalo con Ione."}
        self.c.post("/mobile/assistant", json={"mensajes": [{"role": "user", "content": "¿cuánto ganamos?"}]}, headers=cab("sustituto"))
        self.assertEqual(self.tipos(), ["consulta_asistente", "escalado"])
        self.assertEqual(self.eventos()[1]["detalle"]["a"], "Ione")
        self.respuesta_asistente = {"ok": True, "respuesta": "Para un fallo del sistema avisa a Adrián por WhatsApp."}
        self.c.post("/mobile/assistant", json={"mensajes": [{"role": "user", "content": "la app falla"}]}, headers=cab("sustituto"))
        self.assertEqual(self.eventos()[-1]["detalle"]["a"], "Adrián")

    def test_crear_reserva(self):
        self.c.post("/mobile/create-booking", json={"roomId": 702399, "name": "Eva Ruiz", "arrival": "2026-11-05", "departure": "2026-11-07", "price": 90, "phone": "600", "email": "e@x.com"}, headers=cab("sustituto"))
        (e,) = self.eventos()
        self.assertEqual(e["tipo"], "reserva_creada")
        self.assertEqual(e["detalle"]["despues"], {"habitacion": "Cala del Moraig", "llegada": "2026-11-05", "salida": "2026-11-07", "huesped": "Eva Ruiz", "precio": 90})
        self.assertNotIn("600", str(e)); self.assertNotIn("e@x.com", str(e))      # sin teléfono ni correo del huésped

    def test_modificar_reserva_guarda_el_antes_y_el_despues(self):
        def modifica():
            mobile_routes._state["bookings"][0]["departure"] = "2026-11-04"      # lo que haría la ruta real
            return {"ok": True}, 200
        self.c.application.view_functions["mobile.edit_booking"] = modifica
        self.c.post("/mobile/edit-booking", json={"bookingId": 10, "departure": "2026-11-04"}, headers=cab("sustituto"))
        (e,) = self.eventos()
        self.assertEqual((e["tipo"], e["detalle"]["antes"]["salida"], e["detalle"]["despues"]["salida"]), ("reserva_modificada", "2026-11-03", "2026-11-04"))
        self.assertIn("2026-11-03", e["resumen"]); self.assertIn("2026-11-04", e["resumen"])

    def test_cancelar_reserva_guarda_como_era(self):
        self.c.post("/mobile/cancel-booking", json={"bookingId": 10}, headers=cab("sustituto"))
        (e,) = self.eventos()
        self.assertEqual((e["tipo"], e["detalle"]["antes"]["huesped"], e["detalle"]["antes"]["habitacion"]), ("reserva_cancelada", "Luis Gómez", "Cala del Moraig"))

    def test_bloqueo(self):
        self.c.post("/mobile/block-dates", json={"roomId": 702399, "from": "2026-12-01", "to": "2026-12-03", "reason": "Reparación"}, headers=cab("sustituto"))
        (e,) = self.eventos()
        self.assertEqual(e["tipo"], "bloqueo")
        self.assertIn("Reparación", e["resumen"])

    def test_emergencia_con_el_correo_enmascarado(self):
        self.c.post("/mobile/assistant/preparar-codigos", json={"reserva": "10", "email": "huesped@gmail.com", "idioma": "es", "pin_envio": "1432"}, headers=cab("sustituto"))
        self.c.post("/mobile/assistant/enviar-codigos", json={"reserva": "10", "email": "huesped@gmail.com", "idioma": "es", "pin_envio": "1432"}, headers=cab("sustituto"))
        self.assertEqual(self.tipos(), ["emergencia", "emergencia"])
        self.assertNotIn("huesped@gmail.com", str(self.eventos()))
        self.assertIn("h***@gmail.com", self.eventos()[0]["resumen"])

    def test_tarea_consultada(self):
        self.db.c["kb_operativa"] = {"abc12345": {"tipo": "tarea", "titulo": "Revisar llegadas", "contenido": "1. Abrir.", "estado": "aprobado", "propiedad": ""}}
        self.c.get("/mobile/operativa/entrada?id=abc12345", headers=cab("sustituto"))
        (e,) = self.eventos()
        self.assertEqual((e["tipo"], e["resumen"]), ("tarea_consultada", "Consultó «Revisar llegadas»"))

    def test_una_operacion_rechazada_no_se_registra_como_hecha(self):
        self.c.post("/mobile/create-booking", json={"roomId": CASA, "name": "x"}, headers=cab("sustituto"))     # la Primavera: 403
        self.assertEqual(self.eventos(), [])

    def test_el_nombre_sale_de_la_sesion_no_de_lo_que_mande_el_navegador(self):
        self.c.post("/mobile/block-dates?userName=Ione", json={"roomId": 702399, "from": "2026-12-01", "to": "2026-12-02", "userName": "Ione", "sustituto": "Ione"}, headers=cab("sustituto", "Ana"))
        self.assertEqual(self.eventos()[0]["sustituto"], "Ana")

    def test_sin_firestore_la_operacion_sigue_funcionando(self):
        with mock.patch.object(hs.config, "db", None):
            r = self.c.post("/mobile/cancel-booking", json={"bookingId": 10}, headers=cab("sustituto"))
        self.assertEqual(r.status_code, 200)


class EventosDelCliente(Base):
    def _evento(self, tipo, datos, perfil="sustituto"):
        return self.c.post("/mobile/registro/evento", json={"tipo": tipo, "datos": datos}, headers=cab(perfil))

    def test_botones_y_reservas_vistas(self):
        self._evento("boton", {"nombre": "🚨 Emergencia"})
        self._evento("reserva_consultada", {"habitacion": "Cala del Moraig", "llegada": "2026-11-01", "salida": "2026-11-03", "huesped": "Luis Gómez"})
        self.assertEqual(self.tipos(), ["boton", "reserva_consultada"])
        self.assertIn("Pulsó «🚨 Emergencia»", self.eventos()[0]["resumen"])
        self.assertIn("Luis Gómez", self.eventos()[1]["resumen"])

    def test_solo_los_tipos_del_cliente_y_solo_para_perfiles_auditados(self):
        self._evento("reserva_cancelada", {"x": 1})            # un cliente no puede inventarse una cancelación
        self._evento("acceso", {})
        self._evento("boton", {"nombre": "x"}, perfil="propietario")
        self.assertEqual(self.eventos(), [])

    def test_los_perfiles_sin_operativa_no_entran(self):
        self.assertEqual(self._evento("boton", {"nombre": "x"}, perfil="gestor").status_code, 403)


class Consulta(Base):
    def _registrar(self):
        for nombre, tipo, resumen in (("Ana", "calendario", "Abrió el calendario"), ("Ana", "bloqueo", "Bloqueó algo"), ("Marta", "consulta_asistente", "Preguntó algo")):
            hs.registrar(accesos.PERFILES["sustituto"], nombre, tipo, resumen)

    def test_solo_el_propietario_lee_el_historial(self):
        self._registrar()
        for perfil in ("sustituto", "gestor", "gestor_primavera"):
            self.assertEqual(self.c.get("/mobile/historial-sustitucion", headers=cab(perfil)).status_code, 403, perfil)
            self.assertEqual(self.c.post("/mobile/historial-sustitucion/preguntar", json={"pregunta": "¿qué hizo ayer?"}, headers=cab(perfil)).status_code, 403, perfil)
        self.assertEqual(self.c.get("/mobile/historial-sustitucion", headers=cab("propietario")).status_code, 200)

    def test_filtros_por_sustituto_dia_y_tipo(self):
        self._registrar()
        hoy = self.eventos()[0]["ts"][:10]
        d = self.c.get("/mobile/historial-sustitucion", headers=cab("propietario")).get_json()
        self.assertEqual(len(d["eventos"]), 3)
        self.assertEqual([s["nombre"] for s in d["sustitutos"]].sort(), ["Ana", "Marta"].sort())
        self.assertIn("bloqueo", d["tipos"])
        q = lambda **p: self.c.get("/mobile/historial-sustitucion", query_string=p, headers=cab("propietario")).get_json()["eventos"]
        self.assertEqual({e["sustituto"] for e in q(sustituto="Marta")}, {"Marta"})
        self.assertEqual([e["tipo"] for e in q(tipo="bloqueo")], ["bloqueo"])
        self.assertEqual(len(q(dia=hoy)), 3)
        self.assertEqual(q(dia="2020-01-01"), [])

    def test_solo_se_guardan_tipos_conocidos_y_para_perfiles_auditados(self):
        self.assertFalse(hs.registrar(accesos.PERFILES["sustituto"], "Ana", "inventado", "x"))
        self.assertFalse(hs.registrar(accesos.PERFILES["propietario"], "Ione", "calendario", "x"))
        self.assertFalse(hs.registrar(None, "Ana", "calendario", "x"))
        self.assertEqual(self.eventos(), [])

    def test_los_caducados_se_borran(self):
        self.db.c["historial_sustitucion"] = {"viejo": {"ts": "2020-01-01T10:00:00+00:00", "sustituto": "Ana", "tipo": "calendario", "resumen": "x", "caduca": "2021-01-01T10:00:00+00:00"}}
        hs._ultima_limpieza["t"] = 0
        hs.registrar(accesos.PERFILES["sustituto"], "Ana", "calendario", "nuevo")
        self.assertEqual(len(self.db.c["historial_sustitucion"]), 1)

    def test_la_hora_se_da_en_madrid(self):
        self.db.c["historial_sustitucion"] = {"a": {"ts": "2026-11-01T23:30:00+00:00", "sustituto": "Ana", "tipo": "calendario", "resumen": "x", "caduca": "2099-01-01T00:00:00+00:00"}}
        (e,) = hs.listar()
        self.assertEqual((e["fecha"], e["hora"]), ("2026-11-02", "00:30"))      # a las 23:30 UTC ya es el día siguiente en Madrid

    def test_el_asistente_del_historial_traduce_la_pregunta_a_filtros_y_responde_con_los_registros(self):
        self._registrar()
        vistos = []
        respuestas = [{"sustituto": "Ana", "desde": None, "hasta": None, "tipos": ["bloqueo"]}, {"respuesta": "Ana bloqueó fechas hoy."}]
        def modelo(mensajes):
            vistos.append(mensajes)
            return respuestas.pop(0)
        with mock.patch.object(entrevista, "_llamar_modelo", modelo):
            r = self.c.post("/mobile/historial-sustitucion/preguntar", json={"pregunta": "¿Qué bloqueó Ana?"}, headers=cab("propietario")).get_json()
        self.assertEqual((r["ok"], r["respuesta"], r["eventos"]), (True, "Ana bloqueó fechas hoy.", 1))
        self.assertIn("Bloqueó algo", vistos[1][0]["content"])
        self.assertNotIn("Preguntó algo", vistos[1][0]["content"])             # solo los de Ana y del tipo pedido
        self.assertIn("SOLO con los registros", vistos[1][0]["content"])

    def test_sin_actividad_no_se_llama_al_modelo_para_responder(self):
        llamadas = []
        def modelo(mensajes):
            llamadas.append(1)
            return {"sustituto": None, "desde": None, "hasta": None, "tipos": []}
        with mock.patch.object(entrevista, "_llamar_modelo", modelo):
            r = self.c.post("/mobile/historial-sustitucion/preguntar", json={"pregunta": "¿qué hizo ayer?"}, headers=cab("propietario")).get_json()
        self.assertEqual(len(llamadas), 1)
        self.assertIn("No hay actividad registrada", r["respuesta"])

    def test_errores_del_modelo_llegan_legibles(self):
        with mock.patch.object(entrevista, "_llamar_modelo", side_effect=entrevista.ErrorEntrevista("La IA está saturada ahora mismo", 503)):
            r = self.c.post("/mobile/historial-sustitucion/preguntar", json={"pregunta": "¿qué hizo ayer?"}, headers=cab("propietario"))
        self.assertEqual(r.status_code, 503)


class Arranque(unittest.TestCase):
    """La app REAL arranca con todas las blueprints (un nombre repetido la rompería) y todas sus rutas tienen permiso declarado."""

    def test_la_app_real_arranca_y_todas_sus_rutas_tienen_permiso(self):
        import app as aplicacion
        from rutas_permisos import PERMISOS_RUTAS, RUTAS_PUBLICAS
        prefijos = ("mobile.", "facturas.", "limpieza.", "operativa.", "entrenamiento.", "historial_sustitucion.")
        endpoints = {r.endpoint for r in aplicacion.app.url_map.iter_rules() if r.endpoint.startswith(prefijos)}
        self.assertTrue(endpoints)
        self.assertEqual(sorted(e for e in endpoints if e not in PERMISOS_RUTAS and e not in RUTAS_PUBLICAS), [])


if __name__ == "__main__":
    unittest.main()

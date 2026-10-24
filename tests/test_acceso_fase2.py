"""Fase 2 de los perfiles por PIN: sesión firmada, permiso por ruta (con la tabla completa por perfil), La Casa de la Primavera solo para 1379 y 9999 y
asistente sin el `finanzas` del navegador. Los PIN son los reales (1379 propietario, 9999 gestor de la Primavera, 2486 gestor, 2589 sustituto, 4268 limpieza)."""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_asistente
import mobile_facturas
import mobile_limpieza
import mobile_entrenamiento
import mobile_operativa
import mobile_routes
from rutas_permisos import PERMISOS_RUTAS, RUTAS_PUBLICAS
from services import accesos, limites

PREFIJOS = ("mobile.", "facturas.", "limpieza.", "operativa.", "entrenamiento.")
CASA = 720841


def crear_app():
    app = Flask(__name__)
    app.register_blueprint(mobile_routes.mobile_bp)
    app.register_blueprint(mobile_facturas.facturas_bp)
    app.register_blueprint(mobile_limpieza.limpieza_bp)
    app.register_blueprint(mobile_operativa.operativa_bp)
    app.register_blueprint(mobile_entrenamiento.entrenamiento_bp)
    return app


def rutas_de_la_app(app):
    return [r for r in app.url_map.iter_rules() if r.endpoint.startswith(PREFIJOS)]


def cab(perfil):
    """Credenciales de cada perfil: el sustituto, con sesión (su PIN solo no vale); el resto, con su PIN."""
    if perfil == "sustituto":
        return {"Authorization": "Bearer " + accesos.crear_sesion("sustituto", "Ana")}
    return {"x-app-pin": {"propietario": "1379", "gestor_primavera": "9999", "gestor": "2486", "limpieza": "4268"}[perfil]}


class Base(unittest.TestCase):
    def setUp(self):
        limites._eventos.clear()
        self.app = crear_app()
        self.c = self.app.test_client()
        p = mock.patch.object(mobile_routes, "ACCESO_PINS_ANTIGUOS", False)
        p.start()
        self.addCleanup(p.stop)


class Cobertura(Base):
    def test_toda_ruta_declara_su_permiso_o_es_publica(self):
        sin_declarar = [r.endpoint for r in rutas_de_la_app(self.app) if r.endpoint not in PERMISOS_RUTAS and r.endpoint not in RUTAS_PUBLICAS]
        self.assertEqual(sin_declarar, [], "rutas sin permiso en rutas_permisos.py")

    def test_la_tabla_no_tiene_rutas_que_no_existen(self):
        existentes = {r.endpoint for r in rutas_de_la_app(self.app)}
        self.assertEqual(sorted((set(PERMISOS_RUTAS) | RUTAS_PUBLICAS) - existentes), [])

    def test_todo_permiso_de_la_tabla_existe(self):
        for ep, permisos in PERMISOS_RUTAS.items():
            for p in permisos:
                self.assertIn(p, accesos.TODOS, ep)

    def test_una_ruta_nueva_sin_declarar_se_rechaza(self):
        with self.app.test_request_context("/mobile/ruta-nueva"):
            respuesta, status = mobile_routes.exigir_permiso()
        self.assertEqual(status, 403)


class Matriz(Base):
    """Cada perfil contra TODAS las rutas, con la vista sustituida por un «ok»: prueba solo la puerta."""

    def setUp(self):
        super().setUp()
        for r in rutas_de_la_app(self.app):
            self.app.view_functions[r.endpoint] = lambda: ("ok", 200)

    def _llamar(self, regla, cabeceras):
        metodo = "POST" if "POST" in regla.methods else "GET"
        return self.c.open(regla.rule, method=metodo, json={} if metodo == "POST" else None, headers=cabeceras)

    def test_cada_perfil_solo_pasa_por_donde_tiene_permiso(self):
        for perfil_id in ("propietario", "gestor_primavera", "gestor", "sustituto", "limpieza"):
            perfil = accesos.PERFILES[perfil_id]
            for regla in rutas_de_la_app(self.app):
                if regla.endpoint in RUTAS_PUBLICAS:
                    continue
                esperado = 200 if any(perfil.tiene(p) for p in PERMISOS_RUTAS[regla.endpoint]) else 403
                self.assertEqual(self._llamar(regla, cab(perfil_id)).status_code, esperado, f"{perfil_id} → {regla.rule}")

    def test_sin_credenciales_todo_pide_pin_salvo_lo_publico(self):
        for regla in rutas_de_la_app(self.app):
            esperado = 200 if regla.endpoint in RUTAS_PUBLICAS else 401
            self.assertEqual(self._llamar(regla, {}).status_code, esperado, regla.rule)

    def test_lo_que_no_pueden_2486_2589_y_9999(self):
        """Lo importante, dicho en claro: ni finanzas ni facturas ni diagnóstico, y los precios solo el 1379 y el 9999."""
        ruta = lambda ep: next(r for r in rutas_de_la_app(self.app) if r.endpoint == ep)
        for perfil in ("gestor", "sustituto"):
            for ep in ("mobile.mobile_update", "mobile.mobile_finance_desglose", "facturas.listar_facturas", "facturas.original_factura", "mobile.get_logs",
                       "mobile.test_whatsapp", "mobile.fix_zero_prices"):
                self.assertEqual(self._llamar(ruta(ep), cab(perfil)).status_code, 403, f"{perfil} → {ep}")
        for ep in ("mobile.mobile_finance_desglose", "facturas.listar_facturas", "mobile.get_logs", "mobile.test_email"):
            self.assertEqual(self._llamar(ruta(ep), cab("gestor_primavera")).status_code, 403, ep)
        self.assertEqual(self._llamar(ruta("mobile.mobile_update"), cab("gestor_primavera")).status_code, 200)


class Sesiones(Base):
    def _login(self, pin, **extra):
        return self.c.post("/mobile/login", json={"pin": pin, **extra})

    def test_login_de_cada_perfil(self):
        for pin, perfil, admin in (("1379", "propietario", True), ("9999", "gestor_primavera", False), ("2486", "gestor", False)):
            d = self._login(pin).get_json()
            self.assertEqual((d["ok"], d["perfil"], d["admin"]), (True, perfil, admin), pin)
            self.assertEqual(accesos.verificar_sesion(d["token"])[0].id, perfil)
            self.assertEqual(d["permisos"], sorted(accesos.PERFILES[perfil].permisos))

    def test_la_sesion_vale_sin_mandar_el_pin(self):
        token = self._login("1379").get_json()["token"]
        self.app.view_functions["mobile.mobile_rooms"] = lambda: ("ok", 200)
        self.assertEqual(self.c.get("/mobile/rooms", headers={"Authorization": "Bearer " + token}).status_code, 200)

    def test_sesion_manipulada_o_caducada_no_vale(self):
        self.app.view_functions["mobile.mobile_rooms"] = lambda: ("ok", 200)
        token = self._login("2486").get_json()["token"]
        cuerpo, firma = token.split(".")
        for malo in (token[:-3] + "AAA", cuerpo + ".x", "basura", cuerpo[:-2] + "ab." + firma):
            self.assertEqual(self.c.get("/mobile/rooms", headers={"Authorization": "Bearer " + malo}).status_code, 401)
        caducada = accesos.crear_sesion("propietario", None, ahora=time.time() - 13 * 3600)
        self.assertEqual(self.c.get("/mobile/rooms", headers={"Authorization": "Bearer " + caducada}).status_code, 401)

    def test_no_se_puede_subir_de_perfil_cambiando_el_token(self):
        """El perfil va dentro del token firmado: cambiar «gestor» por «propietario» invalida la firma."""
        import base64
        token = self._login("2486").get_json()["token"]
        cuerpo, firma = token.split(".")
        carga = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)))
        carga["p"] = "propietario"
        falso = base64.urlsafe_b64encode(json.dumps(carga).encode()).rstrip(b"=").decode() + "." + firma
        self.app.view_functions["mobile.mobile_rooms"] = lambda: ("ok", 200)
        self.assertEqual(self.c.get("/mobile/rooms", headers={"Authorization": "Bearer " + falso}).status_code, 401)

    def test_el_sustituto_pide_nombre_y_su_pin_solo_no_vale(self):
        d = self._login("2589").get_json()
        self.assertEqual((d["ok"], d["necesita_nombre"], d.get("token")), (True, True, None))
        self.assertIsNone(self._login("2589", nombre="").get_json().get("token"))
        self.assertIsNone(self._login("2589", nombre="<script>").get_json().get("token"))
        ok = self._login("2589", nombre="  maría   jose ").get_json()
        self.assertEqual((ok["nombre"], ok["auditado"]), ("María Jose", True))
        self.assertEqual(accesos.verificar_sesion(ok["token"])[1], "María Jose")
        self.app.view_functions["mobile.mobile_rooms"] = lambda: ("ok", 200)
        r = self.c.get("/mobile/rooms", headers={"x-app-pin": "2589"})        # el PIN compartido, sin sesión con nombre, no entra
        self.assertEqual((r.status_code, r.get_json()["necesita_nombre"]), (401, True))

    def test_el_nombre_lo_fija_el_servidor_no_el_cliente(self):
        visto = {}
        def vista():
            visto["nombre"] = mobile_routes.nombre_actual()
            return "ok", 200
        self.app.view_functions["mobile.mobile_rooms"] = vista
        token = self._login("2589", nombre="Ana").get_json()["token"]
        self.c.get("/mobile/rooms?userName=Ione&nombre=Ione", headers={"Authorization": "Bearer " + token})
        self.assertEqual(visto["nombre"], "Ana")

    def test_pin_equivocado_y_bloqueo(self):
        self.assertEqual(self._login("0000").status_code, 401)
        for _ in range(mobile_routes.MAX_FALLOS_PIN):
            self._login("0000")
        self.assertEqual(self._login("1379").status_code, 429)

    def test_la_limpiadora_conserva_el_rol_de_siempre(self):
        d = self._login("4268").get_json()
        self.assertEqual((d["perfil"], d["rol"], d["admin"]), ("limpieza", "limpieza", False))

    def test_los_pin_antiguos_siguen_hasta_el_dia_de_corte(self):
        with mock.patch.object(mobile_routes, "ACCESO_PINS_ANTIGUOS", True), mock.patch.object(mobile_routes, "MOBILE_APP_PIN", "5555"), \
             mock.patch.object(mobile_routes, "MOBILE_APP_PIN_ADMIN", "6666"):
            self.assertEqual(self._login("5555").get_json()["perfil"], "gestor_primavera")
            self.assertEqual(self._login("6666").get_json()["perfil"], "propietario")
        self.assertEqual(self._login("5555").status_code, 401)             # con ACCESO_PINS_ANTIGUOS desactivado ya no entran

    def test_si_un_pin_antiguo_coincide_con_uno_nuevo_manda_el_nuevo(self):
        with mock.patch.object(mobile_routes, "ACCESO_PINS_ANTIGUOS", True), mock.patch.object(mobile_routes, "MOBILE_APP_PIN", "2486"):
            self.assertEqual(self._login("2486").get_json()["perfil"], "gestor")


class SoloPrimavera(Base):
    """La Casa de la Primavera y todo lo relacionado solo es visible para el 1379 y el 9999."""
    RESERVAS = [{"id": 1, "roomId": 702399, "arrival": "2026-11-01", "departure": "2026-11-03", "guestName": "Hostal Uno"},
                {"id": 2, "roomId": CASA, "arrival": "2026-11-01", "departure": "2026-11-03", "guestName": "Casa Dos"}]

    def setUp(self):
        super().setUp()
        for o, n, v in ((mobile_routes, "_ensure_loaded", lambda: True), (mobile_routes, "ROOMS", [{"id": 702399, "name": "Cala"}, {"id": CASA, "name": "Primavera"}]),
                        (mobile_routes, "_state", {"bookings": self.RESERVAS, "overrides": {"702399": {"2026-11-01": {"price1": 50}}, str(CASA): {"2026-11-01": {"price1": 99}}},
                                                   "live_prices": {str(CASA): {"2026-11-01": 120}}, "loaded": True, "loaded_at": "x", "checked_at": "y"})):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def _datos(self, perfil):
        return self.c.get("/mobile/all-data", headers=cab(perfil)).get_json()

    def test_all_data(self):
        for perfil in ("gestor", "sustituto"):
            d = self._datos(perfil)
            self.assertEqual([r["id"] for r in d["rooms"]], [702399], perfil)
            self.assertEqual([b["id"] for b in d["bookings"]], [1], perfil)
            self.assertNotIn(str(CASA), d["overrides"])
            self.assertNotIn(str(CASA), d["prices"])
            self.assertNotIn("Casa Dos", json.dumps(d))
            self.assertEqual(d["booking_count"], 1)
        for perfil in ("propietario", "gestor_primavera"):
            d = self._datos(perfil)
            self.assertEqual(sorted(r["id"] for r in d["rooms"]), [702399, CASA])
            self.assertEqual(len(d["bookings"]), 2)

    def test_habitaciones(self):
        self.assertEqual([r["id"] for r in self.c.get("/mobile/rooms", headers=cab("gestor")).get_json()["rooms"]], [702399])
        self.assertEqual(len(self.c.get("/mobile/rooms", headers=cab("gestor_primavera")).get_json()["rooms"]), 2)

    def test_operaciones_sobre_la_casa_se_rechazan_para_gestor_y_sustituto(self):
        for perfil in ("gestor", "sustituto"):
            for ruta, cuerpo in (("/mobile/create-booking", {"roomId": CASA, "name": "x"}), ("/mobile/block-dates", {"roomId": CASA}),
                                 ("/mobile/edit-booking", {"bookingId": 2, "name": "x"}), ("/mobile/edit-booking", {"bookingId": 1, "roomId": CASA}),
                                 ("/mobile/cancel-booking", {"bookingId": 2})):
                r = self.c.post(ruta, json=cuerpo, headers=cab(perfil))
                self.assertEqual(r.status_code, 403, f"{perfil} {ruta} {cuerpo}")
                self.assertEqual(r.get_json()["error"], "No tienes permiso para esta propiedad")

    def test_operaciones_sobre_el_hostal_siguen_pasando_la_puerta(self):
        for ep in ("mobile.create_booking", "mobile.cancel_booking"):
            self.app.view_functions[ep] = lambda: ("ok", 200)
        r = self.c.post("/mobile/cancel-booking", json={"bookingId": 1}, headers=cab("gestor"))
        self.assertEqual(r.status_code, 200)
        r = self.c.post("/mobile/create-booking", json={"roomId": 702399}, headers=cab("gestor"))
        self.assertEqual(r.status_code, 200)

    def test_el_gestor_de_la_primavera_si_puede(self):
        self.app.view_functions["mobile.cancel_booking"] = lambda: ("ok", 200)
        self.assertEqual(self.c.post("/mobile/cancel-booking", json={"bookingId": 2}, headers=cab("gestor_primavera")).status_code, 200)

    def test_incidencias_de_limpieza_solo_para_1379_y_9999(self):
        self.app.view_functions["limpieza.incidencias"] = lambda: ("ok", 200)
        for perfil, esperado in (("propietario", 200), ("gestor_primavera", 200), ("gestor", 403), ("sustituto", 403)):
            self.assertEqual(self.c.get("/mobile/limpieza/incidencias", headers=cab(perfil)).status_code, esperado, perfil)

    def test_finanzas_de_la_primavera(self):
        for perfil in ("gestor", "sustituto"):
            self.assertEqual(self.c.get("/mobile/finance?propertyId=349341&month=2026-10", headers=cab(perfil)).status_code, 403, perfil)
        for perfil in ("gestor", "sustituto", "gestor_primavera"):    # y el hostal, solo el propietario
            self.assertEqual(self.c.get(f"/mobile/finance?propertyId={mobile_routes.PROPERTY_ID}&month=2026-10", headers=cab(perfil)).status_code, 403, perfil)


class Asistente(Base):
    """El asistente decide qué ve cada perfil con el perfil de la sesión: el `finanzas` que mande el navegador no cuenta."""

    def setUp(self):
        super().setUp()
        self.ctx = {}
        def capturar(mensajes, ctx):
            self.ctx.update(ctx)
            return {"ok": True, "respuesta": "hola"}, 200
        for o, n, v in ((mobile_routes, "asistente_responder", capturar), (mobile_routes, "clave_groq", lambda: "k")):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def _preguntar(self, perfil, **extra):
        self.ctx.clear()
        r = self.c.post("/mobile/assistant", json={"mensajes": [{"role": "user", "content": "hola"}], **extra}, headers=cab(perfil))
        self.assertEqual(r.status_code, 200)
        return self.ctx

    def test_el_contexto_sale_del_perfil(self):
        esperado = {"propietario": (True, True, True, True, True, True), "gestor_primavera": (False, True, False, False, False, True),
                    "gestor": (False, False, False, False, False, False), "sustituto": (False, False, True, False, True, False)}
        for perfil, (es_admin, finanzas, emergencia, sin_pin, ver_pin, primavera) in esperado.items():
            ctx = self._preguntar(perfil)
            self.assertEqual((ctx["es_admin"], ctx["finanzas"], ctx["emergencia"], ctx["sin_pin"], ctx["ver_pin"], ctx["primavera"]),
                             (es_admin, finanzas, emergencia, sin_pin, ver_pin, primavera), perfil)

    def test_las_tareas_de_gestion_solo_para_el_propietario_y_el_sustituto(self):
        for perfil, esperado in (("propietario", True), ("sustituto", True), ("gestor_primavera", False), ("gestor", False)):
            self.assertEqual(self._preguntar(perfil)["operativa"], esperado, perfil)

    def test_el_navegador_no_puede_darse_finanzas(self):
        for perfil in ("gestor", "sustituto"):
            self.assertFalse(self._preguntar(perfil, finanzas=True)["finanzas"], perfil)
        self.assertFalse(self._preguntar("gestor", pregunta="¿cuánto ganamos?", mensajes=None)["finanzas"] if False else self._preguntar("gestor", finanzas=True)["finanzas"])

    def test_la_limpiadora_no_usa_el_asistente(self):
        r = self.c.post("/mobile/assistant", json={"mensajes": [{"role": "user", "content": "hola"}]}, headers=cab("limpieza"))
        self.assertEqual(r.status_code, 403)

    def test_herramientas_y_reglas_segun_el_perfil(self):
        def nombres(ctx):
            return {t["function"]["name"] for t in mobile_asistente.herramientas_para(ctx)}
        gestor = {"es_admin": False, "finanzas": False, "emergencia": False, "sin_pin": False, "ver_pin": False, "primavera": False}
        sustituto = {**gestor, "emergencia": True, "ver_pin": True}
        propietario = {"es_admin": True, "finanzas": True, "emergencia": True, "sin_pin": True, "ver_pin": True, "primavera": True}
        for emergencia in ("preparar_correo_codigos", "enlace_parte_emergencia", "pin_envio_codigos"):
            self.assertNotIn(emergencia, nombres(gestor))
            self.assertIn(emergencia, nombres(sustituto))
            self.assertIn(emergencia, nombres(propietario))
        self.assertNotIn("pin_envio_codigos", nombres({**sustituto, "ver_pin": False}))
        self.assertIn("No puedes preparar ni enviar correos con códigos", mobile_asistente._system(gestor))
        self.assertIn("NO tiene acceso a La Casa de la Primavera", mobile_asistente._system(gestor))
        self.assertNotIn("NO tiene acceso a La Casa de la Primavera", mobile_asistente._system(propietario))
        self.assertIn("pin_envio_codigos", mobile_asistente._system(sustituto))            # el sustituto puede pedir el PIN
        self.assertIn("Introduce el PIN de seguridad", mobile_asistente._system(sustituto))
        self.assertNotIn("Introduce el PIN de seguridad", mobile_asistente._system(propietario))   # el propietario no lo necesita

    def test_las_herramientas_no_dan_datos_de_la_primavera(self):
        ctx = {"es_admin": False, "finanzas": False, "primavera": False, "borradores": []}
        for herramienta, args in (("agenda", {"propiedad": "primavera"}), ("listar_reservas", {"propiedad": "primavera"}), ("disponibilidad", {"propiedad": "primavera", "desde": "2026-11-01", "hasta": "2026-11-05"})):
            res = mobile_asistente.ejecutar_herramienta(herramienta, json.dumps(args), ctx)
            self.assertIn("No tienes acceso a La Casa de la Primavera", res.get("error", ""), herramienta)
        self.assertEqual(mobile_asistente._propiedades({}), ["hostal", "primavera"])      # fuera de una llamada, por defecto (compatibilidad)
        marca = mobile_asistente._PRIMAVERA_PERMITIDA.set(False)
        try:
            self.assertEqual(mobile_asistente._propiedades({}), ["hostal"])
        finally:
            mobile_asistente._PRIMAVERA_PERMITIDA.reset(marca)

    def test_codigos_de_una_reserva_de_la_primavera_se_niegan_sin_permiso(self):
        reserva = {"id": 7, "roomId": CASA, "arrival": "2026-11-01", "departure": "2026-11-03", "firstName": "X"}
        with mock.patch.object(mobile_asistente, "buscar_booking_por_id", return_value=reserva):
            with self.assertRaises(mobile_asistente.ErrorHerramienta):
                mobile_asistente.borrador_codigos("7", "a@b.com", "es", primavera=False)
            res = mobile_asistente.ejecutar_herramienta("enlace_parte_emergencia", json.dumps({"reserva": "7"}), {"emergencia": True, "primavera": False, "borradores": []})
            self.assertIn("No tienes acceso a La Casa de la Primavera", res["error"])


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


class Identidad(Base):
    """1379 = Ione o Adrián (según el dispositivo); 9999 = Hermann; el resto, como hasta ahora."""
    DISP_IONE, DISP_ADRIAN = "dispositivo-de-ione-0001", "dispositivo-de-adrian-0002"

    def setUp(self):
        super().setUp()
        self.db = Db()
        from services import dispositivos
        p = mock.patch.object(dispositivos.config, "db", self.db)
        p.start()
        self.addCleanup(p.stop)

    def _login(self, pin, **extra):
        return self.c.post("/mobile/login", json={"pin": pin, **extra}).get_json()

    def _registrar(self, token, dispositivo, nombre):
        return self.c.post("/mobile/dispositivo", json={"deviceId": dispositivo, "nombre": nombre}, headers={"Authorization": "Bearer " + token})

    def test_primera_vez_desde_un_dispositivo_la_app_tiene_que_preguntar(self):
        d = self._login("1379", deviceId=self.DISP_IONE)
        self.assertIsNone(d["nombre"])
        self.assertEqual(d["identidad"], {"necesita": True, "opciones": ["Ione", "Adrián"]})
        self.assertTrue(d["ok"] and d["token"])            # la app antigua sigue entrando sin más

    def test_se_recuerda_a_quien_pertenece_cada_dispositivo(self):
        token = self._login("1379", deviceId=self.DISP_IONE)["token"]
        r = self._registrar(token, self.DISP_IONE, "Ione").get_json()
        self.assertEqual(r["nombre"], "Ione")
        self.assertEqual(accesos.verificar_sesion(r["token"])[1], "Ione")
        self._registrar(self._login("1379", deviceId=self.DISP_ADRIAN)["token"], self.DISP_ADRIAN, "Adrián")
        ione, adrian = self._login("1379", deviceId=self.DISP_IONE), self._login("1379", deviceId=self.DISP_ADRIAN)
        self.assertEqual((ione["nombre"], ione["identidad"]["necesita"]), ("Ione", False))
        self.assertEqual((adrian["nombre"], adrian["identidad"]["necesita"]), ("Adrián", False))
        self.assertEqual(accesos.verificar_sesion(ione["token"])[1], "Ione")
        self.assertIsNone(self._login("1379", deviceId="otro-dispositivo-0003")["nombre"])

    def test_solo_se_puede_elegir_ione_o_adrian(self):
        token = self._login("1379")["token"]
        for malo in ("Hermann", "", "Ione ", "Pepe", None):
            self.assertEqual(self._registrar(token, self.DISP_IONE, malo).status_code, 400, repr(malo))
        for dispositivo in ("", "x", "../../etc", None):
            self.assertEqual(self._registrar(token, dispositivo, "Ione").status_code, 400, repr(dispositivo))

    def test_solo_el_propietario_registra_dispositivos(self):
        for perfil in ("gestor_primavera", "gestor", "sustituto", "limpieza"):
            self.assertEqual(self._registrar(cab(perfil).get("Authorization", "").replace("Bearer ", "") or accesos.crear_sesion(perfil), self.DISP_IONE, "Ione").status_code, 403, perfil)

    def test_un_dispositivo_de_otro_perfil_no_vale_como_propietario(self):
        self.db.a[self.DISP_IONE] = {"nombre": "Ione", "perfil": "gestor", "creado": "x", "ultimo_uso": "x"}
        self.assertIsNone(self._login("1379", deviceId=self.DISP_IONE)["nombre"])

    def test_el_9999_es_siempre_hermann(self):
        d = self._login("9999")
        self.assertEqual((d["nombre"], d["identidad"]), ("Hermann", None))
        self.assertEqual(accesos.verificar_sesion(d["token"])[1], "Hermann")

    def test_el_resto_sigue_como_hasta_ahora(self):
        for pin in ("2486", "4268"):
            d = self._login(pin)
            self.assertIsNone(d["nombre"], pin)               # la app sigue preguntando el nombre por su cuenta
            self.assertIsNone(d["identidad"], pin)
        self.assertTrue(self._login("2589")["necesita_nombre"])

    def test_sin_firestore_nadie_queda_identificado_pero_entra(self):
        from services import dispositivos
        with mock.patch.object(dispositivos.config, "db", None):
            d = self._login("1379", deviceId=self.DISP_IONE)
            self.assertTrue(d["ok"])
            self.assertEqual(self._registrar(d["token"], self.DISP_IONE, "Ione").status_code, 400)


if __name__ == "__main__":
    unittest.main()

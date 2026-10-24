"""Entrenamiento: la entrevista a Ione. El modelo está simulado; se prueba lo que hace el servidor con sus respuestas (borradores, sesiones, permisos)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_entrenamiento
import mobile_operativa
import mobile_routes
from services import accesos, entrevista, limites
from services import operativa_kb as kb


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


def cab(perfil, nombre="Ione"):
    if perfil in ("sustituto", "propietario"):
        return {"Authorization": "Bearer " + accesos.crear_sesion(perfil, nombre)}
    return {"x-app-pin": {"gestor": "2486", "gestor_primavera": "9999"}[perfil]}


ENTRADA = {"tipo": "tarea", "titulo": "Revisar las llegadas", "contenido": "1. Abrir la app.\n2. Mirar quién entra hoy.", "etiquetas": ["llegadas"], "propiedad": "hostal"}


class Base(unittest.TestCase):
    def setUp(self):
        limites._eventos.clear()
        kb.olvidar_cache()
        self.db = Db()
        self.respuestas = []
        self.enviados = []
        app = Flask(__name__)
        app.register_blueprint(mobile_routes.mobile_bp)
        app.register_blueprint(mobile_operativa.operativa_bp)
        app.register_blueprint(mobile_entrenamiento.entrenamiento_bp)
        self.c = app.test_client()
        def modelo(mensajes):
            self.enviados.append(mensajes)
            return self.respuestas.pop(0)
        for o, n, v in ((kb.config, "db", self.db), (entrevista.config, "db", self.db), (mobile_routes, "ACCESO_PINS_ANTIGUOS", False), (entrevista, "_llamar_modelo", modelo)):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def mensaje(self, texto="", tema="dia_a_dia", sesion=None, perfil="propietario"):
        return self.c.post("/mobile/entrenamiento/mensaje", json={"tema": tema, "texto": texto, "sesion_id": sesion}, headers=cab(perfil))


class Entrevista(Base):
    def test_el_primer_turno_hace_la_primera_pregunta_sin_guardar_nada(self):
        self.respuestas = [{"pregunta": "¿Qué es lo primero que haces al empezar el día?", "entradas": [], "tema_cubierto": False, "resumen": ""}]
        d = self.mensaje().get_json()
        self.assertEqual(d["pregunta"], "¿Qué es lo primero que haces al empezar el día?")
        self.assertEqual((d["guardadas"], d["tema_cubierto"]), ([], False))
        self.assertEqual(self.db.c.get("kb_operativa", {}), {})

    def test_el_prompt_lleva_el_tema_lo_que_cubrir_y_las_reglas(self):
        self.respuestas = [{"pregunta": "¿Y luego?", "entradas": []}]
        self.mensaje(tema="escalado_tecnico")
        sistema = self.enviados[0][0]["content"]
        self.assertIn("Escalado de problemas técnicos (a Adrián)", sistema)
        self.assertIn("qué puede probar el sustituto", sistema)
        self.assertIn("UNA sola pregunta", sistema)
        self.assertIn("NO inventes", sistema)
        self.assertIn("Lo resuelve el sustituto", sistema)               # la estructura de las entradas de escalado
        self.assertIn("Si no responde", sistema)

    def test_lo_aprendido_se_guarda_siempre_como_borrador(self):
        self.respuestas = [{"pregunta": "¿Y después?", "entradas": [{**ENTRADA, "estado": "aprobado"}], "tema_cubierto": False, "resumen": "Revisa las llegadas."}]
        d = self.mensaje("Lo primero que hago es mirar quién llega.").get_json()
        self.assertEqual(len(d["guardadas"]), 1)
        doc = self.db.c["kb_operativa"][d["guardadas"][0]["id"]]
        self.assertEqual((doc["estado"], doc["origen"], doc["tema"], doc["editado_por"], doc["propiedad"]), ("borrador", "entrenamiento", "dia_a_dia", "Ione", "hostal"))
        self.assertEqual(d["resumen"], "Revisa las llegadas.")
        self.assertEqual(self.c.get("/mobile/operativa/indice", headers=cab("sustituto")).get_json()["grupos"]["tarea"], [])   # el sustituto no ve borradores
        self.assertEqual(kb.buscar("llegadas"), [])                                                                              # ni el asistente

    def test_volver_a_contar_lo_mismo_actualiza_el_borrador_en_vez_de_duplicarlo(self):
        self.respuestas = [{"pregunta": "a", "entradas": [ENTRADA]}, {"pregunta": "b", "entradas": [{**ENTRADA, "contenido": "1. Abrir la app.\n2. Mirar llegadas.\n3. Mirar partes."}]}]
        s = self.mensaje("uno").get_json()["sesion_id"]
        d = self.mensaje("dos", sesion=s).get_json()
        self.assertEqual(len(self.db.c["kb_operativa"]), 1)
        self.assertEqual(d["guardadas"][0]["accion"], "actualizado")
        self.assertIn("3. Mirar partes", list(self.db.c["kb_operativa"].values())[0]["contenido"])

    def test_lo_ya_aprobado_no_se_pisa_se_propone_una_revision(self):
        self.respuestas = [{"pregunta": "a", "entradas": [ENTRADA]}]
        doc_id = self.mensaje("uno").get_json()["guardadas"][0]["id"]
        self.db.c["kb_operativa"][doc_id]["estado"] = "aprobado"
        original = self.db.c["kb_operativa"][doc_id]["contenido"]
        kb.olvidar_cache()
        self.respuestas = [{"pregunta": "b", "entradas": [{**ENTRADA, "contenido": "Otro texto distinto."}]}]
        d = self.mensaje("dos", sesion=None).get_json()
        self.assertEqual(d["guardadas"][0]["accion"], "revisión")
        self.assertEqual(self.db.c["kb_operativa"][doc_id]["contenido"], original)      # lo aprobado queda intacto
        revision = self.db.c["kb_operativa"][doc_id + "-rev"]
        self.assertEqual((revision["estado"], revision["titulo"]), ("borrador", "Revisar las llegadas (revisión)"))

    def test_entradas_malas_del_modelo_se_descartan_sin_romper_el_turno(self):
        self.respuestas = [{"pregunta": "¿Seguimos?", "entradas": [{"tipo": "inventado", "titulo": "x", "contenido": "y"}, "basura", {**ENTRADA, "contenido": ""}, ENTRADA]}]
        d = self.mensaje("hola").get_json()
        self.assertEqual(len(d["guardadas"]), 1)

    def test_maximo_de_entradas_por_turno(self):
        self.respuestas = [{"pregunta": "a", "entradas": [{**ENTRADA, "titulo": f"Tarea número {i}"} for i in range(9)]}]
        self.assertEqual(len(self.mensaje("hola").get_json()["guardadas"]), entrevista.MAX_ENTRADAS_POR_TURNO)

    def test_se_puede_seguir_otro_dia(self):
        self.respuestas = [{"pregunta": "Primera pregunta", "entradas": []}, {"pregunta": "Segunda pregunta", "entradas": []}]
        s = self.mensaje().get_json()["sesion_id"]
        self.mensaje("respuesta", sesion=s)
        d = self.c.get("/mobile/entrenamiento/sesion?tema=dia_a_dia", headers=cab("propietario")).get_json()["sesion"]
        self.assertEqual([m["rol"] for m in d["mensajes"]], ["entrevistador", "ione", "entrevistador"])
        again = self.mensaje("", sesion=d["id"]).get_json()                      # retomar no gasta una llamada al modelo
        self.assertEqual(again["pregunta"], "Segunda pregunta")
        self.assertEqual(len(self.enviados), 2)
        self.assertIsNone(self.c.get("/mobile/entrenamiento/sesion?tema=limpieza", headers=cab("propietario")).get_json()["sesion"])

    def test_el_modelo_solo_ve_los_ultimos_mensajes(self):
        self.respuestas = [{"pregunta": f"p{i}", "entradas": []} for i in range(20)]
        s = self.mensaje().get_json()["sesion_id"]
        for i in range(19):
            self.mensaje(f"respuesta {i}", sesion=s)
        self.assertLessEqual(len(self.enviados[-1]), entrevista.MENSAJES_AL_MODELO + 1)

    def test_tema_cubierto(self):
        self.respuestas = [{"pregunta": "Pasamos al siguiente tema.", "entradas": [], "tema_cubierto": True, "resumen": "Tema completo"}]
        self.assertTrue(self.mensaje("fin").get_json()["tema_cubierto"])

    def test_errores_del_modelo_llegan_como_mensaje_legible(self):
        with mock.patch.object(entrevista, "_llamar_modelo", side_effect=entrevista.ErrorEntrevista("La IA está saturada ahora mismo", 503)):
            r = self.mensaje("hola")
        self.assertEqual((r.status_code, r.get_json()["ok"]), (503, False))
        self.assertIn("saturada", r.get_json()["error"])

    def test_tema_desconocido_y_sin_base_de_datos(self):
        self.assertEqual(self.mensaje(tema="inventado").status_code, 404)
        with mock.patch.object(entrevista.config, "db", None):
            self.respuestas = [{"pregunta": "a", "entradas": []}]
            self.assertEqual(self.mensaje().status_code, 503)


class Permisos(Base):
    def test_solo_el_propietario_entra(self):
        for perfil in ("sustituto", "gestor", "gestor_primavera"):
            self.assertEqual(self.mensaje("hola", perfil=perfil).status_code, 403, perfil)
            self.assertEqual(self.c.get("/mobile/entrenamiento/temas", headers=cab(perfil)).status_code, 403, perfil)
            self.assertEqual(self.c.get("/mobile/entrenamiento/sesion?tema=limpieza", headers=cab(perfil)).status_code, 403, perfil)
        self.assertEqual(self.enviados, [])

    def test_los_temas_son_los_del_encargo_y_cuentan_lo_recogido(self):
        self.respuestas = [{"pregunta": "a", "entradas": [ENTRADA]}]
        self.mensaje("hola")
        d = self.c.get("/mobile/entrenamiento/temas", headers=cab("propietario")).get_json()["temas"]
        self.assertEqual([t["id"] for t in d], ["dia_a_dia", "limpieza", "reservas", "llamadas", "vending", "plataformas", "faq", "telefonos", "escalado_admin", "escalado_tecnico"])
        self.assertEqual({t["id"]: (t["borradores"], t["aprobadas"]) for t in d}["dia_a_dia"], (1, 0))

    def test_cada_tema_dice_que_cubrir(self):
        for t in entrevista.TEMAS:
            self.assertGreaterEqual(len(t["cubrir"]), 2, t["id"])
        escalado = " ".join(entrevista.TEMAS_POR_ID["escalado_admin"]["cubrir"])
        for clave in ("ejemplos", "qué resuelve", "a quién avisar", "medio", "horario", "qué datos dar", "no responde", "urgente"):
            self.assertIn(clave, escalado)


if __name__ == "__main__":
    unittest.main()

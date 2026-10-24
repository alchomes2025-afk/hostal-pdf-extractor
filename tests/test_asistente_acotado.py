"""Fase 4: el asistente acotado (2486, 9999, 2589 solo calendario, reservas, la app y el puesto de gestora), «Dirígete a tu superior» en finanzas y la base de conocimiento."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mobile_asistente as asistente
from services import operativa_kb

PROPIETARIO = {"es_admin": True, "finanzas": True, "alcance_completo": True, "emergencia": True, "sin_pin": True, "ver_pin": True, "primavera": True, "operativa": True}
GESTOR = {"es_admin": False, "finanzas": False, "alcance_completo": False, "emergencia": False, "sin_pin": False, "ver_pin": False, "primavera": False, "operativa": False}
HERMANN = {**GESTOR, "finanzas": True, "primavera": True}
SUSTITUTO = {**GESTOR, "emergencia": True, "ver_pin": True, "operativa": True}


class Doc:
    def __init__(self, k, d): self.id, self._d = k, d
    def to_dict(self): return dict(self._d)


class Db:
    def __init__(self, entradas): self.entradas = entradas
    def collection(self, nombre):
        return type("C", (), {"stream": lambda s: [Doc(k, d) for k, d in self.entradas.items()]})()


KB = {
    "a": {"tipo": "faq", "titulo": "Parking", "contenido": "Hay parking gratuito en la calle. No hay garaje propio.", "etiquetas": ["aparcamiento", "coche"], "estado": "aprobado", "orden": 2},
    "b": {"tipo": "tarea", "titulo": "Revisar las llegadas del día", "contenido": "1. Abrir el calendario. 2. Mirar quién entra hoy. 3. Comprobar los partes.", "etiquetas": ["llegadas", "diaria"], "estado": "aprobado", "orden": 1},
    "c": {"tipo": "faq", "titulo": "Cuna", "contenido": "BORRADOR sin revisar: hay cuna.", "etiquetas": ["bebé"], "estado": "borrador"},
    "d": {"tipo": "escalado", "titulo": "Fallo de la cerradura", "contenido": "Avisar a Adrián por WhatsApp. Urgente. " + "x" * 3000, "etiquetas": ["técnico", "puerta"], "estado": "aprobado"},
}


class BaseConocimiento(unittest.TestCase):
    def setUp(self):
        operativa_kb.olvidar_cache()
        p = mock.patch.object(operativa_kb.config, "db", Db(KB))
        p.start()
        self.addCleanup(p.stop)

    def test_solo_se_usan_las_entradas_aprobadas(self):
        self.assertEqual({e["titulo"] for e in operativa_kb.entradas()}, {"Parking", "Revisar las llegadas del día", "Fallo de la cerradura"})
        self.assertEqual(operativa_kb.buscar("cuna bebé"), [])

    def test_busca_sin_tildes_ni_mayusculas_y_por_etiquetas(self):
        self.assertEqual(operativa_kb.buscar("¿Dónde APARCAMIENTO?")[0]["titulo"], "Parking")
        self.assertEqual(operativa_kb.buscar("cerradura tecnico")[0]["titulo"], "Fallo de la cerradura")

    def test_acota_el_contenido_que_se_da_al_modelo(self):
        self.assertLessEqual(len(operativa_kb.buscar("cerradura")[0]["contenido"]), operativa_kb.MAX_CHARS_ENTRADA)

    def test_las_tareas_salen_ordenadas(self):
        self.assertEqual([e["titulo"] for e in operativa_kb.entradas()][:2], ["Revisar las llegadas del día", "Parking"])

    def test_sin_coincidencia_o_sin_firestore(self):
        self.assertEqual(operativa_kb.buscar("astronautas"), [])
        with mock.patch.object(operativa_kb.config, "db", None):
            operativa_kb.olvidar_cache()
            self.assertEqual(operativa_kb.buscar("parking"), [])
            self.assertEqual(operativa_kb.temas(), [])

    def test_la_herramienta_responde_con_lo_recogido(self):
        res = asistente.ejecutar_herramienta("buscar_operativa", json.dumps({"consulta": "parking"}), GESTOR)
        self.assertEqual(res["entradas"][0]["titulo"], "Parking")
        self.assertIn("SOLO con lo que dicen", res["nota"])

    def test_la_herramienta_no_inventa_si_no_hay_nada(self):
        res = asistente.ejecutar_herramienta("buscar_operativa", json.dumps({"consulta": "máquina de vending averiada"}), GESTOR)
        self.assertEqual(res["entradas"], [])
        self.assertIn("todavía no está recogido: consúltalo con Ione", res["nota"])
        self.assertIn("NO inventes", res["nota"])
        self.assertIn("Parking", json.dumps(res["temas_disponibles"]))
        self.assertIn("error", asistente.ejecutar_herramienta("buscar_operativa", json.dumps({"consulta": ""}), GESTOR))

    def test_todos_los_perfiles_con_asistente_la_tienen(self):
        for ctx in (PROPIETARIO, SUSTITUTO):
            self.assertIn("buscar_operativa", {t["function"]["name"] for t in asistente.herramientas_para(ctx)})

    def test_las_tareas_de_gestion_solo_para_el_sustituto_y_el_propietario(self):
        for ctx in (GESTOR, HERMANN):
            self.assertNotIn("buscar_operativa", {t["function"]["name"] for t in asistente.herramientas_para(ctx)})
            texto = asistente._system(ctx)
            self.assertIn("NO están disponibles para este usuario", texto)
            self.assertIn("Eso no está disponible para tu perfil: consúltalo con Ione", texto)
            self.assertNotIn("el puesto de gestora del hostal (el que ocupa normalmente Ione)", texto)


class Ambito(unittest.TestCase):
    def test_el_resto_de_perfiles_queda_acotado(self):
        for ctx in (GESTOR, HERMANN, SUSTITUTO):
            texto = asistente._system(ctx)
            self.assertIn("ÁMBITO RESTRINGIDO", texto)
            self.assertIn("Perdona, solo puedo ayudarte con el calendario", texto)
        for ctx in (SUSTITUTO,):
            texto = asistente._system(ctx)
            self.assertIn("el puesto de gestora del hostal", texto)
            self.assertIn("buscar_operativa", texto)

    def test_el_propietario_tiene_alcance_completo(self):
        texto = asistente._system(PROPIETARIO)
        self.assertNotIn("ÁMBITO RESTRINGIDO", texto)
        self.assertIn("OPERATIVA:", texto)

    def test_sin_finanzas_el_prompt_manda_dirigirse_al_superior(self):
        self.assertIn("Dirígete a Ione.", asistente._system(GESTOR))
        self.assertIn("Dirígete a Ione.", asistente._system(SUSTITUTO))
        self.assertIn("Dirígete a Ione.", asistente._system(HERMANN))      # Hermann ve la Primavera, no el hostal
        self.assertNotIn("superior", asistente._system(GESTOR))
        self.assertNotIn("PIN de administrador", asistente._system(GESTOR))

    def test_sin_dinero_no_hay_herramienta_de_finanzas(self):
        def nombres(ctx):
            return {t["function"]["name"] for t in asistente.herramientas_para(ctx)}
        self.assertNotIn("finanzas_mes", nombres(GESTOR))
        self.assertNotIn("finanzas_mes", nombres(SUSTITUTO))
        self.assertIn("finanzas_mes", nombres(HERMANN))
        self.assertIn("finanzas_mes", nombres(PROPIETARIO))

    def test_llamadas_antiguas_sin_el_dato_siguen_con_alcance_completo(self):
        self.assertNotIn("ÁMBITO RESTRINGIDO", asistente._system({"es_admin": False, "finanzas": True}))


class FiltroDeDinero(unittest.TestCase):
    """Sin acceso a finanzas, una pregunta de dinero ni siquiera llega al modelo."""

    def _responder(self, ctx, pregunta):
        with mock.patch.object(asistente.requests, "post", side_effect=AssertionError("no debe llamar a Groq")):
            return asistente.responder([{"role": "user", "content": pregunta}], dict(ctx))

    def test_preguntas_de_dinero_reciben_la_respuesta_fija(self):
        for pregunta in ("¿Cuánto hemos ganado este mes?", "dime los ingresos de octubre", "¿qué beneficio dejó la Primavera?", "comisión de Booking del mes",
                         "¿cuál es la rentabilidad?", "gastos de limpieza", "cuánto pagamos de impuestos", "FINANZAS"):
            for ctx in (GESTOR, SUSTITUTO):
                res, status = self._responder(ctx, pregunta)
                self.assertEqual((status, res["respuesta"]), (200, "Dirígete a Ione."), (pregunta, ctx is SUSTITUTO))

    def test_las_preguntas_normales_si_llegan_al_modelo(self):
        llamadas = []
        def post(url, headers=None, json=None, timeout=None):
            llamadas.append(json)
            return mock.Mock(status_code=200, text="", json=lambda: {"choices": [{"message": {"content": "Entra Ana"}}]})
        with mock.patch.object(asistente.requests, "post", post), mock.patch.object(asistente, "_habitaciones", return_value=[]), mock.patch.object(asistente, "_mr"), \
             mock.patch.object(asistente, "clave_groq", lambda: "k", create=True):
            for pregunta in ("¿Quién entra hoy?", "Un huésped pide factura, ¿qué hago?", "¿Cómo bloqueo una fecha en la app?"):
                res, status = asistente.responder([{"role": "user", "content": pregunta}], dict(GESTOR))
                self.assertEqual((status, res["respuesta"]), (200, "Entra Ana"), pregunta)
        self.assertEqual(len(llamadas), 3)

    def test_quien_tiene_finanzas_no_pasa_por_el_filtro(self):
        llamadas = []
        def post(url, headers=None, json=None, timeout=None):
            llamadas.append(1)
            return mock.Mock(status_code=200, text="", json=lambda: {"choices": [{"message": {"content": "Han sido 1.000 €"}}]})
        with mock.patch.object(asistente.requests, "post", post), mock.patch.object(asistente, "_habitaciones", return_value=[]), mock.patch.object(asistente, "_mr"):
            for ctx in (PROPIETARIO, HERMANN):
                res, _ = asistente.responder([{"role": "user", "content": "¿Cuánto hemos ganado?"}], dict(ctx))
                self.assertEqual(res["respuesta"], "Han sido 1.000 €")
        self.assertEqual(len(llamadas), 2)


if __name__ == "__main__":
    unittest.main()

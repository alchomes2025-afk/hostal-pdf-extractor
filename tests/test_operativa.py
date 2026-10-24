"""Pestaña «Operativa de Gestión»: la ven el 1379 y el 2589; solo el 1379 edita la base de conocimiento."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_operativa
import mobile_routes
from services import accesos, limites
from services import operativa_kb as kb


class Doc:
    def __init__(self, a, k): self.a, self.id = a, k
    def set(self, d): self.a[self.id] = dict(d)
    def delete(self): self.a.pop(self.id, None)
    def to_dict(self): return dict(self.a[self.id])


class Col:
    def __init__(self, a): self.a = a
    def document(self, k): return Doc(self.a, k)
    def stream(self): return [Doc(self.a, k) for k in list(self.a)]


class Db:
    def __init__(self): self.a = {}
    def collection(self, n): return Col(self.a)


def cab(perfil, nombre="Ana"):
    if perfil in ("sustituto", "propietario"):
        return {"Authorization": "Bearer " + accesos.crear_sesion(perfil, nombre)}
    return {"x-app-pin": {"gestor": "2486", "gestor_primavera": "9999", "limpieza": "4268"}[perfil]}


TAREA = {"tipo": "tarea", "titulo": "Revisar llegadas", "contenido": "1. Abrir el calendario.\n2. Mirar quién entra.", "etiquetas": "llegadas, diaria", "estado": "aprobado", "orden": 1}


class Base(unittest.TestCase):
    def setUp(self):
        limites._eventos.clear()
        self.db = Db()
        kb.olvidar_cache()
        app = Flask(__name__)
        app.register_blueprint(mobile_routes.mobile_bp)
        app.register_blueprint(mobile_operativa.operativa_bp)
        self.c = app.test_client()
        for o, n, v in ((kb.config, "db", self.db), (mobile_routes, "ACCESO_PINS_ANTIGUOS", False)):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def guardar(self, datos=None, perfil="propietario", nombre="Ione"):
        return self.c.post("/mobile/operativa/kb", json=datos or TAREA, headers=cab(perfil, nombre))


class Edicion(Base):
    def test_el_propietario_crea_edita_y_borra(self):
        r = self.guardar().get_json()
        self.assertTrue(r["ok"])
        doc = self.db.a[r["id"]]
        self.assertEqual((doc["tipo"], doc["estado"], doc["etiquetas"], doc["editado_por"]), ("tarea", "aprobado", ["llegadas", "diaria"], "Ione"))
        self.guardar({**TAREA, "id": r["id"], "titulo": "Revisar las llegadas del día"})
        self.assertEqual(len(self.db.a), 1)                                        # edita, no duplica
        self.assertEqual(self.db.a[r["id"]]["titulo"], "Revisar las llegadas del día")
        self.assertEqual(self.c.post("/mobile/operativa/kb/borrar", json={"id": r["id"]}, headers=cab("propietario")).status_code, 200)
        self.assertEqual(self.db.a, {})

    def test_nadie_mas_escribe_en_la_base_de_conocimiento(self):
        for perfil in ("sustituto", "gestor", "gestor_primavera", "limpieza"):
            self.assertEqual(self.guardar(perfil=perfil).status_code, 403, perfil)
            self.assertEqual(self.c.post("/mobile/operativa/kb/borrar", json={"id": "abcd1234"}, headers=cab(perfil)).status_code, 403, perfil)
            self.assertEqual(self.c.get("/mobile/operativa/kb", headers=cab(perfil)).status_code, 403, perfil)
        self.assertEqual(self.db.a, {})

    def test_validaciones(self):
        for malo in ({**TAREA, "tipo": "otra"}, {**TAREA, "titulo": "x"}, {**TAREA, "contenido": "  "}, {**TAREA, "contenido": "x" * 8001}, {**TAREA, "orden": "abc"}):
            self.assertEqual(self.guardar(malo).status_code, 400, malo)
        self.assertEqual(self.db.a, {})

    def test_un_estado_raro_queda_como_borrador_y_un_id_raro_se_ignora(self):
        r = self.guardar({**TAREA, "estado": "publicado", "id": "../../x"}).get_json()
        self.assertEqual(self.db.a[r["id"]]["estado"], "borrador")
        self.assertTrue(r["id"].isalnum())

    def test_sin_firestore(self):
        with mock.patch.object(kb.config, "db", None):
            self.assertEqual(self.guardar().status_code, 503)

    def test_el_propietario_ve_todo_con_su_estado(self):
        self.guardar()
        self.guardar({**TAREA, "titulo": "Pendiente de revisar", "estado": "borrador"})
        d = self.c.get("/mobile/operativa/kb", headers=cab("propietario")).get_json()
        self.assertEqual(sorted(e["estado"] for e in d["entradas"]), ["aprobado", "borrador"])
        self.assertIn("escalado", d["tipos"])


class Lectura(Base):
    def setUp(self):
        super().setUp()
        self.id_tarea = self.guardar().get_json()["id"]
        self.id_borrador = self.guardar({**TAREA, "titulo": "Sin revisar", "estado": "borrador"}).get_json()["id"]
        self.id_tel = self.guardar({"tipo": "telefono", "titulo": "Limpiadora", "contenido": "Marta: 600 000 000", "estado": "aprobado"}).get_json()["id"]

    def test_el_sustituto_y_el_propietario_ven_lo_aprobado_agrupado(self):
        for perfil in ("sustituto", "propietario"):
            g = self.c.get("/mobile/operativa/indice", headers=cab(perfil)).get_json()["grupos"]
            self.assertEqual([e["titulo"] for e in g["tarea"]], ["Revisar llegadas"], perfil)
            self.assertEqual([e["titulo"] for e in g["telefono"]], ["Limpiadora"], perfil)
            self.assertNotIn("Sin revisar", str(g))                                # los borradores no se enseñan

    def test_el_detalle_de_una_tarea(self):
        e = self.c.get(f"/mobile/operativa/entrada?id={self.id_tarea}", headers=cab("sustituto")).get_json()["entrada"]
        self.assertEqual((e["titulo"], e["tipo"]), ("Revisar llegadas", "tarea"))
        self.assertIn("Abrir el calendario", e["contenido"])

    def test_un_borrador_o_un_id_malo_no_se_enseñan(self):
        for id_ in (self.id_borrador, "inventado1", "../x", ""):
            self.assertEqual(self.c.get(f"/mobile/operativa/entrada?id={id_}", headers=cab("sustituto")).status_code, 404, id_)

    def test_el_resto_de_perfiles_no_entra(self):
        for perfil in ("gestor", "gestor_primavera", "limpieza"):
            self.assertEqual(self.c.get("/mobile/operativa/indice", headers=cab(perfil)).status_code, 403, perfil)
            self.assertEqual(self.c.get(f"/mobile/operativa/entrada?id={self.id_tarea}", headers=cab(perfil)).status_code, 403, perfil)

    def test_el_sustituto_sin_sesion_con_nombre_no_entra(self):
        self.assertEqual(self.c.get("/mobile/operativa/indice", headers={"x-app-pin": "2589"}).status_code, 401)

    def test_el_asistente_encuentra_lo_aprobado_y_no_los_borradores(self):
        self.assertEqual(kb.buscar("llegadas")[0]["titulo"], "Revisar llegadas")
        self.assertEqual(kb.buscar("sin"), [])                    # «Sin revisar» es un borrador: el asistente no lo ve


class PropiedadDeLasEntradas(Base):
    """Lo de la Casa de la Primavera solo lo ven (y lo usa el asistente) quienes tienen acceso a esa propiedad."""

    def _crear(self, titulo, propiedad):
        d = self.guardar({**TAREA, "tipo": "faq", "titulo": titulo, "contenido": f"Contenido de {titulo}: cajetín y urbanización", "etiquetas": "cajetín", "propiedad": propiedad}).get_json()
        return d["id"]

    def test_lo_de_la_primavera_no_lo_ve_el_sustituto_ni_el_asistente_sin_acceso(self):
        hostal, casa = self._crear("Acceso al hostal", "hostal"), self._crear("Acceso a la casa", "primavera")
        kb.olvidar_cache()
        sustituto = self.c.get("/mobile/operativa/indice", headers=cab("sustituto")).get_json()["grupos"]["faq"]
        propietario = self.c.get("/mobile/operativa/indice", headers=cab("propietario")).get_json()["grupos"]["faq"]
        self.assertEqual([e["titulo"] for e in sustituto], ["Acceso al hostal"])
        self.assertEqual(len(propietario), 2)
        self.assertEqual(self.c.get(f"/mobile/operativa/entrada?id={casa}", headers=cab("sustituto")).status_code, 404)
        self.assertEqual(self.c.get(f"/mobile/operativa/entrada?id={casa}", headers=cab("propietario")).status_code, 200)
        self.assertEqual([e["titulo"] for e in kb.buscar("cajetín urbanización", primavera=False)], ["Acceso al hostal"])
        self.assertEqual(len(kb.buscar("cajetín urbanización", primavera=True)), 2)

    def test_la_propiedad_se_guarda_y_se_valida(self):
        r = self.guardar({**TAREA, "propiedad": "primavera"}).get_json()
        self.assertEqual(self.db.a[r["id"]]["propiedad"], "primavera")
        r = self.guardar({**TAREA, "propiedad": "otra"}).get_json()
        self.assertEqual(self.db.a[r["id"]]["propiedad"], "")

    def test_ya_no_existe_la_importacion_de_preguntas_frecuentes(self):
        self.assertEqual(self.c.post("/mobile/operativa/kb/importar-faq", headers=cab("propietario")).status_code, 404)


if __name__ == "__main__":
    unittest.main()

"""Asesor Contable «inteligente»: compras de otro proyecto fuera de la rentabilidad, limpieza interna estimada y completar con la estimación de siempre."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import config
import mobile_facturas
import mobile_routes as mr
from services import asesor, facturas

PRIMAVERA, HOSTAL = "349341", "339751"


class Doc:
    def __init__(self, a, k): self.a, self.k = a, k
    def get(self):
        d = self.a.get(self.k)
        return type("S", (), {"exists": d is not None, "to_dict": lambda s: dict(d) if d else {}})()
    def set(self, datos): self.a[self.k] = dict(datos)


class Col:
    def __init__(self, a, f=()): self.a, self.f = a, f
    def document(self, k): return Doc(self.a, k)
    def where(self, c, op, v): return Col(self.a, self.f + ((c, v),))
    def stream(self): return [type("D", (), {"id": k, "to_dict": lambda s, v=v: dict(v)})() for k, v in self.a.items() if all(v.get(c) == x for c, x in self.f)]


class FakeDB:
    def __init__(self): self.c = {}
    def collection(self, n): return Col(self.c.setdefault(n, {}))


def f(categoria, total, mes="2026-08", propiedad=PRIMAVERA, **extra):
    return {"cliente": "alc-homes", "propiedad_id": propiedad, "tipo": "gasto", "categoria": categoria, "total": total, "cuota_iva": 0.0, "mes": mes, "fecha": mes + "-10", **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        for o, n, v in ((config, "db", self.db), (facturas, "_coleccion", lambda: self.db.collection("facturas")), (asesor.fac, "_coleccion", lambda: self.db.collection("facturas"))):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def _facturas(self, *docs):
        for i, d in enumerate(docs):
            self.db.c.setdefault("facturas", {})[f"alc-homes-{i}"] = d


class OtroProyecto(Base):
    def test_las_compras_de_otro_proyecto_no_cuentan_en_los_costes(self):
        self._facturas(f("Mobiliario y equipamiento", 3200.0), f("Mantenimiento y reparaciones", 800.0), f("Limpieza y lavandería", 90.0))
        antes = facturas.costes_reales("alc-homes", PRIMAVERA, "2026-08")
        self.assertEqual((antes["grupos"]["otros"], antes["grupos"]["limpieza"]), (4000.0, 90.0))
        asesor.guardar_perfil("alc-homes", PRIMAVERA, {}, None, excluidas=["Mobiliario y equipamiento", "Mantenimiento y reparaciones"])
        despues = facturas.costes_reales("alc-homes", PRIMAVERA, "2026-08")
        self.assertEqual((despues["grupos"]["otros"], despues["grupos"]["limpieza"]), (0.0, 90.0))      # la limpieza sí sigue contando
        self.assertEqual(despues["ajenas"], {"facturas": 2, "total": 4000.0})                              # y se avisa de lo excluido
        self.assertEqual(despues["facturas_costes"], 1)

    def test_una_factura_marcada_de_otro_proyecto_tampoco_cuenta(self):
        self._facturas(f("Mantenimiento y reparaciones", 500.0, proyecto_ajeno=True), f("Mantenimiento y reparaciones", 70.0))
        r = facturas.costes_reales("alc-homes", PRIMAVERA, "2026-08")
        self.assertEqual(r["grupos"]["otros"], 70.0)
        self.assertEqual(r["ajenas"]["facturas"], 1)

    def test_tambien_se_excluyen_del_reparto_de_costes_fijos(self):
        self._facturas(f("Seguros", 1200.0, mes="2026-03", proyecto_ajeno=True))
        r = asesor.costes_fijos_mes("alc-homes", PRIMAVERA, "2026-08", asesor.facturas_de_propiedad("alc-homes", PRIMAVERA), asesor.perfil("alc-homes", PRIMAVERA))
        self.assertEqual(r["conceptos"]["seguro"]["origen"], "falta")

    def test_no_afecta_al_resumen_del_gestor_ni_a_otra_propiedad(self):
        from services import gestor
        self._facturas(f("Mobiliario y equipamiento", 3200.0, proyecto_ajeno=True), f("Mobiliario y equipamiento", 50.0, propiedad=HOSTAL))
        asesor.guardar_perfil("alc-homes", PRIMAVERA, {}, None, excluidas=["Mobiliario y equipamiento"])
        self.assertEqual(gestor.resumen_trimestral("alc-homes", PRIMAVERA, 2026, 3, False)["gastos"]["total"], 3200.0)   # el gestor ve TODAS las facturas
        self.assertEqual(facturas.costes_reales("alc-homes", HOSTAL, "2026-08")["grupos"]["otros"], 50.0)                # el perfil es de cada propiedad

    def test_normalizar_guarda_la_marca(self):
        d, _ = facturas.normalizar({"tipo": "gasto", "proveedor": "X", "fecha": "2026-08-01", "total": 5, "proyecto_ajeno": True})
        self.assertTrue(d["proyecto_ajeno"])
        d, _ = facturas.normalizar({"tipo": "gasto", "proveedor": "X", "fecha": "2026-08-01", "total": 5})
        self.assertFalse(d["proyecto_ajeno"])


class PerfilNuevo(Base):
    def test_excluidas_se_validan_y_completar_se_guarda(self):
        with self.assertRaises(facturas.FacturaError):
            asesor.guardar_perfil("alc-homes", PRIMAVERA, {}, None, excluidas=["Inventada"])
        r = asesor.guardar_perfil("alc-homes", PRIMAVERA, {}, None, excluidas=[], completar=True)
        self.assertEqual((r["excluidas"], r["completar"], r["compras_decididas"]), ([], True, True))      # decidir «son de esta propiedad» también es una decisión
        self.assertTrue(asesor.perfil("alc-homes", PRIMAVERA)["completar"])

    def test_limpieza_con_todos_los_modos(self):
        for lim, esperado in (({"modo": "estancia", "importe": 72}, {"modo": "estancia", "importe": 72.0}), ({"modo": "fija", "importe": "150,5"}, {"modo": "fija", "importe": 150.5}),
                              ({"modo": "externa"}, {"modo": "externa", "importe": 0.0}), ({"modo": "no_aplica"}, {"modo": "no_aplica", "importe": 0.0}),
                              ({"modo": "hora", "importe": 12, "horas_min": 1, "horas_max": 4}, {"modo": "hora", "importe": 12.0, "horas_min": 1.0, "horas_max": 4.0})):
            self.assertEqual(asesor.guardar_perfil("alc-homes", PRIMAVERA, {}, lim)["limpieza"], esperado)
        for malo in ({"modo": "diaria", "importe": 5}, {"modo": "fija", "importe": -1}, {"modo": "fija"}, {"modo": "hora", "importe": 5, "horas_min": 99}):
            with self.assertRaises(facturas.FacturaError):
                asesor.guardar_perfil("alc-homes", PRIMAVERA, {}, malo)

    def test_estado_trae_las_compras_y_las_ideas_de_limpieza(self):
        self._facturas(f("Mobiliario y equipamiento", 3200.0), f("Mobiliario y equipamiento", 100.0), f("Mantenimiento y reparaciones", 800.0), f("Seguros", 600.0, mes="2026-03"))
        e = asesor.estado("alc-homes", PRIMAVERA, False, "2026-08")
        self.assertEqual((e["compras"]["facturas"], e["compras"]["total"], e["compras"]["decidido"]), (3, 4100.0, False))
        self.assertEqual(e["compras"]["categorias"][0], {"categoria": "Mobiliario y equipamiento", "facturas": 2, "total": 3300.0})        # la de más importe primero
        self.assertEqual((e["limpieza"]["facturas"], e["limpieza"]["modo_habitual"], e["limpieza"]["importe_habitual"]), (0, "estancia", 69.0))
        self.assertTrue(any("69" in i for i in e["limpieza"]["ideas"]))                                                                       # parte de la estimación de siempre
        self.assertEqual(e["estimacion_anual_habitual"], 3100.0)
        h = asesor.estado("alc-homes", HOSTAL, True, "2026-08")
        self.assertEqual((h["limpieza"]["modo_habitual"], h["limpieza"]["horas_habituales"], h["estimacion_anual_habitual"]), ("hora", [1.0, 3.0], 5700.0))


RES = {"reservas": 5, "ingresos_brutos": 1000.0, "ingresos_netos": 850.0}


def reales(**extra):
    base = {"facturas": 0, "facturas_costes": 0, "grupos": {"limpieza": 0.0, "fijos": 0.0, "otros": 0.0}, "plataformas": 0.0, "iva_soportado": 0.0, "fijos_detalle": {}, "faltan_fijos": [],
            "hay_datos_fijos": True, "limpieza_cfg": None, "completar_con_estimacion": False, "ajenas": {"facturas": 0, "total": 0.0}}
    return {**base, **extra}


class LimpiezaEstimada(unittest.TestCase):
    def _lim(self, prop, cfg, ocupacion=0.0, **extra):
        r = mr._rentabilidad(prop, RES, 31, ocupacion, reales(limpieza_cfg=cfg, **extra))
        return r["costes_limpieza"], r["limpieza_origen"]

    def test_por_estancia_cuota_fija_externa_y_sin_limpieza(self):
        self.assertEqual(self._lim(PRIMAVERA, {"modo": "estancia", "importe": 80})[0], 400.0)
        self.assertEqual(self._lim(PRIMAVERA, {"modo": "fija", "importe": 150})[0], 150.0)
        self.assertEqual(self._lim(PRIMAVERA, {"modo": "externa", "importe": 0})[1], "sin facturas de limpieza este mes")
        self.assertEqual(self._lim(PRIMAVERA, {"modo": "externa", "importe": 0})[0], 0.0)
        self.assertEqual(self._lim(PRIMAVERA, {"modo": "no_aplica", "importe": 0}), (0.0, "sin limpieza"))

    def test_por_horas_del_hostal_segun_la_ocupacion(self):
        sin_ocupacion = self._lim(HOSTAL, {"modo": "hora", "importe": 10, "horas_min": 1, "horas_max": 3}, ocupacion=0.0)[0]
        llena = self._lim(HOSTAL, {"modo": "hora", "importe": 10, "horas_min": 1, "horas_max": 3}, ocupacion=100.0)[0]
        self.assertEqual((sin_ocupacion, llena), (310.0, 930.0))                      # 1 h × 10 € × 31 días  y  3 h × 10 € × 31 días
        self.assertEqual(self._lim(HOSTAL, {"modo": "hora", "importe": 10}, ocupacion=50.0)[0], round(2 * 10 * 31, 2))      # sin horas: las de siempre (1 a 3)

    def test_por_horas_en_la_casa_necesita_las_horas_de_cada_estancia(self):
        self.assertEqual(self._lim(PRIMAVERA, {"modo": "hora", "importe": 12, "horas_min": 4})[0], round(4 * 12 * 5, 2))
        self.assertEqual(self._lim(PRIMAVERA, {"modo": "hora", "importe": 12})[0], 345.0)                                    # sin horas: la estimación de siempre

    def test_con_facturas_de_limpieza_manda_lo_real(self):
        c, o = self._lim(PRIMAVERA, {"modo": "estancia", "importe": 80}, grupos={"limpieza": 120.0, "fijos": 0.0, "otros": 0.0})
        self.assertEqual((c, o), (120.0, "facturas"))


class CompletarConEstimacion(unittest.TestCase):
    def test_rellena_hasta_la_estimacion_anual_solo_si_faltan_conceptos(self):
        est = round(3100 / 365 * 31, 2)
        r = mr._rentabilidad(PRIMAVERA, RES, 31, 0.0, reales(grupos={"limpieza": 0.0, "fijos": 100.0, "otros": 0.0}, faltan_fijos=["ibi"], completar_con_estimacion=True))
        self.assertEqual(r["costes_fijos"], est)
        self.assertEqual(r["fijos_completado_con_estimacion"], round(est - 100.0, 2))
        sin_flag = mr._rentabilidad(PRIMAVERA, RES, 31, 0.0, reales(grupos={"limpieza": 0.0, "fijos": 100.0, "otros": 0.0}, faltan_fijos=["ibi"]))
        self.assertEqual((sin_flag["costes_fijos"], sin_flag["fijos_completado_con_estimacion"]), (100.0, 0.0))
        completo = mr._rentabilidad(PRIMAVERA, RES, 31, 0.0, reales(grupos={"limpieza": 0.0, "fijos": 100.0, "otros": 0.0}, faltan_fijos=[], completar_con_estimacion=True))
        self.assertEqual(completo["costes_fijos"], 100.0)                              # con todos los conceptos contestados no se añade nada
        sobra = mr._rentabilidad(PRIMAVERA, RES, 31, 0.0, reales(grupos={"limpieza": 0.0, "fijos": 900.0, "otros": 0.0}, faltan_fijos=["ibi"], completar_con_estimacion=True))
        self.assertEqual(sobra["costes_fijos"], 900.0)                                 # si lo real ya supera la estimación, no se toca

    def test_la_respuesta_trae_las_facturas_excluidas(self):
        r = mr._rentabilidad(PRIMAVERA, RES, 31, 0.0, reales(ajenas={"facturas": 3, "total": 4000.0}))
        self.assertEqual(r["ajenas"], {"facturas": 3, "total": 4000.0})


class Endpoint(Base):
    PIN = "1111"

    def test_las_respuestas_aceptan_excluidas_y_completar(self):
        with mock.patch.object(mr, "MOBILE_APP_PIN", self.PIN), mock.patch.object(mr, "MOBILE_APP_PIN_ADMIN", "9999"):
            app = Flask(__name__)
            app.register_blueprint(mobile_facturas.facturas_bp)
            c = app.test_client()
            self._facturas(f("Mobiliario y equipamiento", 3200.0))
            r = c.post("/mobile/facturas/asesor/respuestas", headers={"x-app-pin": self.PIN}, json={
                "propiedad_id": PRIMAVERA, "mes": "2026-08", "conceptos": {}, "excluidas": ["Mobiliario y equipamiento"], "completar": True,
                "limpieza": {"modo": "estancia", "importe": 75}}).get_json()
            self.assertTrue(r["ok"])
            self.assertEqual((r["compras"]["excluidas"], r["compras"]["decidido"], r["completar"]), (["Mobiliario y equipamiento"], True, True))
            self.assertEqual(r["limpieza"]["declarado"], {"modo": "estancia", "importe": 75.0})
            malo = c.post("/mobile/facturas/asesor/respuestas", headers={"x-app-pin": self.PIN}, json={"propiedad_id": PRIMAVERA, "excluidas": ["Inventada"]})
            self.assertEqual(malo.status_code, 400)


if __name__ == "__main__":
    unittest.main()

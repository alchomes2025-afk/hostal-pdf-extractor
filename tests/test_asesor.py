"""Asesor Contable: costes fijos por concepto (facturas repartidas por los meses que cubren → lo declarado → falta) y su efecto en la rentabilidad."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_facturas
import mobile_routes as mr
import config
from services import asesor, facturas


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
    def stream(self):
        return [type("D", (), {"id": k, "to_dict": lambda s, v=v: dict(v)})() for k, v in self.a.items() if all(v.get(c) == x for c, x in self.f)]


class FakeDB:
    def __init__(self): self.colecciones = {}
    def collection(self, n): return Col(self.colecciones.setdefault(n, {}))


def f(categoria, total, mes, cuota=0.0, propiedad="349341", cobertura=None, **extra):
    d = {"cliente": "alc-homes", "propiedad_id": propiedad, "tipo": "gasto", "categoria": categoria, "total": total, "cuota_iva": cuota, "mes": mes, "fecha": mes + "-10"}
    if cobertura:
        d["cobertura_meses"] = cobertura
    return {**d, **extra}


class Devengo(unittest.TestCase):
    def test_un_seguro_anual_se_reparte_en_12_meses_desde_el_mes_de_la_factura(self):
        seguro = f("Seguros", 1200.0, "2026-03")                                  # sin cobertura indicada: lo habitual de los seguros, 12 meses
        self.assertEqual(asesor.devengo(seguro, "2026-02"), 0.0)                 # antes de la factura no cuenta
        self.assertEqual(asesor.devengo(seguro, "2026-03"), 100.0)
        self.assertEqual(asesor.devengo(seguro, "2027-02"), 100.0)
        self.assertEqual(asesor.devengo(seguro, "2027-03"), 0.0)                 # ya terminó

    def test_el_agua_cada_dos_meses_y_los_abonos_restan(self):
        agua = f("Suministros (luz, agua, gas, internet)", 90.0, "2026-08", cobertura=2)
        self.assertEqual([asesor.devengo(agua, m) for m in ("2026-07", "2026-08", "2026-09", "2026-10")], [0.0, 45.0, 45.0, 0.0])
        self.assertEqual(asesor.devengo(f("Seguros", 120.0, "2026-08", es_rectificativa=True), "2026-08"), -10.0)

    def test_cobertura_no_valida_cae_al_habitual(self):
        self.assertEqual(asesor.cobertura(f("Seguros", 1, "2026-01", cobertura=7)), 12)
        self.assertEqual(asesor.cobertura(f("Alquiler", 1, "2026-01", cobertura="x")), 1)

    def test_normalizar_guarda_la_cobertura(self):
        d, _ = facturas.normalizar({"tipo": "gasto", "proveedor": "Mapfre", "fecha": "2026-03-01", "total": 1200, "categoria": "Seguros"})
        self.assertEqual(d["cobertura_meses"], 12)
        d, _ = facturas.normalizar({"tipo": "gasto", "proveedor": "Aguas", "fecha": "2026-03-01", "total": 90, "categoria": "Suministros (luz, agua, gas, internet)", "cobertura_meses": "2"})
        self.assertEqual(d["cobertura_meses"], 2)
        d, _ = facturas.normalizar({"tipo": "ingreso", "proveedor": "X", "fecha": "2026-03-01", "total": 9, "categoria": "Alojamiento"})
        self.assertEqual(d["cobertura_meses"], 1)


class CostesFijos(unittest.TestCase):
    def test_facturas_declarado_y_falta(self):
        facs = [f("Seguros", 1200.0, "2026-03"), f("Suministros (luz, agua, gas, internet)", 120.0, "2026-08", cobertura=1)]
        perfil = {"conceptos": {"ibi": {"modo": "anual", "importe": 900.0}, "alquiler": {"modo": "no_aplica", "importe": 0.0}, "seguro": {"modo": "anual", "importe": 5000.0}}, "limpieza": None}
        r = asesor.costes_fijos_mes("alc-homes", "349341", "2026-08", facs, perfil)
        c = r["conceptos"]
        self.assertEqual((c["seguro"]["origen"], c["seguro"]["importe"]), ("facturas", 100.0))      # la factura manda sobre lo declarado
        self.assertEqual((c["suministros"]["origen"], c["suministros"]["importe"]), ("facturas", 120.0))
        self.assertEqual((c["ibi"]["origen"], c["ibi"]["importe"]), ("declarado", 75.0))              # 900 / 12
        self.assertEqual(c["alquiler"]["origen"], "no_aplica")
        self.assertEqual(c["comunidad"]["origen"], "falta")
        self.assertIn("comunidad", r["faltan"])
        self.assertNotIn("ibi", r["faltan"])
        self.assertEqual(r["total"], round(100 + 120 + 75, 2))
        self.assertTrue(r["hay_datos"])

    def test_lo_declarado_cubre_los_meses_sin_factura(self):
        facs = [f("Seguros", 1200.0, "2026-03")]
        perfil = {"conceptos": {"seguro": {"modo": "anual", "importe": 1200.0}}, "limpieza": None}
        self.assertEqual(asesor.costes_fijos_mes("alc-homes", "349341", "2027-05", facs, perfil)["conceptos"]["seguro"], {"nombre": "Seguros (comercio, hogar, responsabilidad civil)", "importe": 100.0, "origen": "declarado", "facturas": 0})

    def test_sin_nada_todo_falta(self):
        r = asesor.costes_fijos_mes("alc-homes", "349341", "2026-08", [], {"conceptos": {}, "limpieza": None})
        self.assertEqual((r["total"], r["hay_datos"], len(r["faltan"])), (0.0, False, len(asesor.CONCEPTOS_FIJOS)))

    def test_el_personal_solo_se_conoce_por_lo_declarado(self):
        r = asesor.costes_fijos_mes("alc-homes", "349341", "2026-08", [], {"conceptos": {"personal": {"modo": "mensual", "importe": 1800.0}}, "limpieza": None})
        self.assertEqual(r["conceptos"]["personal"]["importe"], 1800.0)

    def test_facturas_de_ingreso_y_de_otra_categoria_no_cuentan(self):
        facs = [f("Alojamiento", 500.0, "2026-08", tipo="ingreso"), f("Mantenimiento y reparaciones", 300.0, "2026-08")]
        r = asesor.costes_fijos_mes("alc-homes", "349341", "2026-08", facs, {"conceptos": {}, "limpieza": None})
        self.assertEqual(r["total"], 0.0)


class Perfil(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        p = mock.patch.object(config, "db", self.db)
        p.start()
        self.addCleanup(p.stop)

    def test_guarda_mezcla_y_valida(self):
        asesor.guardar_perfil("alc-homes", "349341", {"ibi": {"modo": "anual", "importe": "900,50"}}, None, "Ione")
        asesor.guardar_perfil("alc-homes", "349341", {"alquiler": {"modo": "no_aplica"}}, {"modo": "estancia", "importe": 72}, "Ione")
        p = asesor.perfil("alc-homes", "349341")
        self.assertEqual(p["conceptos"]["ibi"], {"modo": "anual", "importe": 900.5})
        self.assertEqual(p["conceptos"]["alquiler"], {"modo": "no_aplica", "importe": 0.0})
        self.assertEqual(p["limpieza"], {"modo": "estancia", "importe": 72.0})
        for malo in ({"inventado": {"modo": "anual", "importe": 1}}, {"ibi": {"modo": "diario", "importe": 1}}, {"ibi": {"modo": "anual", "importe": -5}}, {"ibi": {"modo": "anual", "importe": "x"}}, {"ibi": "900"}):
            with self.assertRaises(facturas.FacturaError):
                asesor.guardar_perfil("alc-homes", "349341", malo, None)
        with self.assertRaises(facturas.FacturaError):
            asesor.guardar_perfil("alc-homes", "349341", {}, {"modo": "semana", "importe": 5})

    def test_perfiles_separados_por_propiedad(self):
        asesor.guardar_perfil("alc-homes", "349341", {"ibi": {"modo": "anual", "importe": 100}}, None)
        self.assertEqual(asesor.perfil("alc-homes", "339751")["conceptos"], {})


class Rentabilidad(unittest.TestCase):
    RESUMEN = {"reservas": 5, "ingresos_brutos": 1000.0, "ingresos_netos": 850.0}

    def _reales(self, **extra):
        base = {"facturas": 0, "facturas_costes": 0, "grupos": {"limpieza": 0.0, "fijos": 0.0, "otros": 0.0}, "plataformas": 0.0, "iva_soportado": 0.0,
                "fijos_detalle": {}, "faltan_fijos": ["ibi"], "hay_datos_fijos": False, "limpieza_cfg": None}
        return {**base, **extra}

    def test_con_datos_de_fijos_aunque_no_haya_facturas_del_mes_son_reales(self):
        r = mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, self.RESUMEN, 31, 0.0, self._reales(grupos={"limpieza": 0.0, "fijos": 175.0, "otros": 0.0}, hay_datos_fijos=True, faltan_fijos=["comunidad"]))
        self.assertEqual(r["origen_costes"], "reales")
        self.assertEqual(r["costes_fijos"], 175.0)
        self.assertEqual(r["limpieza_origen"], "estimada")                      # sin facturas de limpieza del mes se estima
        self.assertEqual(r["costes_limpieza"], 345.0)
        self.assertEqual(r["faltan_fijos"], ["comunidad"])

    def test_la_limpieza_declarada_sustituye_a_la_habitual(self):
        r = mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, self.RESUMEN, 31, 0.0, self._reales(hay_datos_fijos=True, limpieza_cfg={"modo": "estancia", "importe": 80}))
        self.assertEqual(r["costes_limpieza"], 400.0)                           # 5 estancias × 80
        r = mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, self.RESUMEN, 31, 0.0, self._reales(hay_datos_fijos=True, limpieza_cfg={"modo": "hora", "importe": 80}))
        self.assertEqual(r["costes_limpieza"], 345.0)                           # un modo que no es el de la propiedad se ignora

    def test_limpieza_con_facturas_manda(self):
        r = mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, self.RESUMEN, 31, 0.0, self._reales(facturas_costes=1, grupos={"limpieza": 120.0, "fijos": 0.0, "otros": 0.0}))
        self.assertEqual((r["costes_limpieza"], r["limpieza_origen"]), (120.0, "facturas"))

    def test_sin_ningun_dato_sigue_la_estimacion_de_siempre(self):
        r = mr._rentabilidad(mr.PROPERTY_ID_CASA_PRIMAVERA, self.RESUMEN, 31, 0.0, self._reales())
        self.assertEqual(r["origen_costes"], "estimados")
        self.assertEqual(r["costes_fijos"], round(3100 / 365 * 31, 2))


class Endpoints(unittest.TestCase):
    PIN, ADMIN = "1111", "1379"

    def setUp(self):
        self.db = FakeDB()
        self.db.colecciones["facturas"] = {"alc-homes-1": f("Seguros", 1200.0, "2026-03", usuario="x")}
        for o, n, v in ((config, "db", self.db), (mobile_facturas.facturas, "_coleccion", lambda: self.db.collection("facturas")),
                        (mobile_facturas.asesor.fac, "_coleccion", lambda: self.db.collection("facturas")),
                        (__import__("mobile_routes"), "MOBILE_APP_PIN", self.PIN), (__import__("mobile_routes"), "MOBILE_APP_PIN_ADMIN", self.ADMIN)):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(mobile_facturas.facturas_bp)
        self.cliente = app.test_client()

    def test_estado_y_respuestas(self):
        h = {"x-app-pin": self.ADMIN}
        r = self.cliente.get("/mobile/facturas/asesor/estado?propiedad_id=349341&mes=2026-08", headers=h).get_json()
        self.assertTrue(r["ok"])
        por_id = {c["id"]: c for c in r["conceptos"]}
        self.assertEqual((por_id["seguro"]["origen"], por_id["seguro"]["mensual"]), ("facturas", 100.0))
        self.assertEqual(por_id["ibi"]["origen"], "falta")
        r = self.cliente.post("/mobile/facturas/asesor/respuestas", headers=h, json={"propiedad_id": "349341", "mes": "2026-08", "usuario": "Ione",
                                                                                      "conceptos": {"ibi": {"modo": "anual", "importe": 900}}}).get_json()
        por_id = {c["id"]: c for c in r["conceptos"]}
        self.assertEqual((por_id["ibi"]["origen"], por_id["ibi"]["mensual"]), ("declarado", 75.0))
        self.assertNotIn("ibi", r["faltan"])

    def test_permisos_y_validaciones(self):
        self.assertEqual(self.cliente.get("/mobile/facturas/asesor/estado?propiedad_id=349341").status_code, 401)
        self.assertEqual(self.cliente.get("/mobile/facturas/asesor/estado?propiedad_id=349341", headers={"x-app-pin": self.PIN}).status_code, 403)   # el PIN normal ya no entra
        h = {"x-app-pin": self.ADMIN}
        self.assertEqual(self.cliente.get("/mobile/facturas/asesor/estado?propiedad_id=339751", headers={"x-app-pin": self.ADMIN}).status_code, 200)
        self.assertEqual(self.cliente.get("/mobile/facturas/asesor/estado?propiedad_id=349341&mes=2026-8", headers=h).status_code, 400)
        r = self.cliente.post("/mobile/facturas/asesor/respuestas", headers={"x-app-pin": self.PIN}, json={"propiedad_id": "339751", "conceptos": {"ibi": {"modo": "anual", "importe": 1}}})
        self.assertEqual(r.status_code, 403)
        r = self.cliente.post("/mobile/facturas/asesor/respuestas", headers=h, json={"propiedad_id": "349341", "conceptos": {"ibi": {"modo": "anual", "importe": -1}}})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()

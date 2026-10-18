"""Resumen trimestral para el gestor: totales por categoría, IVA, avisos, hoja de Google y permisos."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_facturas
import mobile_routes
from services import asesor, facturas, gestor, puente_plataforma


def f(categoria, total, mes, base=None, cuota=0.0, tipo="gasto", propiedad="349341", **extra):
    return {"cliente": "alc-homes", "propiedad_id": propiedad, "tipo": tipo, "categoria": categoria, "total": total, "base_imponible": base if base is not None else round(total - cuota, 2),
            "cuota_iva": cuota, "mes": mes, "fecha": mes + "-10", "proveedor": extra.pop("proveedor", "Proveedor " + categoria), "nif": extra.pop("nif", "B12345678"), "numero": extra.pop("numero", "1"),
            "url": "https://drive/x", **extra}


DOCS = [f("Suministros (luz, agua, gas, internet)", 121.0, "2026-07", cuota=21.0, proveedor="Iberdrola", numero="10"),
        f("Suministros (luz, agua, gas, internet)", 60.5, "2026-08", cuota=10.5, proveedor="Iberdrola", numero="11"),
        f("Mantenimiento y reparaciones", 242.0, "2026-09", cuota=42.0, proveedor="Fontanero", numero="12"),
        f("Mobiliario y equipamiento", 12.1, "2026-09", cuota=2.1, es_rectificativa=True, proveedor="IKEA", numero="13"),
        f("Comisiones de plataformas", 698.23, "2026-09", base=698.23, cuota=0.0, proveedor="Booking.com B.V.", nif="NL805734958B01", numero="14"),
        f("Alojamiento", 110.0, "2026-08", cuota=10.0, tipo="ingreso", proveedor="Cliente directo", numero="15"),
        f("Alquiler", 500.0, "2026-10"), f("Alquiler", 77.0, "2026-08", propiedad="339751")]


class Datos(unittest.TestCase):
    def setUp(self):
        for o, n, v in ((asesor, "facturas_de_propiedad", lambda c, p: [d for d in DOCS if d["propiedad_id"] == str(p)]),):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def test_totales_por_categoria_e_iva(self):
        r = gestor.resumen_trimestral("alc-homes", "349341", 2026, 3, False)
        self.assertEqual(r["meses"], ["2026-07", "2026-08", "2026-09"])
        self.assertEqual(r["gastos"]["facturas"], 5)
        self.assertEqual(r["gastos"]["total"], round(121 + 60.5 + 242 - 12.1 + 698.23, 2))        # el abono resta; Booking SÍ entra en el resumen del gestor
        self.assertEqual(r["gastos"]["iva"], round(21 + 10.5 + 42 - 2.1, 2))
        cats = {c["categoria"]: c for c in r["gastos"]["por_categoria"]}
        self.assertEqual(cats["Suministros (luz, agua, gas, internet)"], {"categoria": "Suministros (luz, agua, gas, internet)", "facturas": 2, "base": 150.0, "iva": 31.5, "total": 181.5})
        self.assertEqual(cats["Mobiliario y equipamiento"]["total"], -12.1)
        self.assertEqual((r["ingresos"]["facturas"], r["ingresos"]["iva"]), (1, 10.0))
        self.assertEqual(r["iva"]["soportado"], 71.4)
        self.assertEqual(r["iva"]["saldo_estimado"], round(10.0 - 71.4, 2))
        self.assertEqual(len(r["detalle"]), 6)                                                    # el alquiler de octubre y el del hostal quedan fuera
        self.assertEqual([d["fecha"] for d in r["detalle"]], sorted(d["fecha"] for d in r["detalle"]))

    def test_reservas_y_iva_repercutido_estimado(self):
        reservas = {"2026-07": {"brutos": 1100.0, "comisiones": 100.0, "netos": 1000.0}, "2026-09": {"brutos": 550.0, "comisiones": 0, "netos": 550.0}}
        r = gestor.resumen_trimestral("alc-homes", "349341", 2026, 3, False, reservas)
        self.assertEqual([x["mes"] for x in r["reservas"]], ["2026-07", "2026-09"])
        self.assertEqual(r["iva"]["repercutido_estimado_reservas"], round(1650 - 1650 / 1.10, 2))   # 150
        self.assertEqual(r["iva"]["saldo_estimado"], round(10.0 + 150.0 - 71.4, 2))

    def test_avisos(self):
        docs = DOCS + [f("Otros gastos", 50.0, "2026-08", base=50.0, cuota=0.0, nif="", numero="")]
        with mock.patch.object(asesor, "facturas_de_propiedad", lambda c, p: [d for d in docs if d["propiedad_id"] == str(p)]):
            texto = " ".join(gestor.resumen_trimestral("alc-homes", "349341", 2026, 3, False)["avisos"])
        self.assertIn("no tienen NIF", texto)
        self.assertIn("no tienen número", texto)
        self.assertIn("no llevan IVA", texto)
        self.assertIn("inversión del sujeto pasivo", texto)                                         # Booking.com B.V.
        self.assertIn("rectificativas", texto)
        self.assertIn("Solo cuentan las facturas", texto)

    def test_trimestre_vacio_y_no_valido(self):
        r = gestor.resumen_trimestral("alc-homes", "349341", 2026, 1, False)
        self.assertEqual(r["gastos"]["facturas"], 0)
        self.assertIn("no tiene facturas guardadas", " ".join(r["avisos"]))
        for t in (0, 5):
            with self.assertRaises(facturas.FacturaError):
                gestor.resumen_trimestral("alc-homes", "349341", 2026, t, False)

    def test_permisos_por_propiedad(self):
        with self.assertRaises(facturas.FacturaError) as e:
            gestor.resumen_trimestral("alc-homes", "339751", 2026, 3, False)
        self.assertEqual(e.exception.status, 403)
        self.assertEqual(gestor.resumen_trimestral("alc-homes", "339751", 2026, 3, True)["gastos"]["facturas"], 1)


class Hoja(unittest.TestCase):
    def setUp(self):
        for o, n, v in ((asesor, "facturas_de_propiedad", lambda c, p: [d for d in DOCS if d["propiedad_id"] == str(p)]), (facturas, "habilitado", lambda c: True),
                        (facturas, "raiz_drive", lambda c: "RAIZ"), (puente_plataforma, "resumen", self._resumen)):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)
        self.enviado = {}

    def _resumen(self, raiz, ruta, titulo, hojas):
        self.enviado = {"raiz": raiz, "ruta": ruta, "titulo": titulo, "hojas": hojas}
        return {"url": "https://docs.google.com/spreadsheets/d/X"}

    def test_genera_las_pestanas_resumen_y_detalle(self):
        r = gestor.generar_hoja("alc-homes", "349341", 2026, 3, False)
        self.assertEqual(r, {"url": "https://docs.google.com/spreadsheets/d/X", "titulo": "Resumen 2026-3T para el gestor"})
        self.assertEqual(self.enviado["ruta"], ["Facturas", "La Casa de la Primavera", "2026"])
        self.assertEqual(set(self.enviado["hojas"]), {"Resumen", "Detalle"})
        detalle = self.enviado["hojas"]["Detalle"]
        self.assertEqual(detalle[0][0], "Fecha")
        self.assertEqual(len(detalle), 1 + 6)
        planas = [str(c) for fila in self.enviado["hojas"]["Resumen"] for c in fila]
        self.assertTrue(any("TOTAL GASTOS" in c for c in planas))
        self.assertTrue(any("IVA soportado" in c for c in planas))

    def test_error_del_drive(self):
        with mock.patch.object(puente_plataforma, "resumen", side_effect=puente_plataforma.PuenteError("caído")):
            with self.assertRaises(facturas.FacturaError) as e:
                gestor.generar_hoja("alc-homes", "349341", 2026, 3, False)
        self.assertEqual(e.exception.status, 502)


class Endpoints(unittest.TestCase):
    PIN, ADMIN = "1111", "9999"

    def setUp(self):
        for o, n, v in ((asesor, "facturas_de_propiedad", lambda c, p: [d for d in DOCS if d["propiedad_id"] == str(p)]), (facturas, "habilitado", lambda c: True),
                        (facturas, "raiz_drive", lambda c: "RAIZ"), (puente_plataforma, "resumen", lambda *a: {"url": "https://hoja"}),
                        (mobile_routes, "MOBILE_APP_PIN", self.PIN), (mobile_routes, "MOBILE_APP_PIN_ADMIN", self.ADMIN),
                        (mobile_facturas, "_ingresos_reservas", lambda p, m: {"2026-07": {"brutos": 1100.0, "comisiones": 100.0, "netos": 1000.0}})):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(mobile_facturas.facturas_bp)
        self.cliente = app.test_client()

    def test_resumen(self):
        r = self.cliente.get("/mobile/facturas/resumen-trimestral?propiedad_id=349341&anio=2026&trimestre=3&reservas=1", headers={"x-app-pin": self.PIN}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["reservas"][0]["mes"], "2026-07")
        sin = self.cliente.get("/mobile/facturas/resumen-trimestral?propiedad_id=349341&anio=2026&trimestre=3", headers={"x-app-pin": self.PIN}).get_json()
        self.assertIsNone(sin["reservas"])

    def test_hoja_permisos_y_validaciones(self):
        h = {"x-app-pin": self.PIN}
        ok = self.cliente.post("/mobile/facturas/resumen-trimestral/hoja", headers=h, json={"propiedad_id": "349341", "anio": 2026, "trimestre": 3})
        self.assertEqual(ok.get_json()["url"], "https://hoja")
        self.assertEqual(self.cliente.post("/mobile/facturas/resumen-trimestral/hoja", json={"propiedad_id": "349341", "anio": 2026, "trimestre": 3}).status_code, 401)
        self.assertEqual(self.cliente.post("/mobile/facturas/resumen-trimestral/hoja", headers=h, json={"propiedad_id": "339751", "anio": 2026, "trimestre": 3}).status_code, 403)
        self.assertEqual(self.cliente.post("/mobile/facturas/resumen-trimestral/hoja", headers=h, json={"propiedad_id": "349341", "anio": "x", "trimestre": 3}).status_code, 400)
        self.assertEqual(self.cliente.get("/mobile/facturas/resumen-trimestral?propiedad_id=349341&anio=2026&trimestre=9", headers=h).status_code, 400)


if __name__ == "__main__":
    unittest.main()

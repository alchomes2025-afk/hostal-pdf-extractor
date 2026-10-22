"""Comisión de gestión (15 % de los ingresos netos), solo ALC Homes, y su desglose con PIN propio."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_routes
from services import reparto


def importes(d):
    return {(x["nombre"], x["concepto"]): x["importe"] for x in d["partes"]}


class Reparto(unittest.TestCase):
    def test_primavera_80_20(self):
        d = reparto.desglose_primavera(100)
        self.assertEqual([x["importe"] for x in d["partes"]], [80.0, 20.0])

    def test_hostal_sobre_100(self):
        d = reparto.desglose_hostal(100)
        self.assertEqual([x["importe"] for x in d["partes"]], [30.0, 14.0, 39.2, 16.8])
        self.assertEqual({x["nombre"]: x["importe"] for x in d["totales"]}, {"Ione": 69.2, "Adrián": 14.0, "Conchi": 16.8})

    def test_siempre_suma_exactamente_la_comision(self):
        for c in (0, 0.01, 1, 33.33, 123.45, 999.99, 4567.89):
            for fn in (reparto.desglose_primavera, reparto.desglose_hostal):
                d = fn(c)
                self.assertEqual(round(sum(x["importe"] for x in d["partes"]), 2), round(c, 2), (fn.__name__, c))
            self.assertEqual(round(sum(x["importe"] for x in reparto.desglose_hostal(c)["totales"]), 2), round(c, 2))


class Informe(unittest.TestCase):
    RESUMEN = {"reservas": 3, "ingresos_brutos": 1000.0, "comisiones": 150.0, "ingresos_netos": 850.0}

    def _rent(self, activas=True, prop="349341"):
        with mock.patch.object(mobile_routes, "COMISIONES_GESTION_ACTIVAS", activas):
            return mobile_routes._rentabilidad(prop, self.RESUMEN, 30, 50, None)

    def test_gestion_es_15_por_ciento_de_los_netos(self):
        r = self._rent()
        self.assertEqual(r["comision_gestion"], 127.5)      # 15 % de 850, no de 1000
        self.assertEqual(r["comision_blockademy"], 50.0)    # sin cambios: 5 % de los brutos
        self.assertTrue(r["comisiones_activas"])

    def test_otros_clientes_no_las_ven(self):
        r = self._rent(activas=False)
        self.assertFalse(r["comisiones_activas"])
        self.assertEqual((r["comision_gestion"], r["comision_blockademy"]), (0.0, 0.0))
        self.assertAlmostEqual(r["beneficio_antes_impuestos"], self._rent()["beneficio_antes_impuestos"] + 127.5 + 50.0, places=2)


class Endpoint(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(mobile_routes.mobile_bp)
        self.c = app.test_client()
        self.intentos = {"n": 0}
        self.llamadas_informe = []
        mobile_routes._COMISION_RECIENTE.clear()
        informe = {"ok": True, "resumen": Informe.RESUMEN, "rentabilidad": {"comisiones_activas": True, "comision_gestion": 100.0, "comision_gestion_pct": 15}}
        for o, n, v in ((mobile_routes, "MOBILE_APP_PIN", "1111"), (mobile_routes, "MOBILE_APP_PIN_ADMIN", "9999"),
                        (mobile_routes, "_informe_financiero", lambda *a, **k: self.llamadas_informe.append(a) or informe),
                        (mobile_routes, "COMISIONES_GESTION_ACTIVAS", True),
                        (sys.modules["services.limites"], "cantidad", lambda clave, seg: self.intentos["n"]),
                        (sys.modules["services.limites"], "anotar", lambda clave: self.intentos.__setitem__("n", self.intentos["n"] + 1))):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def _post(self, pin_app="1111", pin_desglose="1379", prop="349341"):
        return self.c.post("/mobile/finance/desglose", json={"propertyId": prop, "month": "2026-09", "pin_desglose": pin_desglose}, headers={"x-app-pin": pin_app})

    def test_con_el_pin_correcto_da_el_reparto_de_primavera(self):
        r = self._post()
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual([x["importe"] for x in d["partes"]], [80.0, 20.0])
        self.assertEqual(d["propiedad"], "primavera")

    def test_pin_incorrecto_no_da_cifras(self):
        r = self._post(pin_desglose="0000")
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("partes", r.get_json())

    def test_se_bloquea_tras_5_fallos(self):
        for _ in range(5):
            self._post(pin_desglose="0000")
        self.assertEqual(self._post().status_code, 429)

    def test_pin_de_la_app_incorrecto(self):
        self.assertEqual(self._post(pin_app="0").status_code, 401)

    def test_el_hostal_solo_con_pin_de_administrador(self):
        self.assertEqual(self._post(prop=mobile_routes.PROPERTY_ID).status_code, 403)
        r = self._post(pin_app="9999", prop=mobile_routes.PROPERTY_ID)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["importe"] for x in r.get_json()["partes"]], [30.0, 14.0, 39.2, 16.8])

    def test_otros_clientes_no_tienen_la_funcion(self):
        with mock.patch.object(mobile_routes, "COMISIONES_GESTION_ACTIVAS", False):
            self.assertEqual(self._post().status_code, 404)


    def test_reutiliza_la_comision_que_acaba_de_mostrar_finanzas(self):
        mobile_routes._recordar_comision("349341", "2026-09", {"completo": True, "resumen": Informe.RESUMEN, "rentabilidad": {"comisiones_activas": True, "comision_gestion": 127.5}})
        d = self._post().get_json()
        self.assertEqual(self.llamadas_informe, [])          # no recalcula el mes contra Beds24
        self.assertEqual([x["importe"] for x in d["partes"]], [102.0, 25.5])

    def test_si_lo_recordado_es_viejo_o_incompleto_recalcula(self):
        mobile_routes._recordar_comision("349341", "2026-09", {"completo": False, "resumen": Informe.RESUMEN, "rentabilidad": {"comisiones_activas": True, "comision_gestion": 127.5}})
        self._post()
        self.assertEqual(len(self.llamadas_informe), 1)
        mobile_routes._COMISION_RECIENTE[("349341", "2026-09")] = (0, 850.0, 127.5)   # de hace mucho
        self._post()
        self.assertEqual(len(self.llamadas_informe), 2)


if __name__ == "__main__":
    unittest.main()

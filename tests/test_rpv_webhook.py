"""Webhook de RPV (Svix): firma, deduplicado, avisos de prueba y refresco de la copia de RPV."""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import rpv, rpv_webhook

SECRETO_BYTES = b"secreto-de-prueba-0123456789abcd"
SECRETO = "whsec_" + base64.b64encode(SECRETO_BYTES).decode()
OTRO = "whsec_" + base64.b64encode(b"otro-secreto-distinto-0123456789").decode()
PROP = "prop_W17mVBrVyrGinzWhTOByeA"          # Hab 5 (702395) en RPV_PROPERTY_MAP


def firmar(cuerpo, msg_id="msg_1", marca=None, secreto=SECRETO_BYTES):
    marca = str(int(marca if marca is not None else time.time()))
    firma = base64.b64encode(hmac.new(secreto, f"{msg_id}.{marca}.".encode() + cuerpo, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": marca, "svix-signature": f"v1,{firma}"}


def evento(id_="evt_1", **extra):
    e = {"id": id_, "type": "guest_registration.completed",
         "data": {"booking": {"check_in": "2026-11-05", "check_out": "2026-11-07", "reference": "123"}, "complete": True,
                  "completed_at": "2026-11-04T10:00:00Z", "guests": {"expected": 2, "registered": 2},
                  "property_id": PROP, "property_name": "Hab 5"}}
    e.update(extra)
    return e


class Firma(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"RPV_WEBHOOK_SECRET_702395": SECRETO, "RPV_WEBHOOK_SECRET_702397": OTRO})
        p.start()
        self.addCleanup(p.stop)

    def test_firma_valida_devuelve_la_habitacion(self):
        cuerpo = json.dumps(evento()).encode()
        self.assertEqual(rpv_webhook.verificar_firma(cuerpo, firmar(cuerpo)), "702395")

    def test_cuerpo_alterado_se_rechaza(self):
        cuerpo = json.dumps(evento()).encode()
        with self.assertRaises(rpv_webhook.FirmaInvalida):
            rpv_webhook.verificar_firma(cuerpo + b" ", firmar(cuerpo))

    def test_secreto_desconocido_se_rechaza(self):
        cuerpo = b"{}"
        with self.assertRaises(rpv_webhook.FirmaInvalida):
            rpv_webhook.verificar_firma(cuerpo, firmar(cuerpo, secreto=b"x" * 32))

    def test_sin_cabeceras_o_marca_vieja_se_rechaza(self):
        cuerpo = b"{}"
        with self.assertRaises(rpv_webhook.FirmaInvalida):
            rpv_webhook.verificar_firma(cuerpo, {})
        with self.assertRaises(rpv_webhook.FirmaInvalida):
            rpv_webhook.verificar_firma(cuerpo, firmar(cuerpo, marca=time.time() - 3600))

    def test_sin_secretos_configurados_todo_se_rechaza(self):
        with mock.patch.dict(os.environ, {"RPV_WEBHOOK_SECRET_702395": "", "RPV_WEBHOOK_SECRET_702397": ""}):
            cuerpo = b"{}"
            with self.assertRaises(rpv_webhook.FirmaInvalida):
                rpv_webhook.verificar_firma(cuerpo, firmar(cuerpo))

    def test_varias_firmas_en_la_cabecera(self):
        cuerpo = b"{}"
        c = firmar(cuerpo)
        c["svix-signature"] = "v1,AAAA " + c["svix-signature"]
        self.assertEqual(rpv_webhook.verificar_firma(cuerpo, c), "702395")


class Procesar(unittest.TestCase):
    def setUp(self):
        rpv_webhook._vistos.clear()
        rpv_webhook._ultimos.clear()
        p = mock.patch.object(rpv, "refrescar_cuenta")
        self.refrescar = p.start()
        self.addCleanup(p.stop)

    def test_parte_completado_refresca_la_cuenta(self):
        self.assertEqual(rpv_webhook.procesar(evento(), refrescar_en_segundo_plano=False), {"resultado": "refrescado"})
        self.refrescar.assert_called_once_with("702395")

    def test_reenvio_con_el_mismo_id_no_repite(self):
        rpv_webhook.procesar(evento(), refrescar_en_segundo_plano=False)
        self.assertEqual(rpv_webhook.procesar(evento(), refrescar_en_segundo_plano=False), {"resultado": "repetido"})
        self.assertEqual(self.refrescar.call_count, 1)

    def test_aviso_de_prueba_no_refresca(self):
        self.assertEqual(rpv_webhook.procesar(evento(test=True), refrescar_en_segundo_plano=False), {"resultado": "prueba"})
        self.refrescar.assert_not_called()

    def test_propiedad_desconocida_o_evento_de_otro_tipo_se_ignoran(self):
        e = evento()
        e["data"]["property_id"] = "prop_otra"
        self.assertEqual(rpv_webhook.procesar(e, refrescar_en_segundo_plano=False), {"resultado": "ignorado"})
        self.assertEqual(rpv_webhook.procesar(evento("evt_2", type="otro.evento"), refrescar_en_segundo_plano=False), {"resultado": "ignorado"})
        self.refrescar.assert_not_called()

    def test_cuerpo_raro_no_rompe(self):
        self.assertEqual(rpv_webhook.procesar({"id": "x", "type": "guest_registration.completed", "data": "texto"}, refrescar_en_segundo_plano=False),
                         {"resultado": "ignorado"})

    def test_constancia_sin_datos_personales(self):
        rpv_webhook.procesar(evento(), refrescar_en_segundo_plano=False)
        u = rpv_webhook.ultimos()[0]
        self.assertEqual((u["habitacion"], u["entrada"], u["resultado"], u["registrados"], u["esperados"]), ("702395", "2026-11-05", "refrescado", 2, 2))


class Refresco(unittest.TestCase):
    def setUp(self):
        rpv._copias.clear()
        for nombre, valor in (("RPV_PROPERTY_MAP", {"A": "prop_1"}), ("RPV_API_KEY_MAP", {}), ("RPV_API_KEY", "clave")):
            p = mock.patch.object(rpv, nombre, valor)
            p.start()
            self.addCleanup(p.stop)

    def test_vuelve_a_consultar_aunque_la_copia_sea_reciente(self):
        nuevo = [{"propiedad_id": "prop_1", "fecha_entrada": "2026-11-05", "estado": "comunicado", "parte_completado": True}]
        rpv._copias["clave"] = {"t": time.time(), "partes": []}
        with mock.patch.object(rpv, "_pedir_partes", return_value=nuevo) as pedir:
            rpv.refrescar_cuenta("A")
        pedir.assert_called_once()
        self.assertTrue(rpv.parte_recibido_para("A", "2026-11-05"))

    def test_si_rpv_falla_se_conserva_la_copia_vieja(self):
        vieja = [{"propiedad_id": "prop_1", "fecha_entrada": "2026-11-05", "estado": "pendiente", "parte_completado": False}]
        rpv._copias["clave"] = {"t": time.time(), "partes": vieja}
        with mock.patch.object(rpv, "_pedir_partes", side_effect=Exception("caída")):
            rpv.refrescar_cuenta("A")
        self.assertEqual(rpv._copias["clave"]["partes"], vieja)


class Endpoint(unittest.TestCase):
    def setUp(self):
        import app as aplicacion
        self.c = aplicacion.app.test_client()
        rpv_webhook._vistos.clear()
        p = mock.patch.dict(os.environ, {"RPV_WEBHOOK_SECRET_702395": SECRETO})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(rpv, "refrescar_cuenta")
        self.refrescar = p.start()
        self.addCleanup(p.stop)

    def test_sin_firma_401(self):
        r = self.c.post("/webhook-rpv", data=json.dumps(evento()), content_type="application/json")
        self.assertEqual(r.status_code, 401)
        self.refrescar.assert_not_called()

    def test_con_firma_200(self):
        cuerpo = json.dumps(evento()).encode()
        r = self.c.post("/webhook-rpv", data=cuerpo, content_type="application/json", headers=firmar(cuerpo))
        self.assertEqual((r.status_code, r.get_json()["resultado"]), (200, "refrescado"))

    def test_ultimos_pide_token(self):
        self.assertEqual(self.c.get("/webhook-rpv/ultimos").status_code, 401)


if __name__ == "__main__":
    unittest.main()

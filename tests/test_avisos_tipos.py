"""Avisos por tipo: el número 2 de CallMeBot y los chats «solo sistema» de Telegram solo reciben los avisos de funcionamiento del sistema."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import avisos_registro, whatsapp as wa


class Base(unittest.TestCase):
    def setUp(self):
        self.whatsapp, self.telegram = [], []

        def get(url, timeout=None):
            self.whatsapp.append(url)
            r = mock.Mock()
            r.status_code = 200
            r.text = "Message queued."
            return r

        def post(url, json=None, timeout=None):
            self.telegram.append(json["chat_id"])
            r = mock.Mock()
            r.ok = True
            r.status_code = 200
            r.json.return_value = {"ok": True}
            return r

        valores = (("CALLMEBOT_PHONE", "111"), ("CALLMEBOT_API_KEY", "k1"), ("CALLMEBOT_PHONE_2", "222"), ("CALLMEBOT_API_KEY_2", "k2"),
                   ("TELEGRAM_BOT_TOKEN", "bot"), ("TELEGRAM_CHAT_ID", "10,20"), ("TELEGRAM_CHAT_ID_SOLO_SISTEMA", "30"), ("AVISOS_EMAIL", ""))
        for nombre, valor in valores:
            p = mock.patch.object(wa, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        for obj, nombre, valor in ((wa.requests, "get", get), (wa.requests, "post", post),
                                   (avisos_registro, "registrar", mock.Mock(return_value=0))):
            p = mock.patch.object(obj, nombre, valor)
            p.start()
            self.addCleanup(p.stop)

    def numeros(self):
        return [u.split("phone=")[1].split("&")[0] for u in self.whatsapp]


class WhatsApp(Base):
    def test_un_aviso_de_sistema_llega_a_los_dos_numeros(self):
        wa.enviar_whatsapp_callmebot("🔴 fallo", wa.TIPO_SISTEMA)
        self.assertEqual(self.numeros(), ["111", "222"])

    def test_un_aviso_de_reservas_solo_llega_al_numero_1(self):
        wa.enviar_whatsapp_callmebot("Resumen de llegadas de hoy", "reservas")
        self.assertEqual(self.numeros(), ["111"])

    def test_un_aviso_sin_tipo_tampoco_llega_al_numero_2(self):
        wa.enviar_whatsapp_callmebot("algo nuevo que nadie etiquetó")
        self.assertEqual(self.numeros(), ["111"])

    def test_alerta_pasa_el_tipo(self):
        wa.alerta("Posible sobreventa", "x", nivel="critico")
        self.assertEqual(self.numeros(), ["111"])
        self.whatsapp.clear()
        wa.alerta("Watchdog", "x", nivel="critico", tipo=wa.TIPO_SISTEMA)
        self.assertEqual(self.numeros(), ["111", "222"])

    def test_avisar_error_critico_es_de_sistema(self):
        wa.avisar_error_critico("Fallo de autenticación con Beds24", "x")
        self.assertEqual(self.numeros(), ["111", "222"])


class Telegram(Base):
    def test_reservas_solo_a_los_chats_generales(self):
        wa.enviar_whatsapp_callmebot("Llegada de hoy", "reservas")
        self.assertEqual(sorted(self.telegram), ["10", "20"])

    def test_sistema_a_todos_los_chats(self):
        wa.enviar_whatsapp_callmebot("🔴 fallo", wa.TIPO_SISTEMA)
        self.assertEqual(sorted(self.telegram), ["10", "20", "30"])

    def test_un_chat_en_las_dos_listas_solo_recibe_sistema_y_una_vez(self):
        with mock.patch.object(wa, "TELEGRAM_CHAT_ID", "10,20,30"):
            wa.enviar_whatsapp_callmebot("Llegada de hoy")
            self.assertEqual(sorted(self.telegram), ["10", "20"])
            self.telegram.clear()
            wa.enviar_whatsapp_callmebot("🔴 fallo", wa.TIPO_SISTEMA)
            self.assertEqual(sorted(self.telegram), ["10", "20", "30"])

    def test_sin_la_variable_nueva_todo_sigue_como_antes(self):
        with mock.patch.object(wa, "TELEGRAM_CHAT_ID_SOLO_SISTEMA", ""):
            wa.enviar_whatsapp_callmebot("Llegada de hoy", "reservas")
            self.assertEqual(sorted(self.telegram), ["10", "20"])

    def test_si_el_unico_chat_es_solo_sistema_un_aviso_de_reservas_no_lo_envia(self):
        with mock.patch.object(wa, "TELEGRAM_CHAT_ID", "30"):
            wa.enviar_whatsapp_callmebot("Llegada de hoy")
            self.assertEqual(self.telegram, [])
            self.assertEqual(self.numeros(), ["111"])      # el WhatsApp del número 1 sí

    def test_el_aviso_de_callmebot_caido_llega_a_los_chats_de_sistema(self):
        wa._avisar_callmebot_caido(3)
        self.assertEqual(sorted(self.telegram), ["10", "20", "30"])


if __name__ == "__main__":
    unittest.main()

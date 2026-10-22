"""Herramientas de emergencia con una reserva de VARIAS habitaciones: un solo mensaje con todos los enlaces y un solo correo con todos los códigos."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mobile_asistente as asistente
import routes.acceso as acceso
from services import enlace_parte
from services.acceso_correos import correo_codigos, correo_codigos_grupo

H3 = {"id": 100, "roomId": 702399, "arrival": "2026-10-21", "departure": "2026-10-23", "firstName": "Marta", "lastName": "Ruiz"}
H2 = {"id": 101, "roomId": 702398, "arrival": "2026-10-21", "departure": "2026-10-23", "masterId": 100}
H1 = {"id": 102, "roomId": 702397, "arrival": "2026-10-21", "departure": "2026-10-23", "masterId": 100}
GRUPO = [H3, H2, H1]
CONFIG = {"702399": {"nombre": "Cala del Moraig", "pin": "213456"}, "702398": {"nombre": "Playa del Albir", "pin": "654321"},
          "702397": {"nombre": "Playa Lanuza", "pin": "777777"}}


def setUpModule():
    global _p
    _p = mock.patch.dict(acceso.ROOM_CONFIG, CONFIG)
    _p.start()


def tearDownModule():
    _p.stop()


class Correo(unittest.TestCase):
    def test_una_habitacion_sale_igual_que_antes(self):
        a = correo_codigos("es", "https://x", "Cala del Moraig", "213456", codigo_portal="130773")
        b = correo_codigos_grupo("es", "https://x", [("Cala del Moraig", "213456", False)], codigo_portal="130773")
        self.assertEqual(a, b)

    def test_grupo_lleva_portal_una_vez_y_el_codigo_de_cada_habitacion(self):
        asunto, cuerpo = correo_codigos_grupo("es", "https://x", [("A", "111111", False), ("B", "222222", False), ("C", "333333", False)], codigo_portal="130773")
        self.assertEqual(cuerpo.count("130773"), 1)
        for pin in ("111111", "222222", "333333"):
            self.assertIn(pin, cuerpo)
        self.assertIn("• B: 222222", cuerpo)

    def test_grupo_en_ingles(self):
        _, cuerpo = correo_codigos_grupo("en", "https://x", [("A", "111111", False), ("B", "222222", False)], codigo_portal="130773")
        self.assertIn("enter it on the door keypad", cuerpo)


class RedactarCodigos(unittest.TestCase):
    def test_grupo_incluye_todas_las_habitaciones(self):
        asunto, cuerpo, nombre = acceso.redactar_codigos(H3, "es", GRUPO)
        self.assertEqual(nombre, "Cala del Moraig + Playa del Albir + Playa Lanuza")
        self.assertEqual(cuerpo.count(acceso.CODIGO_PORTAL), 1)

    def test_si_a_una_le_falta_el_codigo_no_se_redacta_nada(self):
        with mock.patch.dict(acceso.ROOM_CONFIG, {"702397": {"nombre": "Playa Lanuza", "pin": ""}}):
            with self.assertRaises(acceso.ErrorCodigos):
                acceso.redactar_codigos(H3, "es", GRUPO)

    def test_sin_grupo_no_cambia(self):
        asunto, cuerpo, nombre = acceso.redactar_codigos(H3, "es")
        self.assertEqual(nombre, "Cala del Moraig")


class Enlace(unittest.TestCase):
    def test_grupo_lleva_el_enlace_las_fechas_y_la_referencia_de_cada_habitacion(self):
        texto, habitaciones, enlaces = enlace_parte.redactar_enlace_parte(H3, "es", GRUPO)
        self.assertEqual(len(enlaces), 3)
        for h in GRUPO:
            self.assertIn(enlace_parte.RPV_LINKS[str(h["roomId"])], texto)
            self.assertIn(f"Referencia: {h['id']}", texto)
        self.assertIn("3 habitaciones", texto)
        self.assertEqual(texto.count("Llegada 21/10/2026 y salida 23/10/2026"), 3)

    def test_sin_grupo_es_el_mensaje_de_siempre(self):
        texto, _, enlace = enlace_parte.redactar_enlace_parte(H3, "es", [H3])
        self.assertIsInstance(enlace, str)
        self.assertNotIn("habitaciones y hay que", texto)


class Herramientas(unittest.TestCase):
    def test_enlace_de_emergencia_detecta_el_grupo(self):
        with mock.patch.object(asistente, "buscar_booking_por_id", return_value=H3), mock.patch.object(asistente, "miembros_del_grupo", return_value=GRUPO):
            res = asistente.ejecutar_herramienta("enlace_parte_emergencia", json.dumps({"reserva": "100", "idioma": "es"}), {"es_admin": False, "borradores": []})
        self.assertEqual(res["habitaciones"], 3)
        self.assertIn("3 habitaciones", res["mensaje"])

    def test_borrador_de_codigos_para_el_grupo(self):
        with mock.patch.object(asistente, "buscar_booking_por_id", return_value=H3), mock.patch.object(asistente, "miembros_del_grupo", return_value=GRUPO):
            b = asistente.borrador_codigos("100", "cliente@empresa.com", "es")
        self.assertEqual(b["habitaciones"], 3)
        self.assertEqual(b["cuerpo"].count(acceso.CODIGO_PORTAL), 1)

    def test_un_fallo_al_buscar_el_grupo_no_impide_enviar_la_habitacion_pedida(self):
        with mock.patch.object(acceso, "reservas_del_grupo", side_effect=Exception("beds24 caido")):
            self.assertEqual(acceso.miembros_del_grupo(H3), [H3])


class Separados(unittest.TestCase):
    """El titular eligió repartir por habitación: cada habitación con su propio mensaje y su propio correo."""

    def setUp(self):
        for o, n, v in ((asistente, "buscar_booking_por_id", lambda i: H3), (asistente, "miembros_del_grupo", lambda b: GRUPO),
                        (acceso.grupos_acceso, "obtener", lambda m: "separados")):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)

    def test_el_mensaje_del_parte_sale_uno_por_habitacion(self):
        res = asistente.ejecutar_herramienta("enlace_parte_emergencia", json.dumps({"reserva": "100", "idioma": "es"}), {"es_admin": False, "borradores": []})
        self.assertEqual(res["reparto"], "separados")
        self.assertEqual(len(res["mensajes"]), 3)
        for m in res["mensajes"]:
            enlaces = [l for l in enlace_parte.RPV_LINKS.values() if l in m["mensaje"]]
            self.assertEqual(len(enlaces), 1)                    # cada mensaje lleva SOLO el enlace de su habitación
            self.assertIn(f"Referencia: {m['reserva']}" if "Referencia" in m["mensaje"] else m["reserva"], m["mensaje"])

    def test_el_correo_de_codigos_lleva_solo_la_habitacion_pedida(self):
        b = asistente.borrador_codigos("100", "uno@empresa.com", "es")
        self.assertEqual((b["habitaciones"], b["reparto"], b["habitacion"]), (1, "separados", "Cala del Moraig"))
        self.assertIn("213456", b["cuerpo"])
        self.assertNotIn("654321", b["cuerpo"])
        self.assertNotIn("777777", b["cuerpo"])
        self.assertEqual({o["reserva"] for o in b["otras_habitaciones"]}, {"101", "102"})

    def test_la_herramienta_avisa_de_que_hacen_falta_mas_correos(self):
        res = asistente.ejecutar_herramienta("preparar_correo_codigos", json.dumps({"reserva": "100", "email": "uno@empresa.com", "idioma": "es"}),
                                             {"es_admin": True, "borradores": []})
        self.assertIn("POR HABITACIÓN", res["nota"])
        self.assertEqual(len(res["otras_habitaciones"]), 2)

    def test_juntos_o_sin_elegir_siguen_igual(self):
        with mock.patch.object(acceso.grupos_acceso, "obtener", lambda m: "juntos"):
            b = asistente.borrador_codigos("100", "uno@empresa.com", "es")
        self.assertEqual((b["habitaciones"], b["reparto"], b["otras_habitaciones"]), (3, None, []))
        self.assertIn("654321", b["cuerpo"])


if __name__ == "__main__":
    unittest.main()

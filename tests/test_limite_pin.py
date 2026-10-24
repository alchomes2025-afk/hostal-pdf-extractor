"""Límite de intentos de PIN: tras 8 PIN equivocados desde una IP se rechaza todo desde ella durante 15 minutos."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_limpieza
import mobile_routes
from services import limites


class Base(unittest.TestCase):
    def setUp(self):
        limites._eventos.clear()
        app = Flask(__name__)
        app.register_blueprint(mobile_routes.mobile_bp)
        app.register_blueprint(mobile_limpieza.limpieza_bp)
        self.c = app.test_client()
        for o, n, v in ((mobile_routes, "MOBILE_APP_PIN", "1111"), (mobile_routes, "MOBILE_APP_PIN_ADMIN", "9999")):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("config.PIN_LIMPIEZA", "4268")
        p.start()
        self.addCleanup(p.stop)

    def login(self, pin, ip="1.1.1.1"):
        return self.c.post("/mobile/login", json={"pin": pin}, headers={"CF-Connecting-IP": ip})

    def fallar(self, n, ip="1.1.1.1"):
        for i in range(n):
            self.login(f"{i:04d}" if f"{i:04d}" not in ("1111", "9999", "4268") else "0001", ip)


class Limite(Base):
    def test_sin_fallos_entra_normal(self):
        self.assertEqual(self.login("1111").status_code, 200)

    def test_tras_ocho_fallos_se_bloquea_incluso_el_pin_bueno(self):
        self.fallar(mobile_routes.MAX_FALLOS_PIN)
        r = self.login("1111")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Demasiados intentos", r.get_json()["error"])

    def test_el_octavo_fallo_ya_avisa(self):
        self.fallar(mobile_routes.MAX_FALLOS_PIN - 1)
        self.assertEqual(self.login("0007").status_code, 429)   # el que agota el cupo ya responde bloqueado

    def test_con_siete_fallos_aun_se_puede_entrar(self):
        self.fallar(mobile_routes.MAX_FALLOS_PIN - 1)
        self.assertEqual(self.login("1111").status_code, 200)

    def test_otra_ip_no_se_ve_afectada(self):
        self.fallar(mobile_routes.MAX_FALLOS_PIN)
        self.assertEqual(self.login("1111", ip="2.2.2.2").status_code, 200)

    def test_pasada_la_ventana_se_desbloquea(self):
        self.fallar(mobile_routes.MAX_FALLOS_PIN)
        ahora = __import__("time").time() + mobile_routes.VENTANA_FALLOS_PIN + 5
        with mock.patch.object(limites.time, "time", return_value=ahora):
            self.assertEqual(self.login("1111").status_code, 200)

    def test_cuenta_en_cualquier_endpoint_no_solo_en_login(self):
        for _ in range(mobile_routes.MAX_FALLOS_PIN):
            self.c.get("/mobile/rooms", headers={"x-app-pin": "0000", "CF-Connecting-IP": "1.1.1.1"})
        self.assertEqual(self.login("1111").status_code, 429)

    def test_sin_pin_no_cuenta(self):
        for _ in range(20):
            self.c.get("/mobile/rooms", headers={"CF-Connecting-IP": "1.1.1.1"})
        self.assertEqual(self.login("1111").status_code, 200)

    def test_un_pin_valido_de_otro_perfil_no_cuenta_como_intento(self):
        for _ in range(20):   # la limpiadora llamando a una ruta que no es suya: se rechaza, pero no es adivinar
            self.c.get("/mobile/rooms", headers={"x-app-pin": "4268", "CF-Connecting-IP": "1.1.1.1"})
        self.assertEqual(self.login("1111").status_code, 200)

    def test_limpieza_tambien_cuenta_y_se_bloquea(self):
        for _ in range(mobile_routes.MAX_FALLOS_PIN):
            self.c.get("/mobile/limpieza/calendario", headers={"x-app-pin": "0000", "CF-Connecting-IP": "1.1.1.1"})
        self.assertEqual(self.c.get("/mobile/limpieza/calendario", headers={"x-app-pin": "4268", "CF-Connecting-IP": "1.1.1.1"}).status_code, 401)
        self.assertEqual(self.login("1111").status_code, 429)


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from services import guest_match, limites
from services.guest_match import _compatible, _match_exacto, emparejar_nombre, normalizar_nombre


def idx(consulta, nombres):
    return _match_exacto(normalizar_nombre(consulta), [normalizar_nombre(n) for n in nombres])


class CoincidenciaDeNombres(unittest.TestCase):
    def test_un_nombre_de_pila_no_abre_ninguna_reserva(self):
        self.assertEqual(idx("Yuka", ["Yuka Tanaka"]), [])
        self.assertEqual(idx("Tanaka", ["Yuka Tanaka"]), [])

    def test_nombre_completo_en_cualquier_orden_y_con_acentos(self):
        self.assertEqual(idx("tanaka YUKA", ["Yuka Tanaka"]), [0])
        self.assertEqual(idx("Maria de los Angeles Gomez", ["María de los Ángeles Gómez Manzanero"]), [0])

    def test_un_apellido_de_mas_o_de_menos(self):
        self.assertEqual(idx("Yuka Tanaka Suzuki", ["Yuka Tanaka"]), [0])        # una palabra de más
        self.assertEqual(idx("Yuka Tanaka", ["Yuka Tanaka Suzuki"]), [0])        # falta un apellido
        self.assertEqual(idx("Yuka Tanaka Suzuki Sato", ["Yuka Tanaka"]), [])    # dos de más

    def test_una_lista_larga_de_nombres_comunes_no_coincide(self):
        self.assertEqual(idx("Maria Juan Pedro Ana Luis Jose Carlos", ["Maria Garcia"]), [])

    def test_reserva_con_un_solo_nombre(self):
        self.assertEqual(idx("Madonna", ["Madonna"]), [0])
        self.assertEqual(idx("Madonna Ciccone", ["Madonna"]), [0])

    def test_emparejar_con_un_nombre_de_pila_no_devuelve_nada(self):
        with mock.patch.object(guest_match, "_desambiguar_con_groq", return_value=None):
            self.assertEqual(emparejar_nombre("Yuka", [("reserva", "Yuka Tanaka")]), (None, True))
            self.assertEqual(emparejar_nombre("Yuka Tanaka", [("reserva", "Yuka Tanaka")]), ("reserva", False))


class EleccionDeGroq(unittest.TestCase):
    def test_tolera_erratas(self):
        self.assertTrue(_compatible("Yuka Tanka", "Yuka Tanaka"))
        self.assertTrue(_compatible("Fabian Feuerstein", "Fabián Feuerstein"))

    def test_descarta_a_alguien_que_no_se_parece(self):
        self.assertFalse(_compatible("Yuka Tanaka", "Fabian Feuerstein"))
        self.assertFalse(_compatible("Yuka", "Yuka Tanaka"))

    def test_emparejar_descarta_la_eleccion_incompatible(self):
        cands = [("a", "Fabian Feuerstein"), ("b", "Bryan Teo")]
        with mock.patch.object(guest_match, "_desambiguar_con_groq", return_value=0):
            self.assertEqual(emparejar_nombre("Yuka Tanaka", cands), (None, True))

    def test_emparejar_acepta_la_eleccion_parecida(self):
        cands = [("a", "Yuka Tanaka"), ("b", "Bryan Teo")]
        with mock.patch.object(guest_match, "_desambiguar_con_groq", return_value=0):
            self.assertEqual(emparejar_nombre("Yuka Tanka", cands), ("a", False))


class IpDelCliente(unittest.TestCase):
    def ip(self, **cabeceras):
        with Flask(__name__).test_request_context("/", headers=cabeceras):
            return limites.ip_cliente()

    def test_x_forwarded_for_no_se_puede_falsear(self):
        # quien llama pone 1.1.1.1; el proxy añade la IP real al final
        self.assertEqual(self.ip(**{"X-Forwarded-For": "1.1.1.1, 203.0.113.7"}), "203.0.113.7")

    def test_manda_la_cabecera_de_cloudflare(self):
        self.assertEqual(self.ip(**{"CF-Connecting-IP": "198.51.100.4", "X-Forwarded-For": "1.1.1.1, 203.0.113.7"}), "198.51.100.4")

    def test_sin_cabeceras(self):
        self.assertTrue(self.ip())


if __name__ == "__main__":
    unittest.main()

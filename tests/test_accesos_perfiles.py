"""Perfiles por PIN y su tabla de permisos (fase 1: el modelo). La tabla es la confirmada por Adrián el 2026-10-24; cualquier cambio debe ser consciente."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import accesos as a

PINES = {"1379": "propietario", "2486": "gestor", "9999": "gestor_primavera", "2589": "sustituto", "4268": "limpieza"}

# permiso → perfiles que lo tienen (la tabla de Adrián)
TABLA = {
    a.CALENDARIO: {"propietario", "gestor_primavera", "gestor", "sustituto"},
    a.RESERVAS: {"propietario", "gestor_primavera", "gestor", "sustituto"},
    a.PRECIOS: {"propietario", "gestor_primavera"},
    a.ASISTENTE: {"propietario", "gestor_primavera", "gestor", "sustituto"},
    a.ASISTENTE_COMPLETO: {"propietario"},
    a.FINANZAS_PRIMAVERA: {"propietario", "gestor_primavera"},
    a.FINANZAS_HOSTAL: {"propietario"},
    a.FACTURAS: {"propietario"},
    a.EMERGENCIA: {"propietario", "sustituto"},
    a.EMERGENCIA_SIN_PIN: {"propietario"},
    a.EMERGENCIA_VER_PIN: {"propietario", "sustituto"},
    a.OPERATIVA: {"propietario", "sustituto"},
    a.ENTRENAMIENTO: {"propietario"},
    a.HISTORIAL: {"propietario"},
    a.INCIDENCIAS_VER: {"propietario", "gestor_primavera"},
    a.PRIMAVERA: {"propietario", "gestor_primavera"},
    a.LIMPIEZA_CALENDARIO: {"limpieza"},
    a.LIMPIEZA_MARCAR: {"limpieza"},
    a.SISTEMA: {"propietario"},
}


class Pines(unittest.TestCase):
    def test_cada_pin_es_de_su_perfil(self):
        for pin, perfil in PINES.items():
            self.assertEqual(a.perfil_por_pin(pin).id, perfil, pin)

    def test_pin_desconocido_o_vacio(self):
        for pin in ("0000", "", None, "  ", "13790", "1432", "x" * 50):
            self.assertIsNone(a.perfil_por_pin(pin), repr(pin))

    def test_el_pin_de_seguridad_1432_no_abre_la_app(self):
        self.assertIsNone(a.perfil_por_pin("1432"))

    def test_se_ignoran_los_espacios(self):
        self.assertEqual(a.perfil_por_pin(" 1379 ").id, "propietario")

    def test_no_se_guardan_pines_en_claro(self):
        for h in a._HASHES.values():
            self.assertIsInstance(h, bytes)
            self.assertEqual(len(h), 32)
        self.assertFalse(hasattr(a, "_PIN_POR_PERFIL"))

    def test_el_mismo_pin_da_hash_distinto_por_perfil(self):
        self.assertNotEqual(a._hash("gestor", "1234"), a._hash("sustituto", "1234"))


class Permisos(unittest.TestCase):
    def test_la_tabla_completa(self):
        for permiso, con_permiso in TABLA.items():
            for perfil in a.PERFILES:
                self.assertEqual(a.tiene(perfil, permiso), perfil in con_permiso, f"{perfil} / {permiso}")

    def test_todos_los_permisos_estan_en_la_tabla(self):
        self.assertEqual(set(TABLA), set(a.TODOS))

    def test_ningun_perfil_tiene_permisos_inventados(self):
        for perfil in a.PERFILES.values():
            self.assertTrue(perfil.permisos <= set(a.TODOS), perfil.id)

    def test_lo_que_no_deben_poder_2486_2589_y_9999(self):
        for perfil in ("gestor", "sustituto"):
            for p in (a.FINANZAS_PRIMAVERA, a.FINANZAS_HOSTAL, a.FACTURAS, a.ENTRENAMIENTO, a.HISTORIAL, a.PRECIOS):
                self.assertFalse(a.tiene(perfil, p), f"{perfil} no debe tener {p}")
        self.assertFalse(a.tiene("gestor_primavera", a.FINANZAS_HOSTAL))
        self.assertFalse(a.tiene("gestor_primavera", a.FACTURAS))
        self.assertFalse(a.tiene("gestor_primavera", a.OPERATIVA))
        self.assertFalse(a.tiene("gestor", a.OPERATIVA))
        self.assertFalse(a.tiene("gestor", a.EMERGENCIA))
        self.assertFalse(a.tiene("gestor_primavera", a.EMERGENCIA))

    def test_la_limpiadora_solo_tiene_lo_suyo(self):
        self.assertEqual(a.PERFILES["limpieza"].permisos, {a.LIMPIEZA_CALENDARIO, a.LIMPIEZA_MARCAR})

    def test_solo_el_sustituto_es_auditado_y_pide_nombre(self):
        self.assertEqual({p.id for p in a.PERFILES.values() if p.auditado}, {"sustituto"})
        self.assertEqual({p.id for p in a.PERFILES.values() if p.pide_nombre}, {"sustituto"})

    def test_perfil_desconocido_o_none_no_tiene_nada(self):
        self.assertFalse(a.tiene(None, a.CALENDARIO))
        self.assertFalse(a.tiene("inventado", a.CALENDARIO))


if __name__ == "__main__":
    unittest.main()

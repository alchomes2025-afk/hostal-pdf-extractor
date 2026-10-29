"""Partes de viajeros de fechas pasadas: consulta a RPV por trozos y herramientas del asistente (ayer y estancias en vigor)."""
import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mobile_asistente as asistente
from services import rpv

HOY = date(2026, 10, 29)
AYER = HOY - timedelta(days=1)


def _parte(prop, fecha, estado="comunicado", completado=True):
    return {"propiedad_id": prop, "fecha_entrada": fecha.isoformat(), "estado": estado, "parte_completado": completado}


class ConsultaPasada(unittest.TestCase):
    def setUp(self):
        rpv._copias_pasados.clear()
        for nombre, valor in (("RPV_PROPERTY_MAP", {"A": 1, "B": 2}), ("RPV_API_KEY_MAP", {}), ("RPV_API_KEY", "clave")):
            p = mock.patch.object(rpv, nombre, valor)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(rpv, "hoy_madrid", return_value=HOY)
        p.start()
        self.addCleanup(p.stop)

    def test_ayer_devuelve_el_estado_por_habitacion(self):
        partes = [_parte(1, AYER), _parte(2, AYER, "pendiente", False)]
        with mock.patch.object(rpv, "_pedir_partes", return_value=partes) as pedir:
            estados, sin = rpv.obtener_estado_partes_pasados(AYER)
        pedir.assert_called_once_with("clave", desde=AYER.isoformat(), hasta=AYER.isoformat())
        self.assertTrue(estados[("A", AYER.isoformat())]["completado"])
        self.assertFalse(estados[("B", AYER.isoformat())]["completado"])
        self.assertEqual(sin, set())

    def test_rango_largo_se_pide_en_trozos_de_31_dias_y_se_recorta(self):
        with mock.patch.object(rpv, "_pedir_partes", return_value=[]) as pedir:
            rpv.obtener_estado_partes_pasados(HOY - timedelta(days=400))
        rangos = [(c.kwargs["desde"], c.kwargs["hasta"]) for c in pedir.call_args_list]
        self.assertEqual(len(rangos), 2)                                   # 62 días máximo = 2 trozos
        self.assertEqual(rangos[0][0], (HOY - timedelta(days=rpv.PASADO_DIAS_MAX)).isoformat())
        self.assertEqual(rangos[-1][1], AYER.isoformat())
        for d, h in rangos:
            self.assertLessEqual((date.fromisoformat(h) - date.fromisoformat(d)).days, 30)

    def test_si_rpv_falla_queda_sin_verificar(self):
        with mock.patch.object(rpv, "_pedir_partes", side_effect=Exception("caída")):
            estados, sin = rpv.obtener_estado_partes_pasados(AYER)
        self.assertEqual(estados, {})
        self.assertEqual(sin, {"A", "B"})

    def test_copia_en_memoria_no_repite_la_llamada(self):
        with mock.patch.object(rpv, "_pedir_partes", return_value=[_parte(1, AYER)]) as pedir:
            rpv.obtener_estado_partes_pasados(AYER)
            rpv.obtener_estado_partes_pasados(AYER)
        self.assertEqual(pedir.call_count, 1)

    def test_desde_hoy_o_futuro_no_consulta_nada(self):
        with mock.patch.object(rpv, "_pedir_partes") as pedir:
            estados, _ = rpv.obtener_estado_partes_pasados(HOY)
        pedir.assert_not_called()
        self.assertEqual(estados, {})


def _reserva(id_, room, huesped, checkin, checkout):
    return {"id": id_, "checkin": checkin, "checkout": checkout, "noches": (checkout - checkin).days, "room_id": room,
            "habitacion": f"Hab {room}", "tipo": "doble", "canal": "Booking.com", "huesped": huesped, "pais": "España",
            "creada": "2026-10-01T10:00:00Z", "antelacion": 5, "bruto": 0, "comision": 0}


class HerramientasAsistente(unittest.TestCase):
    def setUp(self):
        self.reservas = [
            _reserva(1, "A", "Ana Ayer", AYER, HOY + timedelta(days=2)),                 # entró ayer, parte completado
            _reserva(2, "B", "Beto Antiguo", HOY - timedelta(days=5), HOY + timedelta(days=1)),   # en vigor, parte pendiente
            _reserva(3, "A", "Carla Futura", HOY + timedelta(days=3), HOY + timedelta(days=5)),
            _reserva(4, "B", "Dani Pasado", HOY - timedelta(days=9), HOY - timedelta(days=7)),    # ya se fue
        ]
        for objetivo, valor in (
            (asistente, {"hoy_madrid": lambda: HOY}),
        ):
            for nombre, v in valor.items():
                p = mock.patch.object(objetivo, nombre, v)
                p.start()
                self.addCleanup(p.stop)
        p = mock.patch.object(asistente, "_reservas", return_value=(self.reservas, False))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(asistente, "_ids_propiedad", return_value="1")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(asistente, "_propiedades", return_value=["hostal"])
        p.start()
        self.addCleanup(p.stop)
        self.futuros = {("A", (HOY + timedelta(days=3)).isoformat()): {"estado": "pendiente", "completado": False, "comunicado": False, "registrados": 0, "previstos": 2}}
        self.pasados = {
            ("A", AYER.isoformat()): {"estado": "comunicado", "completado": True, "comunicado": True, "registrados": 2, "previstos": 2},
            ("B", (HOY - timedelta(days=5)).isoformat()): {"estado": "pendiente", "completado": False, "comunicado": False, "registrados": 0, "previstos": 2},
            ("B", (HOY - timedelta(days=9)).isoformat()): {"estado": "comunicado", "completado": True, "comunicado": True, "registrados": 1, "previstos": 1},
        }
        p = mock.patch.object(asistente, "obtener_estado_partes", return_value=(self.futuros, set()))
        p.start()
        self.addCleanup(p.stop)
        self.pedido_pasado = mock.patch.object(asistente, "obtener_estado_partes_pasados", return_value=(self.pasados, set()))
        self.mock_pasado = self.pedido_pasado.start()
        self.addCleanup(self.pedido_pasado.stop)

    def test_partes_de_ayer(self):
        r = asistente._t_partes({"desde": AYER.isoformat(), "hasta": AYER.isoformat()}, {})
        self.assertEqual(r["total_llegadas"], 1)
        self.assertEqual(r["llegadas"][0]["huesped"], "Ana Ayer")
        self.assertTrue(r["llegadas"][0]["parte"].startswith("parte recibido"))
        self.mock_pasado.assert_called_once_with(AYER)

    def test_partes_en_vigor_incluye_estancias_ya_empezadas(self):
        r = asistente._t_partes({"en_vigor": True}, {})
        self.assertTrue(r["solo_estancias_en_vigor"])
        self.assertEqual(sorted(f["huesped"] for f in r["llegadas"]), ["Ana Ayer", "Beto Antiguo"])
        beto = next(f for f in r["llegadas"] if f["huesped"] == "Beto Antiguo")
        self.assertIn("PENDIENTE", beto["parte"])
        self.mock_pasado.assert_called_once_with(HOY - timedelta(days=5))

    def test_sin_fechas_no_consulta_pasado(self):
        asistente._t_partes({}, {})
        self.mock_pasado.assert_not_called()

    def test_agenda_de_hoy_da_el_parte_de_los_que_se_quedan(self):
        r = asistente._t_agenda({}, {})
        quedan = {f["huesped"]: f["parte"] for f in r["hostal"]["se_quedan"]}
        self.assertTrue(quedan["Ana Ayer"].startswith("parte recibido"))
        self.assertIn("PENDIENTE", quedan["Beto Antiguo"])

    def test_agenda_de_ayer(self):
        r = asistente._t_agenda({"fecha": AYER.isoformat()}, {})
        self.assertEqual([x["huesped"] for x in r["hostal"]["llegadas"]], ["Ana Ayer"])
        self.assertTrue(r["hostal"]["llegadas"][0]["parte"].startswith("parte recibido"))

    def test_parte_huesped_de_estancia_ya_empezada_avisa_si_falta(self):
        r = asistente._t_parte_huesped({"huesped": "beto"}, {})
        c = r["coincidencias"][0]
        self.assertIn("PENDIENTE", c["estado_parte"])
        self.assertIn("SIN completar", c["que_decir_al_huesped"])
        self.assertTrue(c["anomalia"])

    def test_si_rpv_no_responde_en_el_pasado_no_se_dice_pendiente(self):
        self.mock_pasado.return_value = ({}, {"A", "B"})
        r = asistente._t_partes({"en_vigor": True}, {})
        self.assertTrue(all("SIN VERIFICAR" in f["parte"].upper() for f in r["llegadas"]))

    def test_mas_alla_del_tope_se_dice_que_no_se_puede_consultar(self):
        lejos = HOY - timedelta(days=rpv.PASADO_DIAS_MAX + 10)
        texto = asistente._parte({"room_id": "A", "creada": None}, lejos, {}, set(), HOY)
        self.assertIn("fuera de lo que puedo consultar", texto)


if __name__ == "__main__":
    unittest.main()

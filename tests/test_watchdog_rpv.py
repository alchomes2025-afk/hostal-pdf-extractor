"""Chequeo de RPV del watchdog: un fallo de red (timeout, 5xx) es un aviso con texto fijo y se reintenta una vez; solo 401/403 es crítico."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.watchdog as wd
from services import rpv

TIMEOUT_CASA = ("HTTPSConnectionPool(host=app.registroparteviajeros.com, port=443): Read timed out. (read timeout=10)")
TIMEOUT_HOSTAL = TIMEOUT_CASA


class TipoDeError(unittest.TestCase):
    def test_clasificacion(self):
        casos = {
            TIMEOUT_CASA: "temporal",
            "503 Server Error: Service Unavailable for url: https://app.registroparteviajeros.com/api/v1/partes": "temporal",
            "HTTPSConnectionPool(host=app.registroparteviajeros.com, port=443): Max retries exceeded (Caused by ConnectTimeoutError)": "temporal",
            "API key inválida (401)": "credenciales",
            "Acceso denegado (403) — alguna propiedad no existe o no es de esta cuenta": "credenciales",
            "429 — límite de peticiones de RPV (pausa de 2 min)": "limite",
            "429 — límite local de peticiones a RPV (máx. 20/min por cuenta); se reintenta en un minuto": "limite",
            "RPV rechazó la petición (400): fechas": "contrato",
            "Endpoint de partes no encontrado (404)": "contrato",
            "respuesta inesperada de RPV (sin lista 'partes')": "contrato",
        }
        for texto, esperado in casos.items():
            self.assertEqual(rpv.tipo_error(texto), esperado, texto)


class Reintento(unittest.TestCase):
    def setUp(self):
        self.llamadas = []
        for obj, nombre, valor in ((rpv, "_cuentas", mock.Mock(return_value={"k1": ["A"], "k2": ["B"]})),
                                   (rpv, "_etiqueta", lambda rooms: rooms[0]),
                                   (rpv.time, "sleep", mock.Mock())):
            p = mock.patch.object(obj, nombre, valor)
            p.start()
            self.addCleanup(p.stop)

    def respuestas(self, **por_clave):
        """_partes_cuenta simulado: por clave, la lista de respuestas ([error|None]) que va devolviendo en cada llamada."""
        restantes = {k: list(v) for k, v in por_clave.items()}

        def falso(key, max_age, etiqueta):
            self.llamadas.append((key, max_age))
            return [], restantes[key].pop(0)
        return mock.patch.object(rpv, "_partes_cuenta", falso)

    def test_un_timeout_puntual_se_recupera_en_el_reintento(self):
        with self.respuestas(k1=[None], k2=[TIMEOUT_CASA, None]):
            r = rpv.comprobar_cuentas(reintentar=True)
        self.assertEqual(r, [("A", None), ("B", None)])
        self.assertEqual([c for c in self.llamadas if c[0] == "k2"], [("k2", rpv.TTL_SEGUNDO_PLANO), ("k2", 0)])   # el reintento ignora la copia
        self.assertEqual([c for c in self.llamadas if c[0] == "k1"], [("k1", rpv.TTL_SEGUNDO_PLANO)])               # la cuenta sana no se vuelve a pedir
        rpv.time.sleep.assert_called_once_with(rpv.PAUSA_REINTENTO)

    def test_si_sigue_fallando_se_devuelve_el_error(self):
        with self.respuestas(k1=[None], k2=[TIMEOUT_CASA, TIMEOUT_CASA]):
            r = rpv.comprobar_cuentas(reintentar=True)
        self.assertEqual(r[1], ("B", TIMEOUT_CASA))

    def test_sin_reintentar_no_se_reintenta(self):
        with self.respuestas(k1=[None], k2=[TIMEOUT_CASA]):
            r = rpv.comprobar_cuentas()
        self.assertEqual(r[1], ("B", TIMEOUT_CASA))
        rpv.time.sleep.assert_not_called()

    def test_credenciales_y_limite_no_se_reintentan(self):
        for error in ("API key inválida (401)", "429 — límite de peticiones de RPV (pausa de 2 min)", "Endpoint de partes no encontrado (404)"):
            self.llamadas.clear()
            with self.respuestas(k1=[None], k2=[error]):
                r = rpv.comprobar_cuentas(reintentar=True)
            self.assertEqual(r[1], ("B", error))
            self.assertEqual(len(self.llamadas), 2, error)

    def test_si_la_primera_pasada_fue_lenta_no_se_reintenta(self):
        # Las dos cuentas agotaron su timeout (20 s): reintentar pasaría de los 60 s de la petición del watchdog.
        reloj = mock.Mock(side_effect=[0.0, 21.0])
        with self.respuestas(k1=[TIMEOUT_CASA], k2=[TIMEOUT_HOSTAL]):
            r = rpv.comprobar_cuentas(reintentar=True, reloj=reloj)
        self.assertEqual(r, [("A", TIMEOUT_CASA), ("B", TIMEOUT_HOSTAL)])
        rpv.time.sleep.assert_not_called()
        self.assertEqual(len(self.llamadas), 2)


class ChequeoDelWatchdog(unittest.TestCase):
    def comprobar(self, cuentas):
        with mock.patch.object(wd, "comprobar_cuentas", return_value=cuentas):
            return wd._comprobar_rpv()

    def test_todo_bien(self):
        resultado, problemas = self.comprobar([("hostal", None), ("Casa", None)])
        self.assertEqual((resultado["ok"], problemas), (True, []))

    def test_un_timeout_es_un_aviso_no_un_critico_y_no_habla_de_claves(self):
        resultado, problemas = self.comprobar([("hostal", None), ("Casa", TIMEOUT_CASA)])
        self.assertFalse(resultado["ok"])
        self.assertEqual(problemas, [wd._RPV_LENTO])
        nivel, descripcion, accion = problemas[0]
        self.assertEqual(nivel, "warning")
        self.assertNotIn("Casa", descripcion)
        self.assertNotIn("timed out", descripcion)

    def test_la_secuencia_del_17_de_noviembre_da_siempre_el_mismo_aviso(self):
        """Casa sola, hostal y Casa, hostal solo: antes eran firmas distintas (un aviso nuevo en cada cambio); ahora una sola."""
        firmas = set()
        for cuentas in ([("hostal", None), ("Casa", TIMEOUT_CASA)],
                        [("Playa Lanuza y 4 más", TIMEOUT_HOSTAL), ("Casa", TIMEOUT_CASA)],
                        [("Playa Lanuza y 4 más", TIMEOUT_HOSTAL), ("Casa", None)]):
            _, problemas = self.comprobar(cuentas)
            firmas.add("|".join(sorted(d for _, d, _ in problemas)))
        self.assertEqual(len(firmas), 1)

    def test_un_401_si_es_critico_y_nombra_la_cuenta(self):
        resultado, problemas = self.comprobar([("hostal", None), ("Casa", "API key inválida (401)")])
        nivel, descripcion, accion = problemas[0]
        self.assertEqual(nivel, "critico")
        self.assertIn("Casa", descripcion)
        self.assertIn("RPV_API_KEY", accion)

    def test_un_cambio_de_api_es_critico_pero_no_manda_a_las_claves(self):
        _, problemas = self.comprobar([("Casa", "Endpoint de partes no encontrado (404)")])
        self.assertEqual(problemas, [wd._RPV_CONTRATO])
        self.assertEqual(problemas[0][0], "critico")
        self.assertIn("No son las claves", problemas[0][2])

    def test_el_limite_429_mantiene_su_texto(self):
        _, problemas = self.comprobar([("Casa", "429 — límite de peticiones de RPV (pausa de 2 min)")])
        self.assertEqual(problemas, [wd._RPV_LIMITADO])
        self.assertEqual(problemas[0][1], "RPV API está limitando peticiones (429)")   # la misma descripción que antes: la firma no cambia

    def test_mezcla_de_fallos(self):
        _, problemas = self.comprobar([("hostal", "API key inválida (401)"), ("Casa", TIMEOUT_CASA)])
        self.assertEqual([p[0] for p in problemas], ["critico", "warning"])


if __name__ == "__main__":
    unittest.main()

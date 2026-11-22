"""Los códigos de acceso comunes no están escritos en config.py: solo salen de variables de entorno (Render)."""
import importlib
import os
import re
import sys
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)


def leer(*partes):
    with open(os.path.join(RAIZ, *partes), encoding="utf-8") as f:
        return f.read()


class CodigosFueraDelRepo(unittest.TestCase):
    def test_config_no_lleva_los_numeros(self):
        texto = leer("config.py")
        for numero in ("130773", "2308"):
            self.assertNotIn(numero, texto)

    def test_sin_las_variables_los_codigos_estan_vacios(self):
        import config
        antes = {k: os.environ.pop(k, None) for k in ("PIN_PORTAL", "CODIGO_URBANIZACION")}
        try:
            importlib.reload(config)
            self.assertEqual((config.CODIGO_PORTAL, config.CODIGO_URBANIZACION), ("", ""))
        finally:
            for k, v in antes.items():
                if v is not None:
                    os.environ[k] = v
            importlib.reload(config)

    def test_el_codigo_fuente_no_trae_el_del_portal_escrito(self):
        for carpeta in ("routes", "services"):
            for nombre in os.listdir(os.path.join(RAIZ, carpeta)):
                if nombre.endswith(".py"):
                    self.assertIsNone(re.search(r"\b130773\b", leer(carpeta, nombre)), f"{carpeta}/{nombre}")


if __name__ == "__main__":
    unittest.main()

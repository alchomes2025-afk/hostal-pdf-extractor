"""Arreglos de la revisión general de facturas: fórmulas en la hoja, proveedor = propia empresa e importes con punto de miles."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import facturas, puente_plataforma


class Revision(unittest.TestCase):
    # ── hoja de registro: nunca fórmulas ──
    def test_celda_antepone_apostrofo_a_lo_que_parece_formula(self):
        for peligro in ("=IMPORTXML(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)"):
            self.assertEqual(facturas._celda(peligro), "'" + peligro)

    def test_celda_no_toca_textos_normales_ni_numeros(self):
        for v in ("Iberdrola", "2026-10-14", 12.5, 0, None, "", "https://drive/x"):
            self.assertEqual(facturas._celda(v), v)

    def test_guardar_manda_la_fila_saneada(self):
        enviado = {}

        def guardar(raiz, ruta, nombre, mime, contenido, registro=None, descripcion=None):
            enviado["fila"] = registro["fila"]
            return {"file_id": "F", "url": "https://drive/f", "nombre": nombre, "hoja_url": "https://h"}
        datos = {"tipo": "gasto", "proveedor": "=HYPERLINK(\"http://malo\",\"x\")", "fecha": "2026-10-14", "total": 10, "base_imponible": 8.26, "iva_pct": 21,
                 "cuota_iva": 1.74, "concepto": "@cmd", "numero": "-1"}
        with mock.patch.object(facturas, "habilitado", lambda c: True), mock.patch.object(facturas, "raiz_drive", lambda c: "R"), \
             mock.patch.object(facturas, "_archivo", lambda a: (b"x", "application/pdf")), mock.patch.object(facturas, "ya_guardada", lambda *a: None), \
             mock.patch.object(facturas, "_coleccion", lambda: None), mock.patch.object(puente_plataforma, "guardar", guardar):
            facturas.guardar("alc-homes", "349341", {}, datos, False)
        fila = enviado["fila"]
        self.assertTrue(fila[2].startswith("'="))      # proveedor
        self.assertEqual(fila[5], "'@cmd")             # concepto
        self.assertEqual(fila[4], "'-1")               # número
        self.assertEqual(fila[12], 10.0)               # el total sigue siendo número

    # ── proveedor = la propia empresa ──
    def test_detecta_la_propia_empresa_por_nombre(self):
        c = facturas.CLIENTES["alc-homes"]
        for nombre in ("Alc Homes 2025 S.L", "ALC HOMES 2025 S.L.", "alc-homes"):
            self.assertTrue(facturas.es_propia_empresa(c, {"proveedor": nombre}), nombre)
        for nombre in ("Booking.com B.V.", "IKEA Ibérica S.A.", "", None):
            self.assertFalse(facturas.es_propia_empresa(c, {"proveedor": nombre}), nombre)

    def test_detecta_la_propia_empresa_por_nif(self):
        c = {"nombres_fiscales": ["Mi Negocio"], "nifs_propios": ["B12345678"]}
        self.assertTrue(facturas.es_propia_empresa(c, {"proveedor": "Otro nombre", "nif": " b12345678 "}))
        self.assertFalse(facturas.es_propia_empresa(c, {"proveedor": "Otro nombre", "nif": "A00000000"}))

    def test_leer_avisa_y_no_sale_lista(self):
        with mock.patch.object(facturas, "comprobar_propiedad", lambda *a: "349341"), mock.patch.object(facturas, "_archivo", lambda a: (b"x", "application/pdf")), \
             mock.patch.object(facturas, "ya_guardada", lambda *a: None), mock.patch.object(facturas, "misma_factura", lambda *a: None), \
             mock.patch.object(facturas, "texto_de", lambda c, m: "FACTURA 14/10/2026 total 10 euros"), \
             mock.patch.object(facturas, "_preguntar_ia", lambda p: {"tipo": "gasto", "emisor": "Alc Homes 2025 S.L", "numero": "1", "fecha": "2026-10-14",
                                                                    "base_imponible": 8.26, "iva_pct": 21, "cuota_iva": 1.74, "total": 10}):
            r = facturas.leer("alc-homes", "349341", {}, True)
        self.assertIn("propia empresa", r["avisos"][0])        # con avisos, la app no la da por «lista para guardar»

    # ── importes con punto de miles ──
    def test_punto_de_miles(self):
        for texto, valor in (("1.815", 1815.0), ("12.345.678", 12345678.0), ("1.815,50", 1815.5), ("1,815.50", 1815.5), ("158,93 €", 158.93),
                             ("1234.56", 1234.56), ("0.5", 0.5), ("1.5", 1.5), (1815, 1815.0), ("-1.200", -1200.0)):
            self.assertEqual(facturas.a_numero(texto), valor, texto)


class Gunicorn(unittest.TestCase):
    def test_procfile_con_hilos_para_no_bloquearse_con_lecturas_largas(self):
        ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Procfile")
        linea = open(ruta, encoding="utf-8").read()
        self.assertIn("--worker-class gthread", linea)
        self.assertIn("--threads 8", linea)
        self.assertIn("--workers 1", linea)      # un solo proceso: los contadores y cachés en memoria siguen valiendo


if __name__ == "__main__":
    unittest.main()

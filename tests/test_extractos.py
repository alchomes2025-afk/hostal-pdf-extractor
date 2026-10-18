"""Extractos bancarios (PDF de BBVA y CSV) y cotejo con las facturas guardadas."""
import base64
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_facturas
import mobile_routes
from services import extractos, facturas, puente_plataforma

# Líneas (top, texto) como las devuelve pdfplumber del «Extracto Mensual» de BBVA: ~25 pt entre movimientos y menos de 14 dentro de uno
LINEAS = [
    (50, "1/3"), (60, "Extracto Mensual"), (70, "Titular EMPRESA DE PRUEBA S.L. Periodo Agosto 2026"), (80, "Cuenta ES00 0000 0000 0000 0000 Fecha 16/10/2026"),
    (253.5, "F. CONTABLE F. VALOR CONCEPTO IMPORTE SALDO"),
    (279.2, "TRANSFERENCIAS"), (284.6, "31/08/2026 31/08/2026 59,00 EUR 9.472,21 EUR"), (292.0, "RESERVA"),
    (316.7, "CUOTAS DE LA SEGURIDAD SOCIAL"), (322.1, "31/08/2026 31/08/2026 -354,26 EUR 9.413,21 EUR"), (329.5, "N 2026239001332107 TGSS. COTIZACION 001"),
    (354.2, "PAGO CON TARJETA EN HOGAR, MUEBLES,"), (363.6, "24/08/2026 21/08/2026 DECORACION Y ELECTR -308,9 EUR 9.100,00 EUR"), (376.4, "************0698 Werkhaus SL, SCS"),
    (401.2, "PAGO CON TARJETA EN HOGAR, MUEBLES,"), (410.6, "24/08/2026 23/08/2026 DECORACION Y ELECTR -61,15 EUR 9.038,85 EUR"), (423.3, "************0698 SHOPPING CHEN S.L. SANTA POLA ES"),
    (448.1, "ABONO POR TRANSFERENCIA A SU FAVOR RECIBIDA"), (453.5, "28/08/2026 28/08/2026 EN EUROS 2.157,43 EUR 11.196,28 EUR"), (460.8, "LIQ. OP. N 000362083490001"),
    # página 2: vuelve a empezar arriba
    (50, "2/3"), (60, "Extracto Mensual"), (70, "F. CONTABLE F. VALOR CONCEPTO IMPORTE SALDO"),
    (100.0, "RET. EFECTIVO A DEBITO CON TARJ. EN CAJERO. AUT."), (110.0, "21/08/2026 21/08/2026 -1.000,00 EUR 10.196,28 EUR"), (120.0, "************1943 01820301 999"),
    (145.0, "PAGO CON TARJETA EN HOGAR, MUEBLES,"), (155.0, "21/08/2026 19/08/2026 DECORACION Y ELECTR 28,00 EUR 10.224,28 EUR"), (167.0, "************0698 IKEA IBERICA WEB"),
]


class LectorPdf(unittest.TestCase):
    def test_lee_los_movimientos_con_importe_signo_y_concepto(self):
        movs = extractos.movimientos_de_lineas(LINEAS)
        self.assertEqual(len(movs), 7)
        self.assertEqual([m["importe"] for m in movs], [59.0, -354.26, -308.9, -61.15, 2157.43, -1000.0, 28.0])
        w = movs[2]
        self.assertEqual((w["fecha"], w["contable"]), ("2026-08-21", "2026-08-24"))          # la fecha de la operación es la F. VALOR
        self.assertIn("Werkhaus", w["concepto"])
        self.assertIn("HOGAR, MUEBLES", w["concepto"])
        self.assertIn("DECORACION Y ELECTR", w["concepto"])                                   # el texto intermedio de la fila de fechas se conserva

    def test_etiquetas(self):
        e = [m["etiqueta"] for m in extractos.movimientos_de_lineas(LINEAS)]
        self.assertEqual(e, ["", "Seguridad Social", "", "", "Cobro por transferencia", "Efectivo (cajero)", ""])

    def test_la_cabecera_del_pdf_no_se_cuela_como_movimiento(self):
        for m in extractos.movimientos_de_lineas(LINEAS):
            self.assertNotIn("Titular", m["concepto"])
            self.assertNotIn("Extracto Mensual", m["concepto"])

    def test_parece_extracto(self):
        texto = "\n".join(t for _, t in LINEAS)
        self.assertEqual(extractos.parece_extracto(texto), 7)
        self.assertEqual(extractos.parece_extracto("FACTURA 1073\nFecha 19/08/2026\nTotal 60,00 EUR"), 0)     # una factura no lo parece
        self.assertEqual(extractos.parece_extracto("31/08/2026 31/08/2026 59,00 EUR 9.472,21 EUR\n" * 2), 0)   # con menos de 3 líneas no se da por extracto

    def test_pdf_sin_movimientos_explica_el_formato(self):
        with mock.patch.object(extractos, "_lineas_pdf", lambda c: [(10, "hola")]):
            with self.assertRaises(extractos.ExtractoError) as e:
                extractos.leer_movimientos(b"%PDF", "application/pdf")
        self.assertIn("BBVA", str(e.exception))


class LectorCsv(unittest.TestCase):
    def test_csv_con_importe_unico_punto_y_coma(self):
        csv = "Fecha;Concepto;Importe;Saldo\n21/08/2026;PAGO TARJETA LEROY MERLIN;-83,46;7.465,52\n22/08/2026;ABONO RESERVA;59,00;7.524,52\n"
        movs = extractos.leer_movimientos(csv.encode("utf-8"), "text/csv")
        self.assertEqual([(m["fecha"], m["importe"]) for m in movs], [("2026-08-21", -83.46), ("2026-08-22", 59.0)])
        self.assertEqual(movs[0]["saldo"], 7465.52)

    def test_csv_con_cargo_y_abono_en_columnas_y_latin1(self):
        csv = "Fecha operación,Descripción,Cargo,Abono\n2026-08-21,Compra almacén,45.30,\n2026-08-22,Devolución,,10.00\n".encode("cp1252")
        movs = extractos.leer_movimientos(csv, "application/vnd.ms-excel")
        self.assertEqual([m["importe"] for m in movs], [-45.3, 10.0])

    def test_csv_con_filas_de_titulo_antes_de_la_cabecera(self):
        csv = "Extracto de cuenta\nTitular: X\n\nFecha;Concepto;Importe\n01/08/2026;Algo;-5,00\n"
        self.assertEqual(len(extractos.leer_movimientos(csv.encode(), "text/csv")), 1)

    def test_csv_sin_columnas_utiles_y_excel_binario(self):
        with self.assertRaises(extractos.ExtractoError):
            extractos.leer_movimientos(b"a;b;c\n1;2;3\n", "text/csv")
        with self.assertRaises(extractos.ExtractoError):
            extractos.leer_movimientos(b"PK\x03\x04xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        with self.assertRaises(extractos.ExtractoError) as e:
            extractos.leer_movimientos(b"\xd0\xcf\x11\xe0 xls binario", "application/vnd.ms-excel")
        self.assertIn("CSV", str(e.exception))


def mov(fecha, importe, concepto):
    return {"fecha": fecha, "contable": fecha, "importe": importe, "saldo": None, "concepto": concepto, "etiqueta": extractos._etiqueta(concepto)}


def fac(proveedor, total, fecha, tipo="gasto", **extra):
    return {"tipo": tipo, "proveedor": proveedor, "total": total, "fecha": fecha, "numero": "1", **extra}


class Cotejo(unittest.TestCase):
    def test_casa_por_importe_fecha_y_nombre(self):
        movs = [mov("2026-08-21", -83.46, "PAGO TARJETA ADEO LEROY MERLIN SPAIN"), mov("2026-08-23", -61.15, "SHOPPING CHEN S.L. SANTA POLA")]
        r = extractos.cotejar(movs, [fac("Leroy Merlin España SLU", 83.46, "2026-08-21"), fac("SHOPPING CHEN,S.L.", 61.15, "2026-08-23")])
        self.assertEqual(r["resumen"]["conciliadas"], 2)
        self.assertTrue(all(c["confianza"] == "alta" for c in r["conciliadas"]))
        self.assertEqual(r["facturas_sin_movimiento"], [])
        self.assertEqual(r["movimientos_sin_factura"], [])

    def test_mismo_importe_gana_el_proveedor_que_coincide(self):
        movs = [mov("2026-08-20", -50.0, "PAGO TARJETA OTRO COMERCIO"), mov("2026-08-22", -50.0, "PAGO TARJETA JYSK ISLA DE CORFU")]
        r = extractos.cotejar(movs, [fac("JYSK DBL IBERIA SLU", 50.0, "2026-08-20")])
        (c,) = r["conciliadas"]
        self.assertIn("JYSK", c["movimiento"]["concepto"])           # aunque el otro está más cerca en fecha, el nombre manda
        self.assertEqual(c["confianza"], "alta")

    def test_sin_nombre_en_comun_es_confianza_media(self):
        r = extractos.cotejar([mov("2026-08-21", -99.0, "TRANSFERENCIA A FAVOR DE J PEREZ")], [fac("Servicios Acme", 99.0, "2026-08-21")])
        self.assertEqual(r["conciliadas"][0]["confianza"], "media")

    def test_un_movimiento_solo_casa_con_una_factura(self):
        r = extractos.cotejar([mov("2026-08-21", -10.0, "COMERCIO X")], [fac("Comercio X", 10.0, "2026-08-21"), fac("Comercio X", 10.0, "2026-08-21")])
        self.assertEqual(r["resumen"]["conciliadas"], 1)
        self.assertEqual(r["resumen"]["facturas_sin_movimiento"], 1)

    def test_ventana_de_fechas_y_importe_exacto(self):
        movs = [mov("2026-10-05", -10.0, "COMERCIO X"), mov("2026-08-21", -10.5, "COMERCIO X")]
        r = extractos.cotejar(movs, [fac("Comercio X", 10.0, "2026-08-01")])
        self.assertEqual(r["resumen"]["conciliadas"], 0)             # 60+ días después, o importe distinto: no casan
        self.assertEqual(r["resumen"]["facturas_sin_movimiento"], 1)

    def test_signos_gasto_devolucion_e_ingreso(self):
        movs = [mov("2026-08-21", 28.0, "DEVOLUCION IKEA"), mov("2026-08-22", 120.0, "COBRO BOOKING")]
        r = extractos.cotejar(movs, [fac("IKEA", 28.0, "2026-08-19", es_rectificativa=True), fac("Booking", 120.0, "2026-08-20", tipo="ingreso")])
        self.assertEqual(r["resumen"]["conciliadas"], 2)
        r = extractos.cotejar([mov("2026-08-21", 28.0, "ABONO RARO")], [fac("IKEA", 28.0, "2026-08-19")])   # un gasto NO casa con un abono
        self.assertEqual(r["resumen"]["conciliadas"], 0)

    def test_cargos_y_abonos_sin_factura(self):
        movs = [mov("2026-08-03", -800.0, "RET. EFECTIVO A DEBITO CON TARJ."), mov("2026-08-03", -853.69, "TRANSFERENCIAS NOMINA"), mov("2026-08-28", 2157.43, "ABONO POR TRANSFERENCIA")]
        r = extractos.cotejar(movs, [])
        self.assertEqual(r["resumen"]["cargos_sin_factura"], 2)
        self.assertEqual(r["resumen"]["importe_cargos_sin_factura"], 1653.69)
        self.assertEqual(r["resumen"]["abonos_sin_factura"], 1)
        self.assertEqual({m["etiqueta"] for m in r["movimientos_sin_factura"]}, {"Efectivo (cajero)", "Nómina"})


class Doc:
    def __init__(self, d): self.d = d
    def to_dict(self): return dict(self.d)


class Col:
    def __init__(self, docs): self.docs, self.f = docs, ()
    def where(self, c, op, v):
        n = Col(self.docs)
        n.f = self.f + ((c, v),)
        return n
    def stream(self): return [Doc(d) for d in self.docs if all(d.get(c) == v for c, v in self.f)]


class Endpoint(unittest.TestCase):
    PIN, ADMIN = "1111", "9999"

    def setUp(self):
        self.guardados = []
        docs = [{"cliente": "alc-homes", "propiedad_id": "349341", "tipo": "gasto", "proveedor": "SHOPPING CHEN,S.L.", "numero": "1", "total": 61.15, "fecha": "2026-08-23"},
                {"cliente": "alc-homes", "propiedad_id": "339751", "tipo": "gasto", "proveedor": "Werkhaus", "numero": "2", "total": 308.9, "fecha": "2026-08-21"},
                {"cliente": "otro", "propiedad_id": "349341", "tipo": "gasto", "proveedor": "Otro cliente", "numero": "3", "total": 7.0, "fecha": "2026-08-21"}]
        for o, n, v in ((mobile_routes, "MOBILE_APP_PIN", self.PIN), (mobile_routes, "MOBILE_APP_PIN_ADMIN", self.ADMIN), (facturas, "_coleccion", lambda: Col(docs)),
                        (facturas, "habilitado", lambda c: True), (facturas, "raiz_drive", lambda c: "R"), (puente_plataforma, "guardar", self._guardar),
                        (extractos, "_lineas_pdf", lambda c: LINEAS)):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(mobile_facturas.facturas_bp)
        self.cliente = app.test_client()

    def _guardar(self, raiz, ruta, nombre, mime, contenido, registro=None, descripcion=None):
        self.guardados.append((ruta, nombre))
        return {"file_id": "F", "url": "https://d", "nombre": nombre, "hoja_url": None}

    def _post(self, pin, nombre="Extracto mensual agosto.pdf", mime="application/pdf", contenido=b"%PDF-1.4"):
        return self.cliente.post("/mobile/facturas/cotejar", json={"propiedad_id": "349341", "usuario": "M", "archivo": {"nombre": nombre, "mime": mime, "base64": base64.b64encode(contenido).decode()}},
                                 headers={"x-app-pin": pin} if pin else {})

    def test_cotejo_con_pin_normal_solo_ve_las_facturas_de_su_propiedad(self):
        r = self._post(self.PIN).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["resumen"]["movimientos"], 7)
        self.assertEqual(r["resumen"]["conciliadas"], 1)
        self.assertEqual(r["conciliadas"][0]["factura"]["propiedad"], "La Casa de la Primavera")
        self.assertEqual(r["facturas_consideradas"], 1)                                   # la del hostal y la de otro cliente no cuentan
        self.assertTrue(r["extracto_guardado"])
        self.assertEqual(self.guardados[0][0][-1], "Extractos")

    def test_con_pin_de_admin_se_ven_las_dos_propiedades(self):
        r = self._post(self.ADMIN).get_json()
        self.assertEqual(r["facturas_consideradas"], 2)
        self.assertEqual(r["resumen"]["conciliadas"], 2)

    def test_sin_pin(self):
        self.assertEqual(self._post(None).status_code, 401)

    def test_formato_no_entendido_se_guarda_y_se_explica(self):
        r = self._post(self.PIN, nombre="mov.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", contenido=b"PK\x03\x04")
        self.assertEqual(r.status_code, 422)
        self.assertIn("quedado guardado", r.get_json()["error"])

    def test_leer_reconoce_un_extracto_entre_facturas_sin_gastar_ia(self):
        texto = "\n".join(t for _, t in LINEAS)
        with mock.patch.object(facturas, "comprobar_propiedad", lambda *a: "349341"), mock.patch.object(facturas, "_archivo", lambda a: (b"x", "application/pdf")), \
             mock.patch.object(facturas, "ya_guardada", lambda *a: None), mock.patch.object(facturas, "capa_de_texto", lambda c, m: texto), \
             mock.patch.object(facturas, "_preguntar_ia") as ia, mock.patch.object(facturas, "_preguntar_ia_vision") as vision:
            r = facturas.leer("alc-homes", "349341", {}, True)
        self.assertEqual((r["extracto"], r["movimientos"]), (True, 7))
        ia.assert_not_called()
        vision.assert_not_called()


if __name__ == "__main__":
    unittest.main()

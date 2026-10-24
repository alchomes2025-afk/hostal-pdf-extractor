"""Editar y eliminar facturas ya guardadas (con cambio de propiedad), con sus permisos."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import mobile_facturas
import mobile_routes
from services import facturas, puente_plataforma


class Doc:
    def __init__(self, a, k): self.a, self.k = a, k
    def get(self):
        d = self.a.get(self.k)
        return type("S", (), {"exists": d is not None, "to_dict": lambda s: dict(d) if d else {}})()
    def set(self, datos): self.a[self.k] = dict(datos)
    def delete(self): self.a.pop(self.k, None)


class Col:
    def __init__(self, a): self.a = a
    def document(self, k): return Doc(self.a, k)


FID = "alc-homes-" + "a" * 32
BASE = {"cliente": "alc-homes", "propiedad_id": "349341", "tipo": "gasto", "fecha": "2026-08-23", "proveedor": "SHOPPING CHEN,S.L.", "nif": "B1", "numero": "1",
        "concepto": "Cojines", "categoria": "Otros gastos", "base_imponible": 50.54, "iva_pct": 21.0, "cuota_iva": 10.61, "retencion_pct": 0.0, "retencion": 0.0, "total": 61.15,
        "es_rectificativa": False, "anio": "2026", "mes": "2026-08", "trimestre": "3T", "drive_id": "DRIVE1", "archivo": "2026-08-23_SHOPPING CHEN,S.L._61,15€.pdf",
        "url": "https://drive/old", "hoja_url": "https://hoja", "huella": "a" * 64, "usuario": "Mónica", "subida": "2026-10-18 10:00"}
FORM = {"tipo": "gasto", "fecha": "2026-08-23", "proveedor": "SHOPPING CHEN, S.L.", "numero": "1", "concepto": "Cojines y fundas", "categoria": "Mobiliario y equipamiento",
        "base_imponible": "50,54", "iva_pct": "21", "cuota_iva": "10,61", "total": "61,15"}


class Base(unittest.TestCase):
    PIN, ADMIN = "1111", "1379"

    def setUp(self):
        self.db = {FID: dict(BASE)}
        self.llamadas = []
        for o, n, v in ((mobile_routes, "MOBILE_APP_PIN", self.PIN), (mobile_routes, "MOBILE_APP_PIN_ADMIN", self.ADMIN), (facturas, "_coleccion", lambda: Col(self.db)),
                        (facturas, "habilitado", lambda c: True), (facturas, "raiz_drive", lambda c: "RAIZ"),
                        (puente_plataforma, "actualizar", self._actualizar), (puente_plataforma, "eliminar", self._eliminar), (puente_plataforma, "descargar", self._descargar)):
            p = mock.patch.object(o, n, v)
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(mobile_facturas.facturas_bp)
        self.cliente = app.test_client()
        self.falla = False

    def _actualizar(self, raiz, file_id, ruta, nombre, viejo, nuevo):
        if self.falla:
            raise puente_plataforma.PuenteError("caído")
        self.llamadas.append(("actualizar", raiz, file_id, ruta, nombre, viejo, nuevo))
        return {"url": "https://drive/new", "nombre": nombre, "hoja_url": "https://hoja2"}

    def _eliminar(self, raiz, file_id, registro):
        if self.falla:
            raise puente_plataforma.PuenteError("caído")
        self.llamadas.append(("eliminar", raiz, file_id, registro))
        return {"ok": True}

    def _descargar(self, raiz, file_id):
        if self.falla:
            raise puente_plataforma.PuenteError("caído")
        return {"nombre": "x.pdf", "mime": "application/pdf", "base64": "JVBER"}

    def _post(self, ruta, pin, **cuerpo):
        return self.cliente.post(f"/mobile/facturas/{ruta}", json=cuerpo, headers={"x-app-pin": pin} if pin else {})


class Editar(Base):
    def test_corrige_los_datos_y_renombra_el_archivo(self):
        r = self._post("editar", self.ADMIN, id=FID, propiedad_id="349341", datos=FORM, usuario="Mónica")
        self.assertEqual(r.status_code, 200)
        _, raiz, file_id, ruta, nombre, viejo, nuevo = self.llamadas[0]
        self.assertEqual((raiz, file_id), ("RAIZ", "DRIVE1"))
        self.assertEqual(ruta, ["Facturas", "La Casa de la Primavera", "2026", "3T", "Gastos"])
        self.assertEqual(nombre, "2026-08-23_SHOPPING CHEN, S.L._61,15€.pdf")           # la extensión se conserva
        self.assertEqual(viejo, {"ruta": ["Facturas", "La Casa de la Primavera", "2026"], "titulo": "Registro de facturas 2026", "id": FID})
        self.assertEqual(nuevo["fila"][-1], FID)                                        # la fila nueva lleva el mismo ID
        guardada = self.db[FID]
        self.assertEqual((guardada["proveedor"], guardada["concepto"], guardada["categoria"]), ("SHOPPING CHEN, S.L.", "Cojines y fundas", "Mobiliario y equipamiento"))
        self.assertEqual(guardada["huella"], "a" * 64)                                  # no cambia la identidad del archivo
        self.assertEqual(guardada["editada_por"], "Mónica")

    def test_cambiar_la_fecha_mueve_de_trimestre_y_año(self):
        self._post("editar", self.ADMIN, id=FID, propiedad_id="349341", datos={**FORM, "fecha": "2027-01-05"})
        _, _, _, ruta, _, viejo, nuevo = self.llamadas[0]
        self.assertEqual(ruta[2:], ["2027", "1T", "Gastos"])
        self.assertEqual(viejo["titulo"], "Registro de facturas 2026")                  # se quita de la hoja del año viejo...
        self.assertEqual(nuevo["titulo"], "Registro de facturas 2027")                  # ...y se añade a la del nuevo
        self.assertEqual((self.db[FID]["mes"], self.db[FID]["trimestre"]), ("2027-01", "1T"))

    def test_cambiar_de_propiedad_solo_con_permiso(self):
        r = self._post("editar", self.PIN, id=FID, propiedad_id="339751", datos=FORM)    # el PIN normal no puede mover al hostal
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.llamadas, [])
        r = self._post("editar", self.ADMIN, id=FID, propiedad_id="339751", datos=FORM)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.llamadas[0][3][1], "Hostal ALC Homes")
        self.assertEqual(self.db[FID]["propiedad_id"], "339751")

    def test_con_pin_normal_no_se_edita_una_factura_del_hostal(self):
        self.db[FID]["propiedad_id"] = "339751"
        self.assertEqual(self._post("editar", self.PIN, id=FID, datos=FORM).status_code, 403)

    def test_validaciones_y_errores(self):
        self.assertEqual(self._post("editar", None, id=FID, datos=FORM).status_code, 401)
        self.assertEqual(self._post("editar", self.ADMIN, id="alc-homes-inventada", datos=FORM).status_code, 404)
        self.assertEqual(self._post("editar", self.ADMIN, id="otro-cliente-" + "a" * 32, datos=FORM).status_code, 404)
        self.assertEqual(self._post("editar", self.ADMIN, id=FID, datos={**FORM, "total": "", "base_imponible": "", "cuota_iva": ""}).status_code, 400)       # sin total ni de dónde deducirlo
        self.falla = True
        antes = dict(self.db[FID])
        self.assertEqual(self._post("editar", self.ADMIN, id=FID, datos=FORM).status_code, 502)
        self.assertEqual(self.db[FID], antes)                                           # si el Drive falla, no se toca el índice


class Eliminar(Base):
    def test_borra_archivo_fila_e_indice(self):
        r = self._post("eliminar", self.ADMIN, id=FID, usuario="Mónica")
        self.assertEqual(r.status_code, 200)
        _, raiz, file_id, registro = self.llamadas[0]
        self.assertEqual((raiz, file_id, registro["id"]), ("RAIZ", "DRIVE1", FID))
        self.assertEqual(registro["ruta"], ["Facturas", "La Casa de la Primavera", "2026"])
        self.assertNotIn(FID, self.db)                                                  # ya no cuenta ni aparece, y se puede volver a subir

    def test_permisos_y_errores(self):
        self.assertEqual(self._post("eliminar", None, id=FID).status_code, 401)
        self.db[FID]["propiedad_id"] = "339751"
        self.assertEqual(self._post("eliminar", self.PIN, id=FID).status_code, 403)
        self.assertIn(FID, self.db)
        self.assertEqual(self._post("eliminar", self.ADMIN, id="alc-homes-nada").status_code, 404)
        self.falla = True
        self.assertEqual(self._post("eliminar", self.ADMIN, id=FID).status_code, 502)
        self.assertIn(FID, self.db)                                                     # si el Drive falla, no se borra el índice


class Lista(unittest.TestCase):
    def test_la_lista_trae_lo_necesario_para_editar(self):
        db = {FID: dict(BASE)}
        with mock.patch.object(facturas, "_coleccion", lambda: type("C", (), {"where": lambda s, *a: s, "stream": lambda s: [type("D", (), {"id": FID, "to_dict": lambda s: dict(BASE)})()]})()):
            r = facturas.listar("alc-homes", "349341", "2026-08", False)
        f = r["facturas"][0]
        for campo in ("id", "propiedad_id", "retencion_pct", "archivo", "nif", "categoria", "concepto"):
            self.assertIn(campo, f)


if __name__ == "__main__":
    unittest.main()


class Original(Base):
    def test_devuelve_el_documento(self):
        r = self.cliente.get(f"/mobile/facturas/original?id={FID}", headers={"x-app-pin": self.ADMIN})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["mime"], "application/pdf")

    def test_sin_pin_no(self):
        self.assertEqual(self.cliente.get(f"/mobile/facturas/original?id={FID}").status_code, 401)

    def test_propiedad_solo_admin_no_para_pin_normal(self):
        self.db[FID]["propiedad_id"] = "339751"
        r = self.cliente.get(f"/mobile/facturas/original?id={FID}", headers={"x-app-pin": self.PIN})
        self.assertGreaterEqual(r.status_code, 400)
        r = self.cliente.get(f"/mobile/facturas/original?id={FID}", headers={"x-app-pin": self.ADMIN})
        self.assertEqual(r.status_code, 200)

    def test_drive_caido_da_502(self):
        self.falla = True
        r = self.cliente.get(f"/mobile/facturas/original?id={FID}", headers={"x-app-pin": self.ADMIN})
        self.assertEqual(r.status_code, 502)

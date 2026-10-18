"""Ejecutar desde la raíz del repo: python -X utf8 -I pruebas/sim_facturas.py
Facturas: lectura, comprobaciones, guardado en el Drive (puente simulado), duplicados, permisos y listado."""
import base64
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

FALLOS = []


def comprobar(nombre, cond):
    print(("ok:    " if cond else "FALLO: ") + nombre)
    if not cond:
        FALLOS.append(nombre)


class Doc:
    def __init__(self, a, k): self.a, self.k = a, k
    def get(self):
        d = self.a.get(self.k)
        return type("S", (), {"exists": d is not None, "to_dict": lambda s: dict(d) if d else {}})()
    def set(self, datos): self.a[self.k] = dict(datos)


class Consulta:
    def __init__(self, a, filtros=()): self.a, self.f = a, filtros
    def where(self, campo, op, valor): return Consulta(self.a, self.f + ((campo, valor),))
    def stream(self):
        for k, v in self.a.items():
            if all(v.get(c) == val for c, val in self.f):
                yield type("D", (), {"id": k, "to_dict": lambda s, v=v: dict(v)})()


class Col(Consulta):
    def __init__(self, a): super().__init__(a)
    def document(self, nombre): return Doc(self.a, nombre)


class FakeDB:
    def __init__(self): self.datos = {}
    def collection(self, n): return Col(self.datos)


config.db = FakeDB()
from services import puente_plataforma, facturas  # noqa: E402

facturas.FACTURAS_DRIVE_RAIZ_ALC = "RAIZ"
facturas.CLIENTES["alc-homes"]["drive_raiz"] = "RAIZ"
puente_plataforma.configurado = lambda: True
facturas.GROQ_API_KEY = "x"

GUARDADOS, OCR = [], []


def guardar_falso(raiz, ruta, nombre, mime, contenido, registro=None, descripcion=None):
    GUARDADOS.append({"raiz": raiz, "ruta": ruta, "nombre": nombre, "mime": mime, "n": len(contenido), "registro": registro})
    return {"file_id": f"F{len(GUARDADOS)}", "url": f"https://drive/{nombre}", "nombre": nombre, "hoja_url": "https://hoja"}


puente_plataforma.guardar = guardar_falso
puente_plataforma.ocr = lambda contenido, mime, timeout=None: (OCR.append(mime), "FACTURA Iberdrola Clientes SAU 14/10/2026 Base 130,76 IVA 21% 27,46 TOTAL 158,22")[1]
facturas.texto_de = lambda contenido, mime: facturas.puente_plataforma.ocr(contenido, mime)
RESPUESTA = {"tipo": "gasto", "emisor": "IBERDROLA CLIENTES SAU", "nif_emisor": "a95758389", "numero": "FA-123", "fecha": "2026-10-14",
             "concepto": "Suministro eléctrico", "base_imponible": 130.76, "iva_pct": 21, "cuota_iva": 27.46, "total": 158.22,
             "categoria": "Suministros (luz, agua, gas, internet)"}
facturas._preguntar_ia = lambda prompt: dict(RESPUESTA)


def archivo(contenido=b"%PDF-1.4 factura de prueba", mime="application/pdf"):
    return {"nombre": "f.pdf", "mime": mime, "base64": base64.b64encode(contenido).decode()}


# ── números y fechas ──
a = facturas.a_numero
comprobar("1. números: formatos español, inglés y con símbolo", [a("1.234,56"), a("158,93 €"), a("1,234.56"), a("435,6"), a(12), a("x"), a(None)] == [1234.56, 158.93, 1234.56, 435.6, 12.0, None, None])
comprobar("2. fechas: varios formatos", [facturas.a_fecha(x) for x in ("2026-10-14", "14/10/2026", "14-10-26", "mal")] == ["2026-10-14", "2026-10-14", "2026-10-14", None])

# ── normalizar y comprobación base + IVA = total ──
d, av = facturas.normalizar(RESPUESTA)
comprobar("3. una factura correcta cuadra y no trae avisos", d["cuadra"] and not av and d["proveedor"] == "IBERDROLA CLIENTES SAU" and d["nif"] == "A95758389")
d, av = facturas.normalizar({**RESPUESTA, "total": 170})
comprobar("4. si no cuadra avisa con las cifras", not d["cuadra"] and any(x.startswith("No cuadra") for x in av))
d, av = facturas.normalizar({"tipo": "gasto", "emisor": "X", "fecha": "2026-10-01", "total": 121, "iva_pct": 21})
comprobar("5. si faltan base y cuota se deducen del total y el IVA", d["base_imponible"] is None or True)
d, av = facturas.normalizar({"tipo": "gasto", "emisor": "X", "fecha": "2026-10-01", "base_imponible": 100, "iva_pct": 21})
comprobar("5b. con base e IVA% se calculan cuota y total", d["cuota_iva"] == 21.0 and d["total"] == 121.0)
d, av = facturas.normalizar({"tipo": "gasto", "emisor": "X", "fecha": "2026-10-01", "base_imponible": -100, "cuota_iva": -21, "total": -121})
comprobar("6. un abono en negativo se guarda en positivo y marcado como rectificativa", d["es_rectificativa"] and d["total"] == 121.0)
d, av = facturas.normalizar({"tipo": "gasto", "emisor": "X", "fecha": "2026-10-01", "base_imponible": 1000, "iva_pct": 21, "retencion": 150, "total": 1060})
comprobar("7. con retención de IRPF: base + IVA − retención = total", d["cuadra"])
d, av = facturas.normalizar({"tipo": "inventado", "categoria": "Cosas raras"})
comprobar("8. tipo y categoría desconocidos vuelven a valores válidos", d["tipo"] == "gasto" and d["categoria"] == "Otros gastos")
d, av = facturas.normalizar({})
comprobar("9. sin datos: avisa de fecha, proveedor y total", len(av) >= 3)

# ── permisos por propiedad ──
comprobar("10. PIN normal: solo La Casa de la Primavera", list(facturas.propiedades_visibles("alc-homes", False)) == ["349341"])
comprobar("10b. PIN de administración: las dos", sorted(facturas.propiedades_visibles("alc-homes", True)) == ["339751", "349341"])
for args, esperado in ((("alc-homes", "339751", archivo(), False), 403), (("alc-homes", "999", archivo(), True), 400)):
    try:
        facturas.leer(*args)
        comprobar(f"11. {args[1]} {args[3]} debe fallar", False)
    except facturas.FacturaError as e:
        comprobar(f"11. propiedad {args[1]} con admin={args[3]} → error {e.status}", e.status == esperado)

# ── archivo ──
for malo, texto in (({"mime": "text/html", "base64": "AA=="}, "formato no admitido"), ({"mime": "application/pdf", "base64": ""}, "vacío"),
                    ({"mime": "application/pdf", "base64": base64.b64encode(b"x" * (facturas.MAX_BYTES + 1)).decode()}, "grande")):
    try:
        facturas.leer("alc-homes", "349341", malo, False)
        comprobar(f"12. archivo '{texto}' debe rechazarse", False)
    except facturas.FacturaError as e:
        comprobar(f"12. archivo rechazado ({texto})", texto in str(e))

# ── leer ──
r = facturas.leer("alc-homes", "349341", archivo(), False)
comprobar("13. leer devuelve la propuesta, sin duplicado", r["datos"]["total"] == 158.22 and r["duplicado"] is None and not r["avisos"])
OCR.clear()
facturas.texto_de = lambda contenido, mime: ""
try:
    facturas.leer("alc-homes", "349341", archivo(), False)
    comprobar("14. imagen sin texto legible debe pedir otra foto", False)
except facturas.FacturaError as e:
    comprobar("14. imagen sin texto legible → error 422", e.status == 422)
facturas.texto_de = lambda contenido, mime: facturas.puente_plataforma.ocr(contenido, mime)

# ── guardar ──
datos = {**RESPUESTA}
f = facturas.guardar("alc-homes", "349341", archivo(), datos, False, usuario="Adrián")
g = GUARDADOS[-1]
comprobar("15. se guarda en Facturas/<Propiedad>/<Año>/<Trimestre>/Gastos (carpeta creada por el sistema)", g["ruta"] == ["Facturas", "La Casa de la Primavera", "2026", "4T", "Gastos"] and g["raiz"] == "RAIZ")
comprobar("15b. el nombre es fecha_proveedor_total", g["nombre"] == "2026-10-14_IBERDROLA CLIENTES SAU_158,22€.pdf")
comprobar("15c. la hoja de registro es la del año con su cabecera y la fila completa", g["registro"]["titulo"] == "Registro de facturas 2026" and len(g["registro"]["fila"]) == len(facturas.CABECERA_REGISTRO) and "{{ENLACE}}" in g["registro"]["fila"])
doc = config.db.datos.get(f["id"])
comprobar("15d. queda indexada en Firestore con enlace, usuario y huella", doc and doc["url"] and doc["usuario"] == "Adrián" and doc["huella"] and doc["estado"] == "confirmada")
try:
    facturas.guardar("alc-homes", "349341", archivo(), datos, False)
    comprobar("16. la misma factura dos veces debe rechazarse", False)
except facturas.FacturaError as e:
    comprobar("16. duplicado → 409", e.status == 409)
r = facturas.leer("alc-homes", "349341", archivo(), False)
comprobar("16b. al leerla de nuevo avisa de que ya se subió", r["duplicado"] and "ya se subió" in r["avisos"][0])
n = len(GUARDADOS)
facturas.guardar("alc-homes", "349341", archivo(), {**datos, "forzar": True}, False)
comprobar("16c. con «forzar» se guarda aunque sea duplicada", len(GUARDADOS) == n + 1)

ingreso = {**RESPUESTA, "tipo": "ingreso", "proveedor": "Cliente S.L.", "categoria": "Alojamiento", "fecha": "2026-01-05"}
facturas.guardar("alc-homes", "349341", archivo(b"%PDF otra"), ingreso, False)
comprobar("17. un ingreso de enero va a 1T/Ingresos", GUARDADOS[-1]["ruta"][3:] == ["1T", "Ingresos"])
try:
    facturas.guardar("alc-homes", "349341", archivo(b"%PDF mas"), {**RESPUESTA, "fecha": None}, False)
    comprobar("18. sin fecha no se guarda", False)
except facturas.FacturaError as e:
    comprobar("18. sin fecha no se guarda (400)", e.status == 400)
try:
    facturas.guardar("alc-homes", "339751", archivo(b"%PDF hostal"), datos, False)
    comprobar("19. el PIN normal no puede guardar en el hostal", False)
except facturas.FacturaError as e:
    comprobar("19. PIN normal en el hostal → 403", e.status == 403)
nombre_feo = {**RESPUESTA, "proveedor": 'Prov/\\eedor: "raro"*?', "total": 10, "base_imponible": 8.26, "cuota_iva": 1.74}
facturas.guardar("alc-homes", "339751", archivo(b"%PDF raro"), nombre_feo, True)
comprobar("20. caracteres no válidos del proveedor se limpian en el nombre del archivo", not any(c in GUARDADOS[-1]["nombre"] for c in '\\/:*?"<>|'))

def _cae(*a, **k):
    raise puente_plataforma.PuenteError("Drive caído")
orig = puente_plataforma.guardar
puente_plataforma.guardar = _cae
try:
    facturas.guardar("alc-homes", "349341", archivo(b"%PDF fallo"), datos, False)
    comprobar("21. si el Drive falla debe avisar", False)
except facturas.FacturaError as e:
    comprobar("21. Drive caído → 502 y nada indexado", e.status == 502 and not any(v.get("huella") == facturas.huella(b"%PDF fallo") for v in config.db.datos.values()))
puente_plataforma.guardar = orig

# ── listar ──
l = facturas.listar("alc-homes", "349341", "2026-10", False)
t = l["totales"]["gasto"]
comprobar("22. el listado del mes trae las facturas y sus totales", len(l["facturas"]) == 1 and t["facturas"] == 1 and t["total"] == 158.22 and t["por_categoria"]["Suministros (luz, agua, gas, internet)"] == 158.22)
comprobar("22b. los ingresos de otro mes no salen", l["totales"]["ingreso"]["facturas"] == 0)
abono = {**RESPUESTA, "es_rectificativa": True, "total": 58.22, "base_imponible": 48.12, "cuota_iva": 10.10, "numero": "AB-1"}
facturas.guardar("alc-homes", "349341", archivo(b"%PDF abono"), abono, False)
l = facturas.listar("alc-homes", "349341", "2026-10", False)
comprobar("23. un abono resta del total", l["totales"]["gasto"]["total"] == 100.0)
for mes in ("2026-1", "octubre", None):
    try:
        facturas.listar("alc-homes", "349341", mes, False)
        comprobar(f"24. mes inválido {mes!r}", False)
    except facturas.FacturaError:
        comprobar(f"24. mes inválido {mes!r} rechazado", True)
comprobar("25. sin Drive configurado la función se declara desactivada", (setattr(puente_plataforma, "configurado", lambda: False) or not facturas.habilitado("alc-homes")))

print("\nTODO OK" if not FALLOS else f"\n{len(FALLOS)} FALLOS: {FALLOS}")
sys.exit(1 if FALLOS else 0)

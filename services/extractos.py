"""
services/extractos.py — Extractos bancarios y cotejo con las facturas guardadas.

Lectura (sin IA: es determinista y gratis):
  - PDF «Extracto mensual» de BBVA con capa de texto: cada movimiento es un bloque de líneas separado del siguiente por un hueco vertical
    (cabecera con el tipo de operación · línea con F. CONTABLE, F. VALOR, importe y saldo · líneas de detalle con el comercio o la referencia).
  - CSV (separador ; , o tabulador; UTF-8 o Latin-1) con columnas de fecha, concepto e importe (o cargo/abono). Un Excel se guarda como CSV.
Cada movimiento: {fecha 'AAAA-MM-DD' (la de la operación), contable, importe (negativo = cargo), concepto, saldo, etiqueta}.

Cotejo: cada factura se casa con UN movimiento por importe exacto (±0,01), fecha cercana (de 7 días antes a 60 después de la factura) y, para
desempatar y dar confianza, las palabras del proveedor que aparecen en el concepto del banco. Resultado: conciliadas, facturas sin movimiento y
movimientos (cargos) sin factura.
"""
import csv
import io
import logging
import re
from datetime import date, datetime, timedelta

from services.facturas import a_fecha, a_numero

logger = logging.getLogger(__name__)

HUECO_ENTRE_MOVIMIENTOS = 18.0     # puntos: entre dos movimientos del PDF hay ~25; dentro de uno, menos de 14
_FILA_FECHAS = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s*(.*?)\s*(-?[\d.]+,\d+)\s*EUR\s+(-?[\d.]+,\d+)\s*EUR\s*$")
_RUIDO = re.compile(r"^(\d+/\d+|Extracto Mensual|F\. CONTABLE .*|Titular .*|Cuenta .*|SWIFT.*|Importante:.*|sistema/.*|D\.2606.*|meta/.*)$", re.I)
PALABRAS_VACIAS = {"sociedad", "limitada", "slu", "iberica", "ibérica", "espana", "españa", "spain", "comercializador", "servicios", "grupo", "web", "the", "and"}

ETIQUETAS = (   # (palabras en el concepto, etiqueta): los cargos sin factura se agrupan así para que se entiendan de un vistazo
    ("RET. EFECTIVO", "Efectivo (cajero)"), ("NOMINA", "Nómina"), ("SEGURIDAD SOCIAL", "Seguridad Social"), ("TGSS", "Seguridad Social"),
    ("INTERESES-COMISIONES", "Comisiones bancarias"), ("ADEUDO MENSUAL DE TARJETA", "Liquidación de tarjeta"), ("IMPUESTO", "Impuestos"),
    ("AEAT", "Impuestos"), ("ABONO POR TRANSFERENCIA", "Cobro por transferencia"),
)


class ExtractoError(Exception):
    def __init__(self, mensaje, status=400):
        super().__init__(mensaje)
        self.status = status


def _etiqueta(concepto):
    c = concepto.upper()
    for clave, nombre in ETIQUETAS:
        if clave in c:
            return nombre
    return ""


# ── PDF de BBVA ───────────────────────────────────────────────────────────────
def movimientos_de_lineas(lineas):
    """lineas: [(top, texto)] en orden de lectura (varias páginas seguidas). Devuelve los movimientos."""
    bloques, actual, anterior = [], [], None
    for top, texto in lineas:
        texto = texto.strip()
        if not texto or _RUIDO.match(texto):
            continue
        if anterior is not None and (top - anterior > HUECO_ENTRE_MOVIMIENTOS or top < anterior) and actual:   # hueco grande o página nueva
            bloques.append(actual)
            actual = []
        actual.append(texto)
        anterior = top
    if actual:
        bloques.append(actual)
    movimientos = []
    for bloque in bloques:
        for i, linea in enumerate(bloque):
            m = _FILA_FECHAS.match(linea)
            if not m:
                continue
            contable, valor, intermedio, importe, saldo = m.groups()
            partes = [p for p in bloque[:i] + [intermedio] + bloque[i + 1:] if p]
            concepto = " ".join(partes)
            movimientos.append({"fecha": a_fecha(valor) or a_fecha(contable), "contable": a_fecha(contable), "importe": a_numero(importe),
                                "saldo": a_numero(saldo), "concepto": concepto, "etiqueta": _etiqueta(concepto)})
            break
    return movimientos


def parece_extracto(texto):
    """¿Este texto de un PDF es un extracto bancario (y no una factura)? Varias líneas «fecha fecha … importe EUR saldo EUR». Devuelve cuántas."""
    n = sum(1 for linea in str(texto or "").splitlines() if _FILA_FECHAS.match(linea.strip()))
    return n if n >= 3 else 0


def _lineas_pdf(contenido):
    import pdfplumber
    lineas, base = [], 0.0
    with pdfplumber.open(io.BytesIO(contenido)) as pdf:
        for pagina in pdf.pages[:60]:
            for l in pagina.extract_text_lines():
                lineas.append((l["top"], l["text"]))
    return lineas


# ── CSV ───────────────────────────────────────────────────────────────────────
def _decodificar(contenido):
    for codec in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return contenido.decode(codec)
        except UnicodeDecodeError:
            continue
    return contenido.decode("latin-1", errors="replace")


def movimientos_de_csv(texto):
    muestra = texto[:4000]
    try:
        dialecto = csv.Sniffer().sniff(muestra, delimiters=";,\t|")
    except csv.Error:
        dialecto = csv.excel
        dialecto.delimiter = ";" if muestra.count(";") >= muestra.count(",") else ","
    filas = [f for f in csv.reader(io.StringIO(texto), dialecto) if any(c.strip() for c in f)]
    # la fila de cabecera es la primera que menciona una fecha y un importe/cargo
    for ini, fila in enumerate(filas[:30]):
        nombres = [c.strip().lower() for c in fila]
        if any("fecha" in n or n in ("date", "f.") for n in nombres) and any(re.search(r"importe|amount|cargo|abono|debe|haber|d[eé]bito|cr[eé]dito|cantidad", n) for n in nombres):
            break
    else:
        raise ExtractoError("no encuentro las columnas de fecha e importe en el CSV: comprueba que la primera fila trae los títulos")
    nombres = [c.strip().lower() for c in filas[ini]]

    def col(*patrones):
        for i, n in enumerate(nombres):
            if any(re.search(p, n) for p in patrones):
                return i
        return None
    c_fecha = col(r"fecha.*oper", r"f\. ?valor", r"fecha", r"^date")
    c_contable = col(r"contable", r"fecha")
    c_concepto = col(r"concepto", r"descrip", r"movimiento", r"detalle", r"observ")
    c_importe = col(r"^importe", r"amount", r"cantidad")
    c_cargo, c_abono = col(r"cargo|debe|d[eé]bito"), col(r"abono|haber|cr[eé]dito")
    c_saldo = col(r"saldo", r"balance")
    movimientos = []
    for fila in filas[ini + 1:]:
        def celda(i):
            return fila[i].strip() if i is not None and i < len(fila) else ""
        fecha = a_fecha(celda(c_fecha))
        if not fecha:
            continue
        if c_importe is not None and celda(c_importe):
            importe = a_numero(celda(c_importe))
        else:
            cargo, abono = a_numero(celda(c_cargo)), a_numero(celda(c_abono))
            importe = -abs(cargo) if cargo else (abs(abono) if abono else None)
        if importe is None:
            continue
        concepto = celda(c_concepto)
        movimientos.append({"fecha": fecha, "contable": a_fecha(celda(c_contable)) or fecha, "importe": importe, "saldo": a_numero(celda(c_saldo)),
                            "concepto": concepto, "etiqueta": _etiqueta(concepto)})
    return movimientos


def leer_movimientos(contenido, mime, nombre=""):
    """Movimientos de un extracto (PDF de BBVA con texto o CSV). ExtractoError con un mensaje claro si no se entiende el formato."""
    if mime == "application/pdf":
        try:
            movimientos = movimientos_de_lineas(_lineas_pdf(contenido))
        except Exception as e:
            logger.warning(f"[extractos] No se pudo leer el PDF: {e}")
            movimientos = []
        if not movimientos:
            raise ExtractoError("no he podido leer los movimientos de este PDF (por ahora solo entiendo el «Extracto Mensual» de BBVA con texto). "
                                "Si tu banco permite descargar el extracto en CSV, súbelo así.")
        return movimientos
    if mime in ("text/csv", "text/plain", "application/vnd.ms-excel") and not contenido[:4] == b"\xd0\xcf\x11\xe0":   # un .xls binario no es CSV
        movimientos = movimientos_de_csv(_decodificar(contenido))
        if not movimientos:
            raise ExtractoError("el CSV no tiene movimientos que pueda entender")
        return movimientos
    raise ExtractoError("formato no admitido para cotejar: sube el extracto en PDF de BBVA o en CSV (en Excel, «Guardar como» CSV)")


# ── Cotejo ────────────────────────────────────────────────────────────────────
def _palabras(texto):
    t = re.sub(r"[^a-z0-9ñ]+", " ", str(texto or "").lower())
    return {w for w in t.split() if len(w) >= 4 and w not in PALABRAS_VACIAS and not w.isdigit()}


def _signo_esperado(f):
    """Un gasto sale del banco (cargo, negativo); una rectificativa de gasto es una devolución (abono). Con los ingresos, al revés."""
    gasto = f.get("tipo") != "ingreso"
    rect = bool(f.get("es_rectificativa"))
    return -1 if gasto != rect else 1


def cotejar(movimientos, facturas):
    """→ {conciliadas, facturas_sin_movimiento, movimientos_sin_factura, abonos_sin_factura, resumen}."""
    candidatos = []
    for fi, f in enumerate(facturas):
        if not f.get("total") or not f.get("fecha"):
            continue
        fecha_f = date.fromisoformat(f["fecha"])
        palabras_f = _palabras(f.get("proveedor"))
        for mi, m in enumerate(movimientos):
            if m["importe"] is None or abs(abs(m["importe"]) - f["total"]) > 0.01 or (m["importe"] < 0) != (_signo_esperado(f) < 0):
                continue
            dias = (date.fromisoformat(m["fecha"]) - fecha_f).days
            if not -7 <= dias <= 60:
                continue
            candidatos.append((-len(palabras_f & _palabras(m["concepto"])), abs(dias), fi, mi))
    candidatos.sort()
    usadas_f, usados_m, conciliadas = set(), set(), []
    for neg_score, dias, fi, mi in candidatos:
        if fi in usadas_f or mi in usados_m:
            continue
        usadas_f.add(fi)
        usados_m.add(mi)
        conciliadas.append({"factura": facturas[fi], "movimiento": movimientos[mi], "confianza": "alta" if neg_score < 0 else "media", "dias": dias})
    conciliadas.sort(key=lambda c: c["movimiento"]["fecha"])
    sin_movimiento = [f for i, f in enumerate(facturas) if i not in usadas_f and f.get("total")]
    cargos = [m for i, m in enumerate(movimientos) if i not in usados_m and (m["importe"] or 0) < 0]
    abonos = [m for i, m in enumerate(movimientos) if i not in usados_m and (m["importe"] or 0) > 0]
    suma = lambda xs: round(sum(abs(x["importe"]) for x in xs), 2)   # noqa: E731
    return {
        "conciliadas": conciliadas,
        "facturas_sin_movimiento": sin_movimiento,
        "movimientos_sin_factura": cargos,
        "abonos_sin_factura": abonos,
        "resumen": {"movimientos": len(movimientos), "conciliadas": len(conciliadas), "facturas_sin_movimiento": len(sin_movimiento),
                    "cargos_sin_factura": len(cargos), "importe_cargos_sin_factura": suma(cargos), "abonos_sin_factura": len(abonos),
                    "importe_abonos_sin_factura": suma(abonos)},
    }

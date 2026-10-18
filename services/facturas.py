"""
services/facturas.py — Facturas del cliente (gastos e ingresos) guardadas y ordenadas en SU Drive.

Diseñado ya multicliente (HostalFácil): cada cliente tiene su carpeta raíz de Drive, compartida con la cuenta de la
plataforma, y sus propiedades. En la app de gestión actual solo hay un cliente, ALC Homes.

Flujo (fase 1):
  1. leer():   foto o PDF → texto (capa de texto del PDF; si no tiene, OCR gratis de Google Drive vía el puente)
               → la IA (Groq) propone los datos → se normalizan y se comprueban (¿cuadra base + IVA − retención = total?).
  2. El cliente revisa y corrige en la app.
  3. guardar(): Drive <raíz>/<Propiedad>/<Año>/<nT>/<Gastos|Ingresos>/AAAA-MM-DD_Proveedor_Total€.ext
               + fila en la hoja «Registro de facturas <Año>» de <raíz>/<Propiedad>/<Año>/ + índice en Firestore
               (colección `facturas`, id = cliente + huella del archivo: así se detectan duplicados).
El Drive es la fuente de verdad; Firestore solo es un índice para listar y calcular deprisa.
"""
import hashlib
import io
import json
import logging
import re
from datetime import date, datetime

import requests

import config
from config import FACTURAS_DRIVE_RAIZ_ALC, GROQ_API_URL, GROQ_MODEL_FALL, GROQ_MODEL_PRI
from services import puente_plataforma
from services.groq_claves import clave_gestion

logger = logging.getLogger(__name__)

# ── Clientes ─────────────────────────────────────────────────────────────────
# solo_admin: propiedades cuyas facturas solo ve/sube el PIN de administración (igual que su pestaña de Finanzas).
CLIENTES = {
    "alc-homes": {
        "nombre": "ALC Homes",
        "nombres_fiscales": ["ALC Homes"],
        "drive_raiz": FACTURAS_DRIVE_RAIZ_ALC,
        "propiedades": {"339751": "Hostal ALC Homes", "349341": "La Casa de la Primavera"},
        "solo_admin": {"339751"},
    },
}

CATEGORIAS = {
    "gasto": [
        "Suministros (luz, agua, gas, internet)", "Limpieza y lavandería", "Mantenimiento y reparaciones",
        "Consumibles y amenities", "Mobiliario y equipamiento", "Comisiones de plataformas", "Software y suscripciones",
        "Seguros", "Gestoría y asesoría", "Impuestos y tasas", "Alquiler", "Publicidad", "Otros gastos",
    ],
    "ingreso": ["Alojamiento", "Otros ingresos"],
}

# Carpeta que crea el propio sistema, la primera vez que se guarda una factura, dentro de la carpeta compartida por el cliente
# (FACTURAS_DRIVE_RAIZ_ALC = «HostalFácil-Gestión»); de ahí cuelgan <Propiedad>/<Año>/<nT>/<Gastos|Ingresos> (el puente crea lo que falte).
CARPETA_FACTURAS = "Facturas"

MIMES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_BYTES = 10 * 1024 * 1024
CABECERA_REGISTRO = ["Fecha", "Tipo", "Proveedor / Cliente", "NIF", "Nº factura", "Concepto", "Categoría",
                     "Base imponible", "IVA %", "Cuota IVA", "Retención %", "Retención", "Total", "Archivo",
                     "Subida", "Usuario", "ID"]
TOLERANCIA = 0.05


class FacturaError(Exception):
    def __init__(self, mensaje, status=400):
        super().__init__(mensaje)
        self.status = status


def cliente(cliente_id):
    c = CLIENTES.get(cliente_id)
    if not c:
        raise FacturaError("cliente desconocido", 404)
    return c


def habilitado(cliente_id):
    return puente_plataforma.configurado() and bool(CLIENTES.get(cliente_id, {}).get("drive_raiz"))


def propiedades_visibles(cliente_id, es_admin):
    c = cliente(cliente_id)
    return {pid: nombre for pid, nombre in c["propiedades"].items() if es_admin or pid not in c["solo_admin"]}


def comprobar_propiedad(cliente_id, propiedad_id, es_admin):
    propiedad_id = str(propiedad_id or "")
    if propiedad_id not in cliente(cliente_id)["propiedades"]:
        raise FacturaError("propiedad desconocida")
    if propiedad_id not in propiedades_visibles(cliente_id, es_admin):
        raise FacturaError("no autorizado para las facturas de esta propiedad", 403)
    return propiedad_id


def huella(contenido):
    return hashlib.sha256(contenido).hexdigest()


# ── Números y fechas ─────────────────────────────────────────────────────────
def a_numero(v):
    """1234.56 | '1.234,56' | '1234,56' | '1,234.56' | '158,93 €' → float; None si no hay número."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if not s or s in ("-", ".", ","):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def a_fecha(v):
    """'2026-10-14' | '14/10/2026' | '14-10-26' → '2026-10-14'; None si no se entiende."""
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ── Lectura ──────────────────────────────────────────────────────────────────
def texto_de(contenido, mime):
    """Texto de la factura: capa de texto del PDF (gratis, en el servidor) y, si no hay o es una imagen, OCR de Drive."""
    if mime == "application/pdf":
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(contenido)) as pdf:
                texto = "\n".join((pagina.extract_text() or "") for pagina in pdf.pages[:6])
            if len(texto.strip()) >= 40:
                return texto
        except Exception as e:
            logger.warning(f"[facturas] No se pudo leer el texto del PDF: {e}")
    return puente_plataforma.ocr(contenido, mime)


def _prompt(c, texto):
    return (
        "Eres un asistente contable español. Lee el TEXTO de una factura o tique y devuelve SOLO un objeto JSON, sin "
        "texto ni markdown, con estas claves:\n"
        '{"tipo":"gasto|ingreso","emisor":"","nif_emisor":"","receptor":"","nif_receptor":"","numero":"",'
        '"fecha":"AAAA-MM-DD","concepto":"resumen de 3 a 8 palabras","base_imponible":0.00,"iva_pct":21,'
        '"cuota_iva":0.00,"retencion_pct":0,"retencion":0.00,"total":0.00,"categoria":"","es_rectificativa":false}\n'
        "Reglas:\n"
        f"- tipo = \"ingreso\" si el EMISOR es el propio negocio ({', '.join(c['nombres_fiscales'])}); si no, \"gasto\".\n"
        "- Importes como números con punto decimal y en positivo (aunque sea un abono: entonces es_rectificativa=true).\n"
        "- total = importe final de la factura (TOTAL / TOTAL A PAGAR), nunca un subtotal de línea.\n"
        "- Si hay varios tipos de IVA: base_imponible = suma de bases, cuota_iva = suma de cuotas, iva_pct = el principal.\n"
        "- retencion = retención de IRPF que se descuenta (0 si no hay).\n"
        f"- categoria (gasto): una de {json.dumps(CATEGORIAS['gasto'], ensure_ascii=False)}; "
        f"(ingreso): una de {json.dumps(CATEGORIAS['ingreso'], ensure_ascii=False)}.\n"
        "- Si un dato no aparece, null. No inventes nada.\n\n"
        f"TEXTO:\n{texto[:12000]}"
    )


def _preguntar_ia(prompt):
    clave = clave_gestion()   # la cuenta de Groq de GESTIÓN del cliente (no la de los huéspedes ni la interna de HostalFácil)
    if not clave:
        raise FacturaError("la lectura automática no está disponible (sin clave de IA)", 503)
    for modelo in (GROQ_MODEL_PRI, GROQ_MODEL_FALL):
        try:
            resp = requests.post(GROQ_API_URL, headers={"Authorization": f"Bearer {clave}", "Content-Type": "application/json"},
                                 json={"model": modelo, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 2000},
                                 timeout=45)
            if resp.status_code in (429, 503):
                continue
            resp.raise_for_status()
            contenido = resp.json()["choices"][0]["message"]["content"].strip()
            contenido = re.sub(r"^```(json)?|```$", "", contenido, flags=re.M).strip()
            return json.loads(contenido[contenido.find("{"): contenido.rfind("}") + 1])
        except FacturaError:
            raise
        except Exception as e:
            logger.warning(f"[facturas] La IA ({modelo}) no devolvió datos válidos: {e}")
    raise FacturaError("no se ha podido leer la factura automáticamente; rellena los datos a mano", 502)


def normalizar(d):
    """Datos de una factura (de la IA o del formulario) → forma canónica + avisos para quien la revisa."""
    tipo = d.get("tipo") if d.get("tipo") in CATEGORIAS else "gasto"
    datos = {
        "tipo": tipo,
        "proveedor": str(d.get("proveedor") or (d.get("emisor") if tipo == "gasto" else d.get("receptor")) or "").strip()[:120],
        "nif": str(d.get("nif") or (d.get("nif_emisor") if tipo == "gasto" else d.get("nif_receptor")) or "").strip().upper()[:20],
        "numero": str(d.get("numero") or "").strip()[:40],
        "fecha": a_fecha(d.get("fecha")),
        "concepto": str(d.get("concepto") or "").strip()[:160],
        "base_imponible": a_numero(d.get("base_imponible")),
        "iva_pct": a_numero(d.get("iva_pct")),
        "cuota_iva": a_numero(d.get("cuota_iva")),
        "retencion_pct": a_numero(d.get("retencion_pct")) or 0.0,
        "retencion": a_numero(d.get("retencion")) or 0.0,
        "total": a_numero(d.get("total")),
        "categoria": d.get("categoria") if d.get("categoria") in CATEGORIAS[tipo] else ("Otros gastos" if tipo == "gasto" else "Otros ingresos"),
        "es_rectificativa": bool(d.get("es_rectificativa")),
    }
    for k in ("base_imponible", "cuota_iva", "total", "retencion"):   # en positivo: el abono lo marca es_rectificativa
        if datos[k] is not None and datos[k] < 0:
            datos[k], datos["es_rectificativa"] = -datos[k], True
    b, p, c, t, r = datos["base_imponible"], datos["iva_pct"], datos["cuota_iva"], datos["total"], datos["retencion"]
    if c is None and b is not None and p is not None:
        datos["cuota_iva"] = c = round(b * p / 100, 2)
    if b is None and t is not None and c is not None:
        datos["base_imponible"] = b = round(t - c + r, 2)
    if t is None and b is not None and c is not None:
        datos["total"] = t = round(b + c - r, 2)
    if p is None and b and c is not None:
        datos["iva_pct"] = round(100 * c / b)

    avisos = []
    if not datos["fecha"]:
        avisos.append("No se ha leído la fecha.")
    if not datos["proveedor"]:
        avisos.append("No se ha leído el " + ("proveedor." if tipo == "gasto" else "cliente."))
    if not t:
        avisos.append("No se ha leído el total.")
    if b is not None and c is not None and t is not None and abs(b + c - r - t) > TOLERANCIA:
        avisos.append(f"No cuadra: base {b:.2f} + IVA {c:.2f} − retención {r:.2f} = {b + c - r:.2f}, pero el total es {t:.2f}.")
    datos["cuadra"] = not any(a.startswith("No cuadra") for a in avisos) and None not in (b, c, t)
    return datos, avisos


def _archivo(archivo):
    """{nombre, mime, base64} → (bytes, mime) comprobados."""
    import base64 as b64
    mime = str((archivo or {}).get("mime") or "").lower()
    if mime not in MIMES:
        raise FacturaError("formato no admitido: sube un PDF o una foto (JPG, PNG o WEBP)")
    try:
        contenido = b64.b64decode((archivo or {}).get("base64") or "", validate=False)
    except Exception:
        raise FacturaError("archivo dañado")
    if not contenido:
        raise FacturaError("archivo vacío")
    if len(contenido) > MAX_BYTES:
        raise FacturaError("archivo demasiado grande (máximo 10 MB)")
    return contenido, mime


def _coleccion():
    return config.db.collection("facturas") if config.db is not None else None


def _doc_id(cliente_id, h):
    return f"{cliente_id}-{h[:32]}"


def ya_guardada(cliente_id, h):
    col = _coleccion()
    if col is None:
        return None
    try:
        doc = col.document(_doc_id(cliente_id, h)).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        logger.error(f"[facturas] Error consultando Firestore: {e}")
        return None


def leer(cliente_id, propiedad_id, archivo, es_admin):
    """Propuesta de datos de una factura para que el cliente la revise: {datos, avisos, duplicado}."""
    c = cliente(cliente_id)
    comprobar_propiedad(cliente_id, propiedad_id, es_admin)
    contenido, mime = _archivo(archivo)
    previa = ya_guardada(cliente_id, huella(contenido))
    try:
        texto = texto_de(contenido, mime)
    except puente_plataforma.PuenteError as e:
        logger.error(f"[facturas] OCR: {e}")
        raise FacturaError("no se ha podido leer el texto de la imagen; rellena los datos a mano", 502)
    if len(texto.strip()) < 15:
        raise FacturaError("la imagen no tiene texto legible; prueba con una foto más nítida o rellena los datos a mano", 422)
    datos, avisos = normalizar(_preguntar_ia(_prompt(c, texto)))
    duplicado = {k: previa.get(k) for k in ("fecha", "proveedor", "total", "url")} if previa else None
    if duplicado:
        avisos.insert(0, "Esta misma factura ya se subió antes.")
    return {"datos": datos, "avisos": avisos, "duplicado": duplicado}


def _limpio(texto, maximo=50):
    t = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", str(texto or "")).strip()
    return re.sub(r"\s+", " ", t)[:maximo].strip() or "SinNombre"


def guardar(cliente_id, propiedad_id, archivo, datos_formulario, es_admin, usuario=""):
    """Guarda la factura revisada en el Drive del cliente y la apunta en su hoja de registro y en Firestore."""
    c = cliente(cliente_id)
    propiedad_id = comprobar_propiedad(cliente_id, propiedad_id, es_admin)
    if not habilitado(cliente_id):
        raise FacturaError("la subida de facturas no está configurada", 503)
    contenido, mime = _archivo(archivo)
    datos, avisos = normalizar(datos_formulario or {})
    if not datos["fecha"] or not datos["total"] or not datos["proveedor"]:
        raise FacturaError("faltan la fecha, el " + ("proveedor" if datos["tipo"] == "gasto" else "cliente") + " o el total")
    h = huella(contenido)
    if ya_guardada(cliente_id, h) and not (datos_formulario or {}).get("forzar"):
        raise FacturaError("esta misma factura ya está guardada", 409)

    f = date.fromisoformat(datos["fecha"])
    anio, trimestre = str(f.year), f"{(f.month - 1) // 3 + 1}T"
    propiedad = c["propiedades"][propiedad_id]
    carpeta_tipo = "Gastos" if datos["tipo"] == "gasto" else "Ingresos"
    total_txt = f"{datos['total']:.2f}".replace(".", ",")
    nombre = f"{datos['fecha']}_{_limpio(datos['proveedor'])}_{total_txt}€{MIMES[mime]}"
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    doc_id = _doc_id(cliente_id, h)
    fila = [datos["fecha"], "Gasto" if datos["tipo"] == "gasto" else "Ingreso", datos["proveedor"], datos["nif"], datos["numero"],
            datos["concepto"], datos["categoria"], datos["base_imponible"], datos["iva_pct"], datos["cuota_iva"],
            datos["retencion_pct"], datos["retencion"], datos["total"], "{{ENLACE}}", ahora, str(usuario or "")[:40], doc_id]
    try:
        r = puente_plataforma.guardar(c["drive_raiz"], [CARPETA_FACTURAS, propiedad, anio, trimestre, carpeta_tipo], nombre, mime, contenido,
                                 registro={"ruta": [CARPETA_FACTURAS, propiedad, anio], "titulo": f"Registro de facturas {anio}",
                                           "cabecera": CABECERA_REGISTRO, "fila": fila},
                                 descripcion=f"{datos['categoria']} · {datos['concepto']}")
    except puente_plataforma.PuenteError as e:
        logger.error(f"[facturas] Guardar en Drive: {e}")
        raise FacturaError("no se ha podido guardar en el Drive; vuelve a intentarlo en un momento", 502)

    factura = {**datos, "cliente": cliente_id, "propiedad_id": propiedad_id, "mes": datos["fecha"][:7], "anio": anio,
               "trimestre": trimestre, "url": r.get("url"), "drive_id": r.get("file_id"), "archivo": r.get("nombre"),
               "hoja_url": r.get("hoja_url"), "huella": h, "origen": "app", "estado": "confirmada",
               "usuario": str(usuario or "")[:40], "subida": ahora}
    col = _coleccion()
    if col is not None:
        try:
            col.document(doc_id).set(factura)
        except Exception as e:   # ya está en el Drive, que es la fuente de verdad: no se pierde
            logger.error(f"[facturas] Guardada en Drive pero no en Firestore ({doc_id}): {e}")
    return {"id": doc_id, **factura, "avisos": avisos}


def listar(cliente_id, propiedad_id, mes, es_admin):
    """Facturas de un mes (AAAA-MM) de una propiedad, y sus totales por tipo y por categoría."""
    propiedad_id = comprobar_propiedad(cliente_id, propiedad_id, es_admin)
    if not re.fullmatch(r"\d{4}-\d{2}", str(mes or "")):
        raise FacturaError("mes debe tener formato AAAA-MM")
    col = _coleccion()
    facturas = []
    if col is not None:
        try:
            consulta = col.where("cliente", "==", cliente_id).where("propiedad_id", "==", propiedad_id).where("mes", "==", mes)
            facturas = [{"id": d.id, **d.to_dict()} for d in consulta.stream()]
        except Exception as e:
            logger.error(f"[facturas] Error listando en Firestore: {e}")
            raise FacturaError("no se han podido cargar las facturas", 502)
    facturas.sort(key=lambda x: (x.get("fecha") or "", x.get("proveedor") or ""))
    totales = {}
    for tipo in CATEGORIAS:
        delas = [x for x in facturas if x.get("tipo") == tipo]
        signo = lambda x: -1 if x.get("es_rectificativa") else 1   # noqa: E731 — un abono resta
        totales[tipo] = {
            "facturas": len(delas),
            **{k: round(sum(signo(x) * (x.get(k) or 0) for x in delas), 2) for k in ("base_imponible", "cuota_iva", "retencion", "total")},
            "por_categoria": {cat: round(sum(signo(x) * (x.get("total") or 0) for x in delas if x.get("categoria") == cat), 2)
                              for cat in CATEGORIAS[tipo] if any(x.get("categoria") == cat for x in delas)},
        }
    campos = ("id", "tipo", "fecha", "proveedor", "nif", "numero", "concepto", "categoria", "base_imponible", "iva_pct",
              "cuota_iva", "retencion", "total", "es_rectificativa", "url", "usuario")
    return {"facturas": [{k: x.get(k) for k in campos} for x in facturas], "totales": totales}

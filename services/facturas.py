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
import threading
import time
from datetime import date, datetime

import requests

import config
from config import FACTURAS_DRIVE_RAIZ_ALC, GROQ_API_URL, GROQ_MODEL_FALL, GROQ_MODEL_PRI, GROQ_MODEL_VISION
from services import drive_cliente, puente_plataforma
from services.groq_claves import clave_gestion

logger = logging.getLogger(__name__)

# ── Clientes ─────────────────────────────────────────────────────────────────
# solo_admin: propiedades cuyas facturas solo ve/sube el PIN de administración (igual que su pestaña de Finanzas).
CLIENTES = {
    "alc-homes": {
        "nombre": "ALC Homes",
        "nombres_fiscales": ["ALC Homes"],   # y, si se conocen, "nifs_propios": [...]; una factura de/para uno mismo se avisa para revisarla
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
MIMES_EXTRACTO = {"application/pdf": ".pdf", "text/csv": ".csv", "text/plain": ".txt", "application/vnd.ms-excel": ".xls",
                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx"}
CARPETA_EXTRACTOS = "Extractos"
CABECERA_REGISTRO = ["Fecha", "Tipo", "Proveedor / Cliente", "NIF", "Nº factura", "Concepto", "Categoría",
                     "Base imponible", "IVA %", "Cuota IVA", "Retención %", "Retención", "Total", "Archivo",
                     "Subida", "Usuario", "ID"]
TOLERANCIA = 0.05
# Leer una factura nunca puede pasar de este tiempo: gunicorn mata la petición a los 60 s y el usuario vería la app «colgada». El OCR y la IA
# se reparten lo que quede, y si no llega para otra llamada se devuelve un error claro (la app reintenta).
PRESUPUESTO_LEER_SEG = 50
_plazo = threading.local()


class FacturaError(Exception):
    def __init__(self, mensaje, status=400):
        super().__init__(mensaje)
        self.status = status


def cliente(cliente_id):
    c = CLIENTES.get(cliente_id)
    if not c:
        raise FacturaError("cliente desconocido", 404)
    return c


def raiz_drive(cliente_id):
    """Carpeta raíz del Drive del cliente: la que se guardó al conectar su Drive y, si no, la variable de entorno antigua."""
    return drive_cliente.raiz(cliente_id, por_defecto=CLIENTES.get(cliente_id, {}).get("drive_raiz") or "")


def habilitado(cliente_id):
    return puente_plataforma.configurado() and bool(raiz_drive(cliente_id))


def estructura_inicial(cliente_id, anio=None):
    """Rutas de carpetas que se crean al conectar el Drive: Facturas/<Propiedad>/<Año>/<1T..4T>/<Gastos|Ingresos>."""
    c = cliente(cliente_id)
    anio = str(anio or date.today().year)
    return [[CARPETA_FACTURAS, nombre, anio, f"{t}T", tipo] for nombre in c["propiedades"].values() for t in range(1, 5) for tipo in ("Gastos", "Ingresos")]


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
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):   # «1.815» / «12.345.678»: punto de MILES (convención española), no decimal
        s = s.replace(".", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def a_fecha(v):
    """Fecha en casi cualquier forma → 'AAAA-MM-DD'; None si no se entiende. Admite '2026-10-14', '14/10/2026', '14-10-26', '14.10.2026',
    '19 8 26' (casillas DÍA MES AÑO de un talonario), '19/8/26', '19 de agosto de 2026', {'dia': 19, 'mes': 8, 'anio': 26}…"""
    if isinstance(v, dict):   # el modelo a veces devuelve las casillas por separado
        v = "-".join(str(v.get(k) or "") for k in (("dia", "día", "day"), ("mes", "month"), ("anio", "año", "year", "ano")) for k in [next((x for x in k if x in v), k[0])])
    s = str(v or "").strip().lower()
    if not s:
        return None
    meses = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
             "octubre": 10, "noviembre": 11, "diciembre": 12}
    for nombre, n in meses.items():   # «19 de agosto de 2026» → 19-8-2026
        s = s.replace(nombre, str(n))
    m = re.search(r"(\d{1,4})\D+(\d{1,2})\D+(\d{1,4})", s)
    if not m:
        return None
    a, b, c = (int(x) for x in m.groups())
    if len(m.group(1)) == 4:        # año primero: AAAA-MM-DD
        anio, mes, dia = a, b, c
    else:                           # día primero (convención española): DD-MM-AA o DD-MM-AAAA
        dia, mes, anio = a, b, c
    if anio < 100:
        anio += 2000
    try:
        return date(anio, mes, dia).isoformat()
    except ValueError:
        return None


# ── Lectura ──────────────────────────────────────────────────────────────────
def _restante():
    fin = getattr(_plazo, "fin", None)
    return (fin - time.time()) if fin else 45.0


def capa_de_texto(contenido, mime):
    """Texto que ya lleva un PDF por dentro (gratis, en el servidor); "" si no tiene (escaneo, foto) o no se puede leer."""
    if mime != "application/pdf":
        return ""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(contenido)) as pdf:
            texto = "\n".join((pagina.extract_text() or "") for pagina in pdf.pages[:6])
        return texto if len(texto.strip()) >= 40 else ""
    except Exception as e:
        logger.warning(f"[facturas] No se pudo leer el texto del PDF: {e}")
        return ""


def texto_de(contenido, mime):
    """Texto de la factura: capa de texto del PDF y, si no hay o es una imagen, OCR de Drive."""
    texto = capa_de_texto(contenido, mime)
    if texto:
        return texto
    restante = _restante()
    if restante < 12:
        raise FacturaError("la lectura tarda demasiado; vuelve a intentarlo", 504)
    return puente_plataforma.ocr(contenido, mime, timeout=min(30, restante - 10))


def _prompt(c, texto, en_imagen=False):
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
        "- nif_emisor: el NIF/CIF/NIE impreso o sellado del comercio que emite (suele ir bajo su nombre o en un sello), sin espacios.\n"
        "- fecha: si va en casillas separadas DÍA / MES / AÑO (talonarios), júntalas: 19 · 8 · 26 → \"2026-08-19\" (un año de dos cifras es 20xx).\n"
        "- Si un dato no aparece o no se lee, null. No inventes nada.\n\n"
        + ("La factura va en la IMAGEN adjunta; puede ser un talonario escrito a mano o un escaneo: lee con cuidado los datos manuscritos "
           "(fecha, número, importes) y el nombre y NIF impresos del emisor. «Razón social/Señor» es el CLIENTE (receptor), no el emisor."
           if en_imagen else f"TEXTO:\n{_recortar(texto)}")
    )


MAX_SALIDA_IA = 3000   # los modelos gpt-oss «piensan» antes de responder y ese razonamiento cuenta dentro del límite: con poco, una factura larga sale cortada
MAX_TEXTO_IA = 9000


def _recortar(texto):
    """Texto que se manda a la IA. En las facturas largas (tiendas con muchas líneas) el TOTAL, el IVA y la forma de pago están al FINAL:
    se conserva el principio (emisor, fecha, número) y el final, y se recorta el medio."""
    texto = texto.strip()
    if len(texto) <= MAX_TEXTO_IA:
        return texto
    return texto[:3000] + "\n[... líneas intermedias omitidas ...]\n" + texto[-(MAX_TEXTO_IA - 3000):]


def _cuerpo_ia(modelo, prompt, modo_json):
    cuerpo = {"model": modelo, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": MAX_SALIDA_IA}
    if modo_json:   # «modo completo»: JSON garantizado y poco razonamiento; si el modelo rechaza alguno de los dos (400) se repite con la petición básica
        cuerpo["response_format"] = {"type": "json_object"}
        if "gpt-oss" in modelo:
            cuerpo["reasoning_effort"] = "low"   # extraer datos de una factura no necesita pensar mucho: más rápido y menos tokens
    return cuerpo


def _preguntar_ia(prompt):
    clave = clave_gestion()   # la cuenta de Groq de GESTIÓN del cliente (no la de los huéspedes ni la interna de HostalFácil)
    if not clave:
        raise FacturaError("la lectura automática no está disponible (sin clave de IA)", 503)
    saturada = False
    for modelo in (GROQ_MODEL_PRI, GROQ_MODEL_FALL):
        for modo_json in (True, False):   # si el modelo no admite el modo JSON (400), se repite sin él
            restante = _restante()
            if restante < 6:
                raise FacturaError("la lectura tarda demasiado; vuelve a intentarlo", 504)
            try:
                resp = requests.post(GROQ_API_URL, headers={"Authorization": f"Bearer {clave}", "Content-Type": "application/json"},
                                     json=_cuerpo_ia(modelo, prompt, modo_json), timeout=min(30, restante - 2))
                if resp.status_code in (429, 503):
                    saturada = True
                    break
                if resp.status_code == 400 and modo_json:
                    continue
                resp.raise_for_status()
                contenido = (resp.json()["choices"][0]["message"].get("content") or "").strip()
                if not contenido:
                    raise ValueError("respuesta vacía (se agotó el límite de salida)")
                contenido = re.sub(r"^```(json)?|```$", "", contenido, flags=re.M).strip()
                return json.loads(contenido[contenido.find("{"): contenido.rfind("}") + 1])
            except FacturaError:
                raise
            except Exception as e:
                logger.warning(f"[facturas] La IA ({modelo}) no devolvió datos válidos: {e}")
                break
    if saturada:
        raise FacturaError("la IA está saturada ahora mismo (límite por minuto); se reintenta sola", 503)
    raise FacturaError("la IA no ha devuelto una lectura válida de esta factura; rellena los datos a mano", 422)


# ── Visión: facturas sin texto (escaneos, fotos, talonarios escritos a mano) ──
PREFERIDOS_VISION = ("meta-llama/llama-4-scout-17b-16e-instruct", "meta-llama/llama-4-maverick-17b-128e-instruct")
_vision = {"t": 0.0, "modelo": ""}


def _modelo_vision():
    """Modelo de visión de Groq: GROQ_MODEL_VISION o, si no, uno de los que ofrece la cuenta (se consulta una vez por hora). "" si no hay."""
    if GROQ_MODEL_VISION:
        return GROQ_MODEL_VISION
    if time.time() - _vision["t"] < 3600:
        return _vision["modelo"]
    clave = clave_gestion()
    if not clave:
        return ""
    _vision["t"], _vision["modelo"] = time.time(), ""
    try:
        resp = requests.get(GROQ_API_URL.replace("/chat/completions", "/models"), headers={"Authorization": f"Bearer {clave}"}, timeout=8)
        ids = [m.get("id", "") for m in resp.json().get("data", [])]
        _vision["modelo"] = next((m for m in PREFERIDOS_VISION if m in ids), "") or \
            next((m for m in ids if re.search(r"scout|maverick|vision|(^|[-/_])vl([-_/]|$)", m, re.I)), "")
    except Exception as e:
        logger.warning(f"[facturas] No se pudo consultar los modelos de visión de Groq: {e}")
    return _vision["modelo"]


def _imagenes_para_ia(contenido, mime, max_paginas=2, lado=1600):
    """Imágenes JPEG (data URL base64) para el modelo de visión: la foto reducida, o las primeras páginas del PDF."""
    import base64 as b64
    from PIL import Image
    imagenes = []
    if mime == "application/pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(contenido)
        for i in range(min(len(pdf), max_paginas)):
            pagina = pdf[i]
            imagenes.append(pagina.render(scale=lado / max(pagina.get_width(), pagina.get_height())).to_pil())
    else:
        imagenes.append(Image.open(io.BytesIO(contenido)))
    salida = []
    for im in imagenes:
        im = im.convert("RGB")
        im.thumbnail((lado, lado))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        salida.append("data:image/jpeg;base64," + b64.b64encode(buf.getvalue()).decode())
    return salida


def _preguntar_ia_vision(prompt, imagenes):
    clave = clave_gestion()
    modelo = _modelo_vision()
    if not clave or not modelo:
        raise FacturaError("la lectura de imágenes no está disponible", 422)
    contenido_msg = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": u}} for u in imagenes]
    for modo_json in (True, False):
        restante = _restante()
        if restante < 6:
            raise FacturaError("la lectura tarda demasiado; vuelve a intentarlo", 504)
        cuerpo = {"model": modelo, "messages": [{"role": "user", "content": contenido_msg}], "temperature": 0, "max_tokens": MAX_SALIDA_IA}
        if modo_json:
            cuerpo["response_format"] = {"type": "json_object"}
        try:
            resp = requests.post(GROQ_API_URL, headers={"Authorization": f"Bearer {clave}", "Content-Type": "application/json"}, json=cuerpo,
                                 timeout=min(40, restante - 2))
            if resp.status_code in (429, 503):
                raise FacturaError("la IA está saturada ahora mismo (límite por minuto); se reintenta sola", 503)
            if resp.status_code == 400:
                if modo_json:
                    continue
                _vision["modelo"] = ""   # el modelo no acepta imágenes: no se vuelve a intentar durante una hora
                raise FacturaError("el modelo de visión no ha aceptado la imagen", 422)
            resp.raise_for_status()
            texto = (resp.json()["choices"][0]["message"].get("content") or "").strip()
            texto = re.sub(r"^```(json)?|```$", "", texto, flags=re.M).strip()
            return json.loads(texto[texto.find("{"): texto.rfind("}") + 1])
        except FacturaError:
            raise
        except Exception as e:
            logger.warning(f"[facturas] La visión ({modelo}) no devolvió datos válidos: {e}")
            break
    raise FacturaError("la IA no ha devuelto una lectura válida de esta imagen", 422)


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
    iva_no_desglosado = False
    if p and p > 0 and not c and b is not None and t is not None and abs(b - t) <= TOLERANCIA:
        # La IA suele rellenar «IVA 21 %» y «cuota 0» en un talonario o tique que solo trae el total: no es que no haya IVA, es que no se desglosa.
        # Se deja sin base/IVA/cuota para que la app ofrezca calcularlos («el total ya incluye el IVA») en vez de dar por buena una factura sin IVA.
        datos["iva_pct"] = datos["cuota_iva"] = None
        p = c = None
        iva_no_desglosado = True
    if c is None and b is not None and p is not None:
        datos["cuota_iva"] = c = round(b * p / 100, 2)
    if b is None and t is not None and c is not None:
        datos["base_imponible"] = b = round(t - c + r, 2)
    if t is None and b is not None and c is not None:
        datos["total"] = t = round(b + c - r, 2)
    if p is None and b and c is not None:
        datos["iva_pct"] = round(100 * c / b)

    avisos = []
    if iva_no_desglosado:
        avisos.append("La factura no desglosa el IVA. Si el total ya lo incluye, pulsa «El total ya incluye el IVA» para calcular la base y la cuota.")
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


def guardar_extracto(cliente_id, propiedad_id, archivo, es_admin, usuario=""):
    """Guarda el extracto bancario que sube el cliente en <raíz>/Facturas/<Propiedad>/<Año>/Extractos/ (para el cotejo con las facturas)."""
    import base64 as b64
    c = cliente(cliente_id)
    propiedad_id = comprobar_propiedad(cliente_id, propiedad_id, es_admin)
    if not habilitado(cliente_id):
        raise FacturaError("la subida de documentos no está configurada", 503)
    mime = str((archivo or {}).get("mime") or "").lower()
    if mime not in MIMES_EXTRACTO:
        raise FacturaError("formato no admitido: sube el extracto en CSV, Excel (XLS o XLSX) o PDF")
    try:
        contenido = b64.b64decode((archivo or {}).get("base64") or "", validate=False)
    except Exception:
        raise FacturaError("archivo dañado")
    if not contenido:
        raise FacturaError("archivo vacío")
    if len(contenido) > MAX_BYTES:
        raise FacturaError("archivo demasiado grande (máximo 10 MB)")
    ahora = datetime.now()
    nombre = f"{ahora:%Y-%m-%d}_Extracto_{_limpio(re.sub(r'[.][^.]*$', '', str((archivo or {}).get('nombre') or 'extracto')), 40)}{MIMES_EXTRACTO[mime]}"
    try:
        r = puente_plataforma.guardar(raiz_drive(cliente_id), [CARPETA_FACTURAS, c["propiedades"][propiedad_id], str(ahora.year), CARPETA_EXTRACTOS],
                                      nombre, mime, contenido, registro=None, descripcion=f"Extracto bancario subido por {str(usuario or '?')[:40]}")
    except puente_plataforma.PuenteError as e:
        logger.error(f"[facturas] Guardar el extracto en Drive: {e}")
        raise FacturaError("no se ha podido guardar en el Drive; vuelve a intentarlo en un momento", 502)
    logger.info(f"[facturas] Extracto guardado ({nombre}) por {usuario or '?'}")
    return {"nombre": r.get("nombre"), "url": r.get("url")}


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


def conocidas(cliente_id, huellas):
    """De estas huellas SHA-256 de archivos, las que ya están guardadas (así se puede volver a subir un ZIP entero y que solo se lean las nuevas)."""
    col = _coleccion()
    encontradas = []
    if col is None:
        return encontradas
    for h in list(dict.fromkeys(str(x) for x in (huellas or [])))[:200]:
        if not re.fullmatch(r"[0-9a-f]{64}", h):
            continue
        try:
            if col.document(_doc_id(cliente_id, h)).get().exists:
                encontradas.append(h)
        except Exception as e:
            logger.error(f"[facturas] Error consultando una huella: {e}")
    return encontradas


def _norm(texto):
    return re.sub(r"[^a-z0-9]", "", str(texto or "").lower())


def es_propia_empresa(c, datos):
    """¿El «proveedor/cliente» leído es el propio negocio? Una compra con ALC Homes como proveedor casi seguro es una lectura equivocada
    de quién emite (o una factura que no es de gasto): se avisa para revisarla y no sale como «lista para guardar»."""
    nombre = _norm(datos.get("proveedor"))
    if nombre and any(_norm(n) and _norm(n) in nombre for n in c.get("nombres_fiscales", [])):
        return True
    nif = str(datos.get("nif") or "").strip().upper()
    return bool(nif) and nif in {str(x).strip().upper() for x in c.get("nifs_propios", [])}


def misma_factura(cliente_id, datos):
    """Factura ya guardada con el mismo número y total (aunque venga de otro archivo, p. ej. un extracto o una copia): su resumen o None."""
    col = _coleccion()
    if col is None or not datos.get("numero") or datos.get("total") is None:
        return None
    try:
        for d in col.where("cliente", "==", cliente_id).where("numero", "==", datos["numero"]).where("total", "==", datos["total"]).stream():
            x = d.to_dict()
            return {k: x.get(k) for k in ("fecha", "proveedor", "total", "url")}
    except Exception as e:
        logger.error(f"[facturas] Error buscando duplicadas por número: {e}")
    return None


def leer(cliente_id, propiedad_id, archivo, es_admin):
    """Propuesta de datos de una factura para que el cliente la revise: {datos, avisos, duplicado}."""
    c = cliente(cliente_id)
    comprobar_propiedad(cliente_id, propiedad_id, es_admin)
    contenido, mime = _archivo(archivo)
    previa = ya_guardada(cliente_id, huella(contenido))
    _plazo.fin = time.time() + PRESUPUESTO_LEER_SEG
    try:
        crudo = None
        # Un escaneo, una foto o un talonario escrito a mano no tienen texto que leer: se le enseña la imagen a un modelo de visión (lee la letra
        # manuscrita mucho mejor que el OCR). Si no hay modelo o no acierta, se prueba con el OCR de siempre.
        if not capa_de_texto(contenido, mime) and _modelo_vision():
            try:
                crudo = _preguntar_ia_vision(_prompt(c, None, en_imagen=True), _imagenes_para_ia(contenido, mime))
            except FacturaError as e:
                if e.status in (503, 504):
                    raise
                logger.warning(f"[facturas] Visión sin resultado ({e}); se prueba con el OCR")
            except Exception as e:
                logger.warning(f"[facturas] No se pudo preparar la imagen para la visión: {e}")
        if crudo is None:
            try:
                texto = texto_de(contenido, mime)
            except puente_plataforma.PuenteError as e:
                logger.error(f"[facturas] OCR: {e}")
                raise FacturaError("no se ha podido leer el texto de la imagen; rellena los datos a mano", 502)
            if len(texto.strip()) < 15:
                raise FacturaError("la imagen no tiene texto legible; prueba con una foto más nítida o rellena los datos a mano", 422)
            crudo = _preguntar_ia(_prompt(c, texto))
        datos, avisos = normalizar(crudo)
    finally:
        _plazo.fin = None
    if es_propia_empresa(c, datos):
        avisos.insert(0, "El " + ("proveedor" if datos["tipo"] == "gasto" else "cliente") + " leído parece ser tu propia empresa: comprueba quién emite la factura.")
    duplicado = {k: previa.get(k) for k in ("fecha", "proveedor", "total", "url")} if previa else None
    if duplicado:
        avisos.insert(0, "Esta misma factura ya se subió antes.")
    else:
        otra = misma_factura(cliente_id, datos)
        if otra:
            duplicado = otra
            avisos.insert(0, "Ya hay una factura guardada con el mismo número y total (de otro archivo): puede ser un duplicado.")
    return {"datos": datos, "avisos": avisos, "duplicado": duplicado}


def _celda(v):
    """Valor para la hoja de registro. Un texto leído de una factura que empiece por = + - @ se interpretaría como FÓRMULA al abrir la hoja
    (inyección de fórmulas): se antepone un apóstrofo para que quede como texto. Los números no se tocan."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


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
    fila = [_celda(v) for v in (datos["fecha"], "Gasto" if datos["tipo"] == "gasto" else "Ingreso", datos["proveedor"], datos["nif"], datos["numero"],
                                datos["concepto"], datos["categoria"], datos["base_imponible"], datos["iva_pct"], datos["cuota_iva"],
                                datos["retencion_pct"], datos["retencion"], datos["total"], "{{ENLACE}}", ahora, str(usuario or "")[:40], doc_id)]
    try:
        r = puente_plataforma.guardar(raiz_drive(cliente_id), [CARPETA_FACTURAS, propiedad, anio, trimestre, carpeta_tipo], nombre, mime, contenido,
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


def meses_con_facturas(cliente_id, propiedad_id, es_admin):
    """Resumen por mes (más reciente primero) de lo ya subido de una propiedad: [{mes, facturas, gasto, ingreso}]. Para ver de un vistazo lo que hay."""
    propiedad_id = comprobar_propiedad(cliente_id, propiedad_id, es_admin)
    col = _coleccion()
    resumen = {}
    if col is not None:
        try:
            for d in col.where("cliente", "==", cliente_id).where("propiedad_id", "==", propiedad_id).stream():
                x = d.to_dict()
                if not x.get("mes"):
                    continue
                r = resumen.setdefault(x["mes"], {"mes": x["mes"], "facturas": 0, "gasto": 0.0, "ingreso": 0.0})
                r["facturas"] += 1
                r["gasto" if x.get("tipo") == "gasto" else "ingreso"] += (-1 if x.get("es_rectificativa") else 1) * (x.get("total") or 0)
        except Exception as e:
            logger.error(f"[facturas] Error resumiendo los meses: {e}")
            raise FacturaError("no se han podido cargar las facturas", 502)
    return [{**r, "gasto": round(r["gasto"], 2), "ingreso": round(r["ingreso"], 2)} for r in sorted(resumen.values(), key=lambda r: r["mes"], reverse=True)]


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

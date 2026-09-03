"""
services/pdf_extract.py — Desencriptado y parsing del PDF del parte de
viajero: nombre, teléfono, email, fechas de entrada/salida.
"""
import io
import re
import logging
from datetime import date

from pypdf import PdfReader, PdfWriter
import pdfplumber

from config import PDF_PASSWORD

logger = logging.getLogger(__name__)


def habitacion_desde_nombre_archivo(pdf_filename):
    nombre_base = pdf_filename.replace(".pdf", "")
    nombre_base = re.sub(r"[_\s]+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$", "", nombre_base)
    nombre_base = re.sub(r"[_\s]+\d{4}[-/]\d{2}[-/]\d{2}$", "", nombre_base)
    nombre_base = nombre_base.replace("_", " ").strip()
    m = re.search(r"(Habitaci[oó]n\s+\S+(?:\s+\d+)?)", nombre_base, re.IGNORECASE)
    if m:
        hab = m.group(1).strip()
        hab = re.sub(r"[Hh]abitacion", "Habitación", hab, flags=re.IGNORECASE)
        return hab
    return nombre_base.title()


def parsear_fecha(texto_fecha):
    texto_fecha = texto_fecha.strip()
    m = re.match(r"^(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})$", texto_fecha)
    if m:
        try: return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError: pass
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", texto_fecha)
    if m:
        try: return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError: pass
    return None


def extraer_fechas_por_etiqueta(texto):
    entrada = salida = None
    patrones = [
        (re.compile(r"fecha\s*de?\s*entrada[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})", re.I), "entrada"),
        (re.compile(r"check[\s\-]?in[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})", re.I), "entrada"),
        (re.compile(r"fecha\s*de?\s*salida[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})", re.I), "salida"),
        (re.compile(r"check[\s\-]?out[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})", re.I), "salida"),
    ]
    for patron, destino in patrones:
        m = patron.search(texto)
        if m:
            d = parsear_fecha(m.group(1))
            if d:
                if destino == "entrada" and entrada is None: entrada = d.isoformat()
                elif destino == "salida" and salida is None: salida = d.isoformat()
    return entrada, salida


def _limpiar_nombre(s):
    """Limpia ruido al final de un nombre capturado por regex."""
    s = re.split(r"\s{2,}|\t", s)[0].strip()
    s = re.sub(r"\d+", "", s).strip()
    s = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ\s\-']", "", s).strip()
    return s.title() if len(s) >= 3 else None


def extraer_nombre_completo(texto):
    """
    Extrae el nombre completo del parte de viajero.
    Prueba múltiples formatos que usa registroparteviajeros.com.
    """
    # Formato 1: "Nombre y apellidos: ..."
    m = re.search(r"nombre\s+y\s+apellidos?\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][^\n\r]{2,50})", texto, re.I)
    if m: return _limpiar_nombre(m.group(1))

    # Formato 2: "Nombre completo: ..."
    m = re.search(r"nombre\s+completo\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][^\n\r]{2,50})", texto, re.I)
    if m: return _limpiar_nombre(m.group(1))

    # Formato 3: Nombre + Primer apellido + Segundo apellido (campos separados)
    m_n  = re.search(r"(?:^|\n)\s*nombre\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]{1,25})", texto, re.I | re.M)
    m_a1 = re.search(r"primer\s+apellido\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]{1,30})", texto, re.I)
    m_a2 = re.search(r"segundo\s+apellido\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]{0,30})", texto, re.I)
    if m_n:
        partes = [m_n.group(1).strip()]
        if m_a1: partes.append(m_a1.group(1).strip())
        if m_a2 and m_a2.group(1).strip(): partes.append(m_a2.group(1).strip())
        nombre = " ".join(partes)
        return nombre.title() if len(nombre) >= 3 else None

    # Formato 4: "Apellidos: ... Nombre: ..."
    m_ap = re.search(r"apellidos?\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][^\n\r]{2,40})", texto, re.I)
    m_n2 = re.search(r"(?:^|\n)\s*nombre\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]{1,25})", texto, re.I | re.M)
    if m_n2 and m_ap:
        nombre = f"{m_n2.group(1).strip()} {_limpiar_nombre(m_ap.group(1)) or ''}".strip()
        return nombre.title() if len(nombre) >= 3 else None

    return None


def extraer_telefono(texto):
    """
    Extrae número de teléfono del parte de viajero.
    Busca primero por etiqueta, luego por patrón de 9 dígitos españoles.
    """
    # Por etiqueta
    patrones_etiqueta = [
        re.compile(r"tel[eé]fono\s*[:\s]+([\+\d][\d\s\-]{7,18})", re.I),
        re.compile(r"m[oó]vil\s*[:\s]+([\+\d][\d\s\-]{7,18})", re.I),
        re.compile(r"\btel[\.:\s]+([\+\d][\d\s\-]{7,18})", re.I),
        re.compile(r"phone\s*[:\s]+([\+\d][\d\s\-]{7,18})", re.I),
    ]
    for patron in patrones_etiqueta:
        m = patron.search(texto)
        if m:
            tel = re.sub(r"[\s\-\.]", "", m.group(1)).strip()
            if 9 <= len(tel) <= 15:
                return tel
    return None


def procesar_pdf_bytes(pdf_bytes, pdf_filename, incluir_texto=False):
    resultado = {"habitacion": habitacion_desde_nombre_archivo(pdf_filename),
                 "nombre": None, "telefono": None,
                 "email": None, "fecha_entrada": None, "fecha_salida": None,
                 "texto_extraido": None, "error": None}
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            if reader.decrypt(PDF_PASSWORD) == 0:
                resultado["error"] = "Contraseña incorrecta"; return resultado
        writer = PdfWriter()
        for page in reader.pages: writer.add_page(page)
        buf = io.BytesIO(); writer.write(buf); buf.seek(0)
    except Exception as e:
        resultado["error"] = f"Error desencriptando: {e}"; return resultado
    try:
        partes = []
        with pdfplumber.open(buf) as pdf:
            for p in pdf.pages:
                t = p.extract_text()
                if t: partes.append(t)
        texto = "\n".join(partes)
    except Exception as e:
        resultado["error"] = f"Error extrayendo texto: {e}"; return resultado
    if incluir_texto: resultado["texto_extraido"] = texto
    emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", texto)
    validos = [e for e in emails if "registroparteviajeros" not in e.lower()]
    if validos: resultado["email"] = validos[0]
    resultado["nombre"]   = extraer_nombre_completo(texto)
    resultado["telefono"] = extraer_telefono(texto)
    entrada, salida = extraer_fechas_por_etiqueta(texto)
    resultado["fecha_entrada"] = entrada
    resultado["fecha_salida"] = salida
    if not entrada or not salida:
        m_gen = re.search(r"generado[:\s]+(\d{1,2}/\d{2}/\d{4})", texto, re.I)
        fecha_gen = parsear_fecha(m_gen.group(1)) if m_gen else None
        todas = re.findall(r"\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})\b", texto)
        fechas_iso = []
        for dia, mes, anyo in todas:
            try:
                d = date(int(anyo), int(mes), int(dia))
                if fecha_gen and d == fecha_gen: continue
                fechas_iso.append(d.isoformat())
            except ValueError: continue
        vistas = set(); fechas_unicas = []
        for f in fechas_iso:
            if f not in vistas: vistas.add(f); fechas_unicas.append(f)
        if not entrada and len(fechas_unicas) >= 1: resultado["fecha_entrada"] = fechas_unicas[0]
        if not salida  and len(fechas_unicas) >= 2: resultado["fecha_salida"]  = fechas_unicas[1]
    if resultado["fecha_entrada"] and resultado["fecha_salida"]:
        if resultado["fecha_salida"] < resultado["fecha_entrada"]:
            resultado["fecha_entrada"], resultado["fecha_salida"] = resultado["fecha_salida"], resultado["fecha_entrada"]
    return resultado

import os
import io
import re
import base64
import logging
import requests
from flask import Flask, request, jsonify, render_template_string
from pypdf import PdfReader, PdfWriter
import pdfplumber
from datetime import date

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PDF_PASSWORD = os.environ.get("PDF_PASSWORD", "Alchomes2025")
API_TOKEN    = os.environ.get("API_TOKEN", "")
TEST_TOKEN   = os.environ.get("TEST_TOKEN", "test1234")

HABITACIONES = {
    "habitacion simple 1": "Habitación Simple 1",
    "habitacion simple 2": "Habitación Simple 2",
    "habitacion simple 3": "Habitación Simple 3",
    "habitacion doble 1":  "Habitación Doble 1",
    "habitacion doble 2":  "Habitación Doble 2",
}

TEST_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test — Hostal PDF Extractor</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1.5rem}
  .card{background:#fff;border-radius:12px;border:1px solid #e0e0e0;padding:2rem;width:100%;max-width:580px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
  h1{font-size:1.2rem;font-weight:600;margin-bottom:.25rem}
  .sub{font-size:.85rem;color:#666;margin-bottom:1.75rem}
  label{display:block;font-size:.82rem;font-weight:500;color:#444;margin-bottom:.35rem}
  input[type=text],input[type=file]{width:100%;padding:.55rem .75rem;border:1px solid #d0d0d0;border-radius:8px;font-size:.9rem;margin-bottom:1.1rem}
  input[type=file]{padding:.4rem .5rem;cursor:pointer}
  button{width:100%;padding:.7rem;background:#5c2d91;color:#fff;border:none;border-radius:8px;font-size:.95rem;font-weight:500;cursor:pointer;transition:background .15s}
  button:hover{background:#4a2275}
  button:disabled{background:#aaa;cursor:not-allowed}
  .result{margin-top:1.5rem;padding:1.25rem;border-radius:8px;font-size:.875rem}
  .result.ok{background:#f0faf4;border:1px solid #a3d9b5}
  .result.err{background:#fff5f5;border:1px solid #f5b8b8}
  .field{display:flex;justify-content:space-between;padding:.45rem 0;border-bottom:1px solid #eee}
  .field:last-child{border-bottom:none}
  .field .key{color:#555;font-weight:500}
  .field .val{color:#222;text-align:right;max-width:65%;word-break:break-all}
  .field .val.ok{color:#1a7a3f;font-weight:600}
  .field .val.err{color:#c0392b;font-weight:600}
  .spinner{display:none;text-align:center;margin-top:1.2rem;color:#888;font-size:.85rem}
  details{margin-top:.75rem}
  details summary{cursor:pointer;font-size:.8rem;color:#666;padding:.3rem 0;outline:none}
  .raw{background:#f7f7f7;border:1px solid #e0e0e0;border-radius:6px;padding:.75rem;font-size:.75rem;font-family:monospace;color:#333;white-space:pre-wrap;word-break:break-all;max-height:220px;overflow-y:auto;margin-top:.4rem}
</style>
</head>
<body>
<div class="card">
  <h1>🏨 Hostal PDF Extractor — Test</h1>
  <p class="sub">Sube un parte de viajero para comprobar que los datos se extraen correctamente.</p>
  <label>Contraseña de acceso al test</label>
  <input type="text" id="token" placeholder="test1234" />
  <label>Archivo PDF (parte de viajero)</label>
  <input type="file" id="pdffile" accept=".pdf" />
  <button id="btn" onclick="enviar()">Analizar PDF</button>
  <div class="spinner" id="spin">⏳ Procesando…</div>
  <div id="out"></div>
</div>
<script>
async function enviar() {
  const token = document.getElementById('token').value.trim();
  const fileInput = document.getElementById('pdffile');
  const out = document.getElementById('out');
  const btn = document.getElementById('btn');
  const spin = document.getElementById('spin');
  out.innerHTML = '';
  if (!token) { alert('Introduce la contraseña de test'); return; }
  if (!fileInput.files.length) { alert('Selecciona un PDF'); return; }
  const file = fileInput.files[0];
  const reader = new FileReader();
  reader.onload = async function(e) {
    const b64 = e.target.result.split(',')[1];
    btn.disabled = true;
    spin.style.display = 'block';
    try {
      const resp = await fetch('/extraer-test', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ pdf_base64: b64, pdf_filename: file.name, token: token })
      });
      const data = await resp.json();
      if (data.ok) {
        const warn = (v) => v ? '' : ' style="color:#c0392b;font-weight:600"';
        const fmt  = (v) => v || '<span style="color:#c0392b">⚠ No encontrado</span>';
        out.innerHTML = `
          <div class="result ok">
            <div class="field"><span class="key">Estado</span><span class="val ok">✅ Procesado correctamente</span></div>
            <div class="field"><span class="key">Habitación</span><span class="val"${warn(data.habitacion)}>${fmt(data.habitacion)}</span></div>
            <div class="field"><span class="key">Email cliente</span><span class="val"${warn(data.email)}>${fmt(data.email)}</span></div>
            <div class="field"><span class="key">Fecha entrada</span><span class="val"${warn(data.fecha_entrada)}>${fmt(data.fecha_entrada)}</span></div>
            <div class="field"><span class="key">Fecha salida</span><span class="val"${warn(data.fecha_salida)}>${fmt(data.fecha_salida)}</span></div>
          </div>
          <details><summary>🔍 Ver texto completo extraído del PDF</summary><div class="raw">${escHtml(data.texto_extraido || '(vacío)')}</div></details>
          <details><summary>{ } Ver respuesta JSON completa</summary><div class="raw">${escHtml(JSON.stringify(data, null, 2))}</div></details>`;
      } else {
        out.innerHTML = `<div class="result err"><div class="field"><span class="key">Estado</span><span class="val err">❌ Error</span></div><div class="field"><span class="key">Mensaje</span><span class="val">${escHtml(data.error)}</span></div></div>`;
      }
    } catch(err) {
      out.innerHTML = `<div class="result err"><div class="field"><span class="key">Error de red</span><span class="val">${escHtml(String(err))}</span></div></div>`;
    } finally {
      btn.disabled = false;
      spin.style.display = 'none';
    }
  };
  reader.readAsDataURL(file);
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
</script>
</body>
</html>"""


def normalizar(texto):
    t = texto.lower()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")]:
        t = t.replace(a, b)
    t = t.replace("habitación", "habitacion")
    t = re.sub(r"[^a-z0-9 ]", "", t).strip()
    return t


def habitacion_desde_nombre_archivo(pdf_filename):
    nombre_base = pdf_filename.replace(".pdf", "").replace("_", " ").strip()
    clave = normalizar(nombre_base)
    for k, v in HABITACIONES.items():
        if k in clave or clave in k:
            return v
    return nombre_base.title()


def parsear_fecha(texto_fecha):
    texto_fecha = texto_fecha.strip()
    m = re.match(r"^(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})$", texto_fecha)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", texto_fecha)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def extraer_fechas_por_etiqueta(texto):
    entrada = None
    salida  = None
    patron_entrada = re.compile(
        r"fecha\s*de?\s*entrada[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})",
        re.IGNORECASE)
    patron_salida = re.compile(
        r"fecha\s*de?\s*salida[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})",
        re.IGNORECASE)
    patron_checkin = re.compile(
        r"check[\s\-]?in[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})",
        re.IGNORECASE)
    patron_checkout = re.compile(
        r"check[\s\-]?out[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})",
        re.IGNORECASE)
    for patron, destino in [
        (patron_entrada,  "entrada"),
        (patron_checkin,  "entrada"),
        (patron_salida,   "salida"),
        (patron_checkout, "salida"),
    ]:
        m = patron.search(texto)
        if m:
            d = parsear_fecha(m.group(1))
            if d:
                if destino == "entrada" and entrada is None:
                    entrada = d.isoformat()
                elif destino == "salida" and salida is None:
                    salida = d.isoformat()
    return entrada, salida


def procesar_pdf_bytes(pdf_bytes, pdf_filename, incluir_texto=False):
    resultado = {
        "habitacion": habitacion_desde_nombre_archivo(pdf_filename),
        "email": None,
        "fecha_entrada": None,
        "fecha_salida": None,
        "texto_extraido": None,
        "error": None,
    }
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            rc = reader.decrypt(PDF_PASSWORD)
            if rc == 0:
                resultado["error"] = "Contraseña incorrecta"
                return resultado
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
    except Exception as e:
        resultado["error"] = f"Error desencriptando: {e}"
        return resultado
    try:
        partes = []
        with pdfplumber.open(buf) as pdf:
            for p in pdf.pages:
                t = p.extract_text()
                if t:
                    partes.append(t)
        texto = "\n".join(partes)
    except Exception as e:
        resultado["error"] = f"Error extrayendo texto: {e}"
        return resultado
    if incluir_texto:
        resultado["texto_extraido"] = texto
    emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", texto)
    validos = [e for e in emails if "registroparteviajeros" not in e.lower()]
    if validos:
        resultado["email"] = validos[0]
    entrada, salida = extraer_fechas_por_etiqueta(texto)
    resultado["fecha_entrada"] = entrada
    resultado["fecha_salida"]  = salida
    if not entrada or not salida:
        fecha_gen = None
        m_gen = re.search(r"generado[:\s]+(\d{1,2}/\d{2}/\d{4})", texto, re.IGNORECASE)
        if m_gen:
            fecha_gen = parsear_fecha(m_gen.group(1))
        todas = re.findall(r"\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})\b", texto)
        fechas_iso = []
        for dia, mes, anyo in todas:
            try:
                d = date(int(anyo), int(mes), int(dia))
                if fecha_gen and d == fecha_gen:
                    continue
                fechas_iso.append(d.isoformat())
            except ValueError:
                continue
        vistas = set()
        fechas_unicas = []
        for f in fechas_iso:
            if f not in vistas:
                vistas.add(f)
                fechas_unicas.append(f)
        if not entrada and len(fechas_unicas) >= 1:
            resultado["fecha_entrada"] = fechas_unicas[0]
        if not salida and len(fechas_unicas) >= 2:
            resultado["fecha_salida"] = fechas_unicas[1]
    if resultado["fecha_entrada"] and resultado["fecha_salida"]:
        if resultado["fecha_salida"] < resultado["fecha_entrada"]:
            resultado["fecha_entrada"], resultado["fecha_salida"] = (
                resultado["fecha_salida"], resultado["fecha_entrada"])
    return resultado


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "servicio": "Hostal PDF Extractor"}), 200


@app.route("/test", methods=["GET"])
def test_page():
    return render_template_string(TEST_PAGE)


@app.route("/extraer-test", methods=["POST"])
def extraer_test():
    data = request.get_json(force=True)
    if data.get("token") != TEST_TOKEN:
        return jsonify({"ok": False, "error": "Contraseña incorrecta"}), 401
    if "pdf_base64" not in data:
        return jsonify({"ok": False, "error": "Falta pdf_base64"}), 400
    try:
        pdf_bytes = base64.b64decode(data["pdf_base64"])
    except Exception as e:
        return jsonify({"ok": False, "error": f"Base64 inválido: {e}"}), 400
    r = procesar_pdf_bytes(pdf_bytes, data.get("pdf_filename", "documento.pdf"), incluir_texto=True)
    if r["error"]:
        return jsonify({"ok": False, "error": r["error"]}), 500
    return jsonify({"ok": True, "habitacion": r["habitacion"], "email": r["email"],
                    "fecha_entrada": r["fecha_entrada"], "fecha_salida": r["fecha_salida"],
                    "texto_extraido": r["texto_extraido"]}), 200


@app.route("/extraer", methods=["POST"])
def extraer():
    """
    Acepta dos modos:
    Modo A (base64):  { "pdf_base64": "...", "pdf_filename": "...", "token": "..." }
    Modo B (URL):     { "pdf_url": "...", "pdf_filename": "...", "token": "...", "gmail_token": "..." }
    """
    data = request.get_json(force=True)
    if API_TOKEN and data.get("token") != API_TOKEN:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    pdf_filename = data.get("pdf_filename", "documento.pdf")

    # Modo C: email MIME crudo (Make Raw content)
    if "raw_email" in data:
        import email as emaillib
        try:
            raw = data["raw_email"]
            # Make puede enviar el MIME como string o base64
            try:
                mime_bytes = base64.b64decode(raw)
            except Exception:
                mime_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw

            msg = emaillib.message_from_bytes(mime_bytes)
            pdf_bytes = None
            found_filename = None

            for part in msg.walk():
                ct = part.get_content_type()
                cd = part.get("Content-Disposition", "")
                fn = part.get_filename("")
                if ct == "application/pdf" or (fn and fn.lower().endswith(".pdf")):
                    payload = part.get_payload(decode=True)
                    if payload:
                        pdf_bytes = payload
                        found_filename = fn or pdf_filename
                        break

            if not pdf_bytes:
                return jsonify({"ok": False, "error": "No se encontró PDF en el email MIME"}), 400

            if found_filename:
                pdf_filename = found_filename

        except Exception as e:
            return jsonify({"ok": False, "error": f"Error procesando MIME: {e}"}), 500

    # Modo B: descarga directa desde URL (Gmail attachment URL)
    elif "pdf_url" in data:
        gmail_token = data.get("gmail_token", "")
        pdf_url = data["pdf_url"]
        try:
            headers = {}
            if gmail_token:
                headers["Authorization"] = f"Bearer {gmail_token}"
            resp = requests.get(pdf_url, headers=headers, timeout=30)
            resp.raise_for_status()
            pdf_bytes = resp.content
        except Exception as e:
            return jsonify({"ok": False, "error": f"Error descargando PDF: {e}"}), 500

    # Modo A: base64
    elif "pdf_base64" in data:
        try:
            pdf_bytes = base64.b64decode(data["pdf_base64"])
        except Exception as e:
            return jsonify({"ok": False, "error": f"Base64 inválido: {e}"}), 400
    else:
        return jsonify({"ok": False, "error": "Falta pdf_base64 o pdf_url"}), 400

    r = procesar_pdf_bytes(pdf_bytes, pdf_filename, incluir_texto=False)
    if r["error"]:
        return jsonify({"ok": False, "error": r["error"]}), 500

    logger.info(f"OK → hab={r['habitacion']} email={r['email']} "
                f"entrada={r['fecha_entrada']} salida={r['fecha_salida']}")

    return jsonify({"ok": True, "habitacion": r["habitacion"], "email": r["email"],
                    "fecha_entrada": r["fecha_entrada"], "fecha_salida": r["fecha_salida"]}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
# NOTA: este bloque no se usa, solo verificando sintaxis

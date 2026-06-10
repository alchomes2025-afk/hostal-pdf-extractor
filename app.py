import os
import io
import re
import base64
import logging
import requests
import json
from flask import Flask, request, jsonify, render_template_string, redirect
from flask_cors import CORS
from pypdf import PdfReader, PdfWriter
import pdfplumber
from datetime import date

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PDF_PASSWORD   = os.environ.get("PDF_PASSWORD", "Alchomes2025")
API_TOKEN      = os.environ.get("API_TOKEN", "")
TEST_TOKEN     = os.environ.get("TEST_TOKEN", "test1234")
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
REDIRECT_URI   = os.environ.get("REDIRECT_URI", "https://hostal-pdf-extractor.onrender.com/oauth/callback")

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
  button{width:100%;padding:.7rem;background:#5c2d91;color:#fff;border:none;border-radius:8px;font-size:.95rem;font-weight:500;cursor:pointer}
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
  details summary{cursor:pointer;font-size:.8rem;color:#666;padding:.3rem 0}
  .raw{background:#f7f7f7;border:1px solid #e0e0e0;border-radius:6px;padding:.75rem;font-size:.75rem;font-family:monospace;white-space:pre-wrap;word-break:break-all;max-height:220px;overflow-y:auto;margin-top:.4rem}
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
  const out = document.getElementById('out'); const btn = document.getElementById('btn');
  const spin = document.getElementById('spin');
  out.innerHTML = '';
  if (!token) { alert('Introduce la contraseña'); return; }
  if (!fileInput.files.length) { alert('Selecciona un PDF'); return; }
  const file = fileInput.files[0];
  const reader = new FileReader();
  reader.onload = async function(e) {
    const b64 = e.target.result.split(',')[1];
    btn.disabled = true; spin.style.display = 'block';
    try {
      const resp = await fetch('/extraer-test', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ pdf_base64: b64, pdf_filename: file.name, token: token })
      });
      const data = await resp.json();
      if (data.ok) {
        const fmt = (v) => v || '<span style="color:#c0392b">⚠ No encontrado</span>';
        out.innerHTML = `<div class="result ok">
          <div class="field"><span class="key">Estado</span><span class="val ok">✅ Correcto</span></div>
          <div class="field"><span class="key">Habitación</span><span class="val">${fmt(data.habitacion)}</span></div>
          <div class="field"><span class="key">Email</span><span class="val">${fmt(data.email)}</span></div>
          <div class="field"><span class="key">Fecha entrada</span><span class="val">${fmt(data.fecha_entrada)}</span></div>
          <div class="field"><span class="key">Fecha salida</span><span class="val">${fmt(data.fecha_salida)}</span></div>
        </div>
        <details><summary>🔍 Texto extraído del PDF</summary><div class="raw">${escHtml(data.texto_extraido||'')}</div></details>
        <details><summary>{ } JSON completo</summary><div class="raw">${escHtml(JSON.stringify(data,null,2))}</div></details>`;
      } else {
        out.innerHTML = `<div class="result err"><div class="field"><span class="key">Error</span><span class="val err">${escHtml(data.error)}</span></div></div>`;
      }
    } catch(err) {
      out.innerHTML = `<div class="result err"><div class="field"><span class="key">Error de red</span><span class="val">${escHtml(String(err))}</span></div></div>`;
    } finally { btn.disabled = false; spin.style.display = 'none'; }
  };
  reader.readAsDataURL(file);
}
function escHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
</script></body></html>"""


# ── OAuth helpers ──────────────────────────────────────────────────────────────

def get_access_token():
    """Obtiene un access token fresco usando el refresh token guardado."""
    refresh_token = GOOGLE_REFRESH_TOKEN
    if not refresh_token:
        raise Exception("GOOGLE_REFRESH_TOKEN no configurado. Ve a /oauth/inicio para autorizarlo.")
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def descargar_adjunto_gmail(message_id, access_token):
    """Descarga el primer adjunto PDF de un email de Gmail por su message_id."""
    # 1. Obtener metadatos del mensaje
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    msg = resp.json()

    # 2. Buscar la parte que es PDF
    def buscar_pdf(parts):
        for part in parts:
            mime = part.get("mimeType", "")
            filename = part.get("filename", "")
            body = part.get("body", {})
            if mime == "application/pdf" or filename.lower().endswith(".pdf"):
                attachment_id = body.get("attachmentId")
                if attachment_id:
                    return attachment_id, filename
            # Recursar en subpartes
            subparts = part.get("parts", [])
            if subparts:
                result = buscar_pdf(subparts)
                if result:
                    return result
        return None, None

    parts = msg.get("payload", {}).get("parts", [])
    attachment_id, filename = buscar_pdf(parts)

    if not attachment_id:
        raise Exception(f"No se encontró adjunto PDF en el mensaje {message_id}")

    # 3. Descargar el adjunto
    url2 = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}"
    resp2 = requests.get(url2, headers=headers, timeout=30)
    resp2.raise_for_status()
    data_b64 = resp2.json().get("data", "")
    # Gmail devuelve base64url, convertir a bytes
    pdf_bytes = base64.urlsafe_b64decode(data_b64 + "==")
    return pdf_bytes, filename


# ── PDF helpers ────────────────────────────────────────────────────────────────

def normalizar(texto):
    t = texto.lower()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")]:
        t = t.replace(a, b)
    t = t.replace("habitación","habitacion")
    t = re.sub(r"[^a-z0-9 ]","",t).strip()
    return t

def habitacion_desde_nombre_archivo(pdf_filename):
    nombre_base = pdf_filename.replace(".pdf","").replace("_"," ").strip()
    clave = normalizar(nombre_base)
    for k, v in HABITACIONES.items():
        if k in clave or clave in k:
            return v
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

def procesar_pdf_bytes(pdf_bytes, pdf_filename, incluir_texto=False):
    resultado = {"habitacion": habitacion_desde_nombre_archivo(pdf_filename),
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
        if not salida and len(fechas_unicas) >= 2: resultado["fecha_salida"] = fechas_unicas[1]
    if resultado["fecha_entrada"] and resultado["fecha_salida"]:
        if resultado["fecha_salida"] < resultado["fecha_entrada"]:
            resultado["fecha_entrada"], resultado["fecha_salida"] = resultado["fecha_salida"], resultado["fecha_entrada"]
    return resultado


# ── Rutas ──────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    token_ok = bool(GOOGLE_REFRESH_TOKEN)
    return jsonify({"status": "ok", "servicio": "Hostal PDF Extractor", "gmail_autorizado": token_ok}), 200

@app.route("/test", methods=["GET"])
def test_page():
    return render_template_string(TEST_PAGE)

@app.route("/oauth/inicio", methods=["GET"])
def oauth_inicio():
    """Redirige a Google para autorizar acceso a Gmail."""
    from urllib.parse import urlencode
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "access_type": "offline",
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return redirect(url)

@app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Google redirige aquí con el código de autorización."""
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"<h2>Error: {error}</h2>", 400
    if not code:
        return "<h2>No se recibió código de autorización</h2>", 400
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=15)
    tokens = resp.json()
    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        return f"<h2>Error obteniendo refresh_token</h2><pre>{json.dumps(tokens, indent=2)}</pre>", 400
    return f"""<h2>✅ Autorización completada</h2>
    <p>Copia este valor y añádelo en Render como variable de entorno:</p>
    <p><strong>GOOGLE_REFRESH_TOKEN</strong></p>
    <pre style="background:#f0f0f0;padding:1rem;border-radius:8px;word-break:break-all">{refresh_token}</pre>
    <p>Una vez guardado en Render, el servidor podrá acceder a Gmail automáticamente.</p>"""

@app.route("/debug-mensaje", methods=["POST"])
def debug_mensaje():
    """Devuelve la estructura completa del mensaje para depuración."""
    data = request.get_json(force=True)
    if data.get("token") != TEST_TOKEN:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    try:
        access_token = get_access_token()
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{data['message_id']}"
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        msg = resp.json()
        # Extraer solo la estructura de partes (sin datos binarios)
        def resumir_partes(parts, nivel=0):
            resultado = []
            for p in parts:
                item = {
                    "mimeType": p.get("mimeType",""),
                    "filename": p.get("filename",""),
                    "hasAttachmentId": bool(p.get("body",{}).get("attachmentId")),
                    "bodySize": p.get("body",{}).get("size",0),
                }
                subparts = p.get("parts",[])
                if subparts:
                    item["parts"] = resumir_partes(subparts, nivel+1)
                resultado.append(item)
            return resultado
        payload = msg.get("payload",{})
        estructura = {
            "mimeType": payload.get("mimeType",""),
            "filename": payload.get("filename",""),
            "hasAttachmentId": bool(payload.get("body",{}).get("attachmentId")),
            "parts": resumir_partes(payload.get("parts",[]))
        }
        return jsonify({"ok": True, "estructura": estructura}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
    if r["error"]: return jsonify({"ok": False, "error": r["error"]}), 500
    return jsonify({"ok": True, "habitacion": r["habitacion"], "email": r["email"],
                    "fecha_entrada": r["fecha_entrada"], "fecha_salida": r["fecha_salida"],
                    "texto_extraido": r["texto_extraido"]}), 200

@app.route("/extraer", methods=["POST"])
def extraer():
    """
    Modo A (message_id): { "message_id": "...", "token": "..." }
    Modo B (base64):     { "pdf_base64": "...", "pdf_filename": "...", "token": "..." }
    """
    data = request.get_json(force=True)
    if API_TOKEN and data.get("token") != API_TOKEN:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    pdf_filename = data.get("pdf_filename", "documento.pdf")

    if "message_id" in data:
        try:
            access_token = get_access_token()
            pdf_bytes, fn = descargar_adjunto_gmail(data["message_id"], access_token)
            if fn: pdf_filename = fn
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    elif "pdf_base64" in data:
        try:
            pdf_bytes = base64.b64decode(data["pdf_base64"])
        except Exception as e:
            return jsonify({"ok": False, "error": f"Base64 inválido: {e}"}), 400
    else:
        return jsonify({"ok": False, "error": "Falta message_id o pdf_base64"}), 400

    r = procesar_pdf_bytes(pdf_bytes, pdf_filename)
    if r["error"]: return jsonify({"ok": False, "error": r["error"]}), 500

    logger.info(f"OK → hab={r['habitacion']} email={r['email']} entrada={r['fecha_entrada']} salida={r['fecha_salida']}")
    return jsonify({"ok": True, "habitacion": r["habitacion"], "email": r["email"],
                    "fecha_entrada": r["fecha_entrada"], "fecha_salida": r["fecha_salida"]}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# Ruta de debug visual (se añade al final del fichero como parche)
DEBUG_PAGE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><title>Debug Gmail</title>
<style>body{font-family:system-ui;padding:2rem;max-width:600px;margin:auto}
input{width:100%;padding:.5rem;margin:.5rem 0 1rem;border:1px solid #ccc;border-radius:6px}
button{padding:.6rem 1.5rem;background:#5c2d91;color:#fff;border:none;border-radius:6px;cursor:pointer}
pre{background:#f4f4f4;padding:1rem;border-radius:6px;white-space:pre-wrap;word-break:break-all;font-size:.8rem}
</style></head><body>
<h2>🔍 Debug estructura email Gmail</h2>
<label>Test token</label><input id="tok" value="test1234"/>
<label>Message ID (del email original de registroparteviajeros.com)</label>
<input id="mid" placeholder="ej: 19eac43cf2de581f"/>
<button onclick="run()">Analizar</button>
<pre id="out">Resultado aparecerá aquí...</pre>
<script>
async function run(){
  const out=document.getElementById('out');
  out.textContent='Consultando...';
  try{
    const r=await fetch('/debug-mensaje',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message_id:document.getElementById('mid').value,
        token:document.getElementById('tok').value})});
    const d=await r.json();
    out.textContent=JSON.stringify(d,null,2);
  }catch(e){out.textContent='Error: '+e;}
}
</script></body></html>"""

@app.route("/debug", methods=["GET"])
def debug_page():
    return render_template_string(DEBUG_PAGE)

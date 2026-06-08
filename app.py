import os
import io
import re
import base64
import logging
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
  .card{background:#fff;border-radius:12px;border:1px solid #e0e0e0;padding:2rem;width:100%;max-width:560px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
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
  .field .val{color:#222;text-align:right;max-width:60%;word-break:break-all}
  .field .val.ok{color:#1a7a3f;font-weight:600}
  .field .val.err{color:#c0392b;font-weight:600}
  .spinner{display:none;text-align:center;margin-top:1.2rem;color:#888;font-size:.85rem}
  .raw{margin-top:1rem;background:#f7f7f7;border:1px solid #e0e0e0;border-radius:6px;padding:.75rem;font-size:.78rem;font-family:monospace;color:#333;white-space:pre-wrap;word-break:break-all;max-height:180px;overflow-y:auto}
  .raw summary{cursor:pointer;font-size:.8rem;color:#666;margin-bottom:.4rem;outline:none}
</style>
</head>
<body>
<div class="card">
  <h1>🏨 Hostal PDF Extractor — Test</h1>
  <p class="sub">Sube un parte de viajero para comprobar que los datos se extraen correctamente.</p>

  <label>Contraseña de acceso al test</label>
  <input type="text" id="token" placeholder="La que configuraste en TEST_TOKEN" />

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
        body: JSON.stringify({
          pdf_base64: b64,
          pdf_filename: file.name,
          token: token
        })
      });
      const data = await resp.json();

      if (data.ok) {
        out.innerHTML = `
          <div class="result ok">
            <div class="field"><span class="key">Estado</span><span class="val ok">✅ Extraído correctamente</span></div>
            <div class="field"><span class="key">Habitación</span><span class="val">${data.habitacion || '—'}</span></div>
            <div class="field"><span class="key">Email cliente</span><span class="val">${data.email || '—'}</span></div>
            <div class="field"><span class="key">Fecha entrada</span><span class="val">${data.fecha_entrada || '—'}</span></div>
            <div class="field"><span class="key">Fecha salida</span><span class="val">${data.fecha_salida || '—'}</span></div>
          </div>
          <details><summary>Ver respuesta completa (JSON)</summary><div class="raw">${JSON.stringify(data, null, 2)}</div></details>`;
      } else {
        out.innerHTML = `
          <div class="result err">
            <div class="field"><span class="key">Estado</span><span class="val err">❌ Error</span></div>
            <div class="field"><span class="key">Mensaje</span><span class="val">${data.error}</span></div>
          </div>`;
      }
    } catch(err) {
      out.innerHTML = `<div class="result err"><div class="field"><span class="key">Error de red</span><span class="val">${err}</span></div></div>`;
    } finally {
      btn.disabled = false;
      spin.style.display = 'none';
    }
  };
  reader.readAsDataURL(file);
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


def extraer_datos_pdf(pdf_bytes, pdf_filename):
    resultado = {
        "habitacion": habitacion_desde_nombre_archivo(pdf_filename),
        "email": None,
        "fecha_entrada": None,
        "fecha_salida": None,
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

    emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", texto)
    validos = [e for e in emails if "registroparteviajeros" not in e.lower()]
    if validos:
        resultado["email"] = validos[0]

    fechas_raw = re.findall(r"\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})\b", texto)
    fechas_iso = []
    for dia, mes, anyo in fechas_raw:
        try:
            fechas_iso.append(date(int(anyo), int(mes), int(dia)).isoformat())
        except ValueError:
            continue

    if len(fechas_iso) >= 1:
        resultado["fecha_entrada"] = fechas_iso[0]
    if len(fechas_iso) >= 2:
        resultado["fecha_salida"] = fechas_iso[1]

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
    """Igual que /extraer pero autenticado con TEST_TOKEN (separado del token de Make)."""
    data = request.get_json(force=True)

    if data.get("token") != TEST_TOKEN:
        return jsonify({"ok": False, "error": "Contraseña incorrecta"}), 401

    if "pdf_base64" not in data:
        return jsonify({"ok": False, "error": "Falta pdf_base64"}), 400

    try:
        pdf_bytes = base64.b64decode(data["pdf_base64"])
    except Exception as e:
        return jsonify({"ok": False, "error": f"Base64 inválido: {e}"}), 400

    r = extraer_datos_pdf(pdf_bytes, data.get("pdf_filename", "documento.pdf"))
    if r["error"]:
        return jsonify({"ok": False, "error": r["error"]}), 500

    logger.info(f"TEST OK → hab={r['habitacion']} email={r['email']} "
                f"entrada={r['fecha_entrada']} salida={r['fecha_salida']}")

    return jsonify({
        "ok": True,
        "habitacion": r["habitacion"],
        "email": r["email"],
        "fecha_entrada": r["fecha_entrada"],
        "fecha_salida": r["fecha_salida"],
    }), 200


@app.route("/extraer", methods=["POST"])
def extraer():
    data = request.get_json(force=True)

    if API_TOKEN and data.get("token") != API_TOKEN:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    if "pdf_base64" not in data:
        return jsonify({"ok": False, "error": "Falta pdf_base64"}), 400

    try:
        pdf_bytes = base64.b64decode(data["pdf_base64"])
    except Exception as e:
        return jsonify({"ok": False, "error": f"Base64 inválido: {e}"}), 400

    r = extraer_datos_pdf(pdf_bytes, data.get("pdf_filename", "documento.pdf"))
    if r["error"]:
        return jsonify({"ok": False, "error": r["error"]}), 500

    logger.info(f"OK → hab={r['habitacion']} email={r['email']} "
                f"entrada={r['fecha_entrada']} salida={r['fecha_salida']}")

    return jsonify({
        "ok": True,
        "habitacion": r["habitacion"],
        "email": r["email"],
        "fecha_entrada": r["fecha_entrada"],
        "fecha_salida": r["fecha_salida"],
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

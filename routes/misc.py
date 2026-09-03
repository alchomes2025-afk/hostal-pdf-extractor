"""
routes/misc.py — Health check y página de test manual de extracción de PDF.
"""
from flask import Blueprint, jsonify, render_template_string

from config import GOOGLE_REFRESH_TOKEN, CALLMEBOT_PHONE, CALLMEBOT_API_KEY

misc_bp = Blueprint("misc", __name__)


@misc_bp.route("/", methods=["GET"])
def health():
    token_ok = bool(GOOGLE_REFRESH_TOKEN)
    callmebot_ok = bool(CALLMEBOT_PHONE and CALLMEBOT_API_KEY)
    return jsonify({
        "status": "ok",
        "servicio": "Hostal PDF Extractor",
        "gmail_autorizado": token_ok,
        "callmebot_configurado": callmebot_ok,
    }), 200


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


@misc_bp.route("/test", methods=["GET"])
def test_page():
    return render_template_string(TEST_PAGE)

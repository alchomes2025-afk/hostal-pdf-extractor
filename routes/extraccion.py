"""
routes/extraccion.py — Extracción del PDF del parte de viajero y envío del
código de puerta: /debug-mensaje, /extraer-test, /extraer, /probar-ultimo-parte.
"""
import base64
import logging
import requests
from flask import Blueprint, request, jsonify

from config import API_TOKEN, TEST_TOKEN, ROOM_CONFIG, GmailAuthError
from services.gmail import get_access_token, descargar_adjunto_gmail, buscar_message_ids_gmail
from services.pdf_extract import procesar_pdf_bytes
from services.rooms import detectar_room_id, extraer_localizador
from services.beds24 import enviar_codigo_puerta_beds24

logger = logging.getLogger(__name__)
extraccion_bp = Blueprint("extraccion", __name__)


@extraccion_bp.route("/debug-mensaje", methods=["POST"])
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


@extraccion_bp.route("/extraer-test", methods=["POST"])
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


@extraccion_bp.route("/extraer", methods=["POST"])
def extraer():
    """
    Modo A (message_id): { "message_id": "...", "token": "..." }
    Modo B (base64):     { "pdf_base64": "...", "pdf_filename": "...", "token": "..." }
    """
    data = request.get_json(force=True)
    if API_TOKEN and data.get("token") != API_TOKEN:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    pdf_filename = data.get("pdf_filename", "documento.pdf")
    cuerpo_email = ""

    if "message_id" in data:
        try:
            access_token = get_access_token()
            pdf_bytes, fn, cuerpo_email = descargar_adjunto_gmail(data["message_id"], access_token)
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

    r = procesar_pdf_bytes(pdf_bytes, pdf_filename, incluir_texto=True)
    if r["error"]: return jsonify({"ok": False, "error": r["error"]}), 500

    logger.info(f"OK → hab={r['habitacion']} email={r['email']} entrada={r['fecha_entrada']} salida={r['fecha_salida']}")

    # Envío del código de puerta a través de Booking.com Messages (Beds24)
    # Combinamos el texto del PDF con el cuerpo del email, ya que el localizador
    # (identificador fiable de la habitación real) viene en el cuerpo del email.
    texto_para_detectar = (r.get("texto_extraido") or "") + "\n" + cuerpo_email
    beds24_resultado = enviar_codigo_puerta_beds24(
        habitacion_texto=r["habitacion"],
        texto_completo=texto_para_detectar,
        fecha_entrada=r["fecha_entrada"],
        nombre_cliente=r.get("nombre"),
    )

    return jsonify({"ok": True, "habitacion": r["habitacion"], "email": r["email"],
                    "fecha_entrada": r["fecha_entrada"], "fecha_salida": r["fecha_salida"],
                    "codigo_puerta": beds24_resultado}), 200


@extraccion_bp.route("/probar-ultimo-parte", methods=["GET"])
def probar_ultimo_parte():
    """
    Endpoint de prueba manual: busca el email de 'Parte de viajeros' más reciente
    en Gmail y ejecuta el mismo flujo que /extraer (extracción + envío del código
    de puerta por Beds24), sin necesitar Make ni conocer el message_id.

    Uso (modo simulación, no envía nada al cliente):
        /probar-ultimo-parte?token=Alchomes2025

    Uso (envío real, solo si estás seguro):
        /probar-ultimo-parte?token=Alchomes2025&enviar=1

    Uso (envío real SIN enlace ni teléfono, para diagnosticar bloqueos de Booking.com):
        /probar-ultimo-parte?token=Alchomes2025&enviar=1&minimo=1

    Uso (envío real con sendEmail=true, última variante de diagnóstico):
        /probar-ultimo-parte?token=Alchomes2025&enviar=1&con_email=1
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    dry_run = request.args.get("enviar", "0") != "1"
    version_minima = request.args.get("minimo", "0") == "1"
    con_email = request.args.get("con_email", "0") == "1"

    try:
        access_token = get_access_token()
    except GmailAuthError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error de autenticación Gmail: {e}"}), 500

    try:
        message_ids = buscar_message_ids_gmail(access_token, max_results=1)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error buscando en Gmail: {e}"}), 500

    if not message_ids:
        return jsonify({"ok": False, "error": "No se encontró ningún email de 'Parte de viajeros' en los últimos 90 días"}), 404

    message_id = message_ids[0]

    try:
        pdf_bytes, pdf_filename, cuerpo_email = descargar_adjunto_gmail(message_id, access_token)
    except Exception as e:
        return jsonify({"ok": False, "message_id": message_id, "error": f"Error descargando adjunto: {e}"}), 500

    r = procesar_pdf_bytes(pdf_bytes, pdf_filename, incluir_texto=True)
    if r["error"]:
        return jsonify({"ok": False, "message_id": message_id, "error": r["error"]}), 500

    texto_para_detectar = (r.get("texto_extraido") or "") + "\n" + cuerpo_email
    room_id_detectado = detectar_room_id(r["habitacion"], texto_para_detectar)
    localizador = extraer_localizador(texto_para_detectar)

    beds24_resultado = enviar_codigo_puerta_beds24(
        habitacion_texto=r["habitacion"],
        texto_completo=texto_para_detectar,
        fecha_entrada=r["fecha_entrada"],
        nombre_cliente=r.get("nombre"),
        dry_run=dry_run,
        version_minima=version_minima,
        enviar_tambien_email=con_email,
    )

    return jsonify({
        "ok": True,
        "modo": "SIMULACIÓN (no se envió nada)" if dry_run else "ENVÍO REAL",
        "message_id": message_id,
        "pdf_filename": pdf_filename,
        "habitacion_texto_pdf": r["habitacion"],
        "localizador_detectado": localizador,
        "room_id_detectado": room_id_detectado,
        "nombre_habitacion_real": ROOM_CONFIG.get(room_id_detectado, {}).get("nombre") if room_id_detectado else None,
        "email_cliente": r["email"],
        "fecha_entrada": r["fecha_entrada"],
        "fecha_salida": r["fecha_salida"],
        "codigo_puerta": beds24_resultado,
    }), 200

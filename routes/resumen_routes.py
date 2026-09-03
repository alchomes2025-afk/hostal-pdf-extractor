"""
routes/resumen_routes.py — Resumen diario por WhatsApp.
"""
import logging
from flask import Blueprint, request, jsonify

from config import API_TOKEN, TEST_TOKEN
from services.resumen import generar_mensaje_resumen
from services.whatsapp import enviar_whatsapp_callmebot

logger = logging.getLogger(__name__)
resumen_bp = Blueprint("resumen", __name__)


@resumen_bp.route("/resumen", methods=["GET", "POST"])
def resumen_whatsapp():
    """
    Genera y (opcionalmente) envía el resumen diario por WhatsApp via CallMeBot.

    GET  (on-demand / test sin enviar):
        /resumen?token=Alchomes2025&enviar=0
        /resumen?token=Alchomes2025&enviar=1&hora=09

    POST (Make.com scheduler — 9h y 21h):
        { "token": "Alchomes2025", "hora": "09" }
        { "token": "Alchomes2025", "hora": "21" }

    Respuesta:
        { "ok": true, "mensaje": "...", "enviado": true/false,
          "callmebot_resp": "...", "error_whatsapp": "..." }
    """
    if request.method == "POST":
        data   = request.get_json(force=True) or {}
        token  = data.get("token", "")
        enviar = data.get("enviar", True)   # por defecto SÍ envía en POST (Make)
        hora   = data.get("hora")
    else:
        token  = request.args.get("token", "")
        enviar = request.args.get("enviar", "1") == "1"
        hora   = request.args.get("hora")

    # Acepta API_TOKEN (Alchomes2025) o TEST_TOKEN (test1234)
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    try:
        mensaje = generar_mensaje_resumen(hora)
    except Exception as e:
        logger.error(f"Error generando resumen: {e}")
        return jsonify({"ok": False, "error": f"Error generando resumen: {e}"}), 500

    resultado = {"ok": True, "mensaje": mensaje, "enviado": False}

    if enviar:
        try:
            cb_resp = enviar_whatsapp_callmebot(mensaje)
            resultado["enviado"] = True
            resultado["callmebot_resp"] = cb_resp[:300]
        except Exception as e:
            logger.error(f"Error enviando WhatsApp: {e}")
            resultado["enviado"] = False
            resultado["error_whatsapp"] = str(e)

    return jsonify(resultado), 200

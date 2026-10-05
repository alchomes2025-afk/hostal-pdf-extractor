"""
routes/resumen_routes.py — Resumen diario por WhatsApp.
"""
import logging
from datetime import timedelta
from flask import Blueprint, request, jsonify

from config import API_TOKEN, TEST_TOKEN
from services.resumen import generar_mensaje_resumen
from services.whatsapp import enviar_whatsapp_callmebot
from services.checkins_ultima_hora import marcar_anunciados
from services.fechas import hoy_madrid

logger = logging.getLogger(__name__)
resumen_bp = Blueprint("resumen", __name__)


@resumen_bp.route("/resumen", methods=["GET", "POST"])
def resumen_whatsapp():
    """
    Genera y (opcionalmente) envía un resumen por WhatsApp via CallMeBot.

    Uso MANUAL / de prueba. Los dos resúmenes diarios (08:00 hoy y 23:00
    mañana) los envía solo /watchdog — ver services/resumen_programado.py.

    GET:
        /resumen?token=<TOKEN>&enviar=0              (ver el de hoy, sin enviar)
        /resumen?token=<TOKEN>&enviar=0&dia=manana   (ver el de mañana, sin enviar)
        /resumen?token=<TOKEN>&enviar=1&hora=09      (enviarlo)

    POST:
        { "token": "<TOKEN>", "hora": "09", "dia": "manana" }

    Respuesta:
        { "ok": true, "mensaje": "...", "enviado": true/false,
          "callmebot_resp": "...", "error_whatsapp": "..." }
    """
    if request.method == "POST":
        data   = request.get_json(force=True) or {}
        token  = data.get("token", "")
        enviar = data.get("enviar", True)   # por defecto SÍ envía en POST
        hora   = data.get("hora")
        dia_param = data.get("dia", "")
    else:
        token  = request.args.get("token", "")
        enviar = request.args.get("enviar", "1") == "1"
        hora   = request.args.get("hora")
        dia_param = request.args.get("dia", "")

    # Acepta API_TOKEN o TEST_TOKEN (test1234)
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    dia = hoy_madrid() + timedelta(days=1) if dia_param == "manana" else hoy_madrid()

    try:
        mensaje, book_ids_dia = generar_mensaje_resumen(hora, dia=dia)
    except Exception as e:
        logger.error(f"Error generando resumen: {e}")
        return jsonify({"ok": False, "error": f"Error generando resumen: {e}"}), 500

    resultado = {"ok": True, "mensaje": mensaje, "enviado": False}

    if enviar:
        try:
            cb_resp = enviar_whatsapp_callmebot(mensaje)
            resultado["enviado"] = True
            resultado["callmebot_resp"] = cb_resp[:300]
            # Solo tras confirmar el envío: las entradas del día (hostal +
            # Primavera) quedan "ya anunciadas" para que el chequeo de
            # última hora (cada 15 min desde /watchdog) no vuelva a avisar de
            # ellas — ver services/checkins_ultima_hora.py.
            marcar_anunciados(book_ids_dia, dia=dia)
        except Exception as e:
            logger.error(f"Error enviando WhatsApp: {e}")
            resultado["enviado"] = False
            resultado["error_whatsapp"] = str(e)

    return jsonify(resultado), 200

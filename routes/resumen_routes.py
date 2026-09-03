"""
routes/resumen_routes.py — Resumen diario por WhatsApp y procesado
automático de partes recibidos hoy (envío de código de puerta).
"""
import logging
from datetime import date
from flask import Blueprint, request, jsonify

from config import API_TOKEN, TEST_TOKEN, ROOM_CONFIG, GmailAuthError
from services.resumen import generar_mensaje_resumen
from services.whatsapp import enviar_whatsapp_callmebot
from services.rpv import parte_recibido_para
from services.beds24 import obtener_bookings_dia_beds24, enviar_codigo_por_room_id

logger = logging.getLogger(__name__)
resumen_bp = Blueprint("resumen", __name__)


@resumen_bp.route("/procesar-partes-hoy", methods=["GET", "POST"])
def procesar_partes_hoy():
    """
    Verifica si se han recibido partes de viajeros para las entradas de hoy
    y envía automáticamente el código de puerta a los que aún no lo tienen.

    Reemplaza el trigger de Gmail/Make.com — llamar desde Make.com cada
    10-15 minutos durante el día.

    GET  /procesar-partes-hoy?token=Alchomes2025
    POST { "token": "Alchomes2025" }

    Respuesta:
    {
      "ok": true,
      "fecha": "2026-07-17",
      "procesado": [
        { "habitacion": "Playa del Albir", "parte": "recibido", "accion": "código enviado ahora" },
        { "habitacion": "Cala Coveta Fumá", "parte": "pendiente", "accion": "ninguna" }
      ]
    }
    """
    if request.method == "POST":
        token = (request.get_json(force=True) or {}).get("token", "")
    else:
        token = request.args.get("token", "")

    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    hoy = date.today().isoformat()
    entradas = obtener_bookings_dia_beds24(hoy, tipo="checkin")

    if not entradas:
        return jsonify({"ok": True, "fecha": hoy, "procesado": [], "nota": "Sin entradas hoy"})

    resultado = []

    for entrada in entradas:
        room_id   = entrada["room_id"]
        huesped   = entrada["huesped"]
        hab_nombre = entrada["nombre_habitacion"]

        # Consultar RPV: ¿se ha enviado el parte?
        parte_ok = parte_recibido_para(room_id, hoy)

        if not parte_ok:
            resultado.append({
                "habitacion": hab_nombre,
                "huesped":    huesped,
                "parte":      "pendiente",
                "accion":     "ninguna — parte aún no recibido",
            })
            continue

        # Enviar código (solo si no se envió ya)
        envio = enviar_codigo_por_room_id(room_id, hoy, huesped)

        if envio.get("ya_enviado"):
            resultado.append({
                "habitacion": hab_nombre,
                "huesped":    huesped,
                "parte":      "recibido",
                "accion":     "código ya enviado anteriormente — omitido",
            })
        elif envio.get("enviado"):
            resultado.append({
                "habitacion": hab_nombre,
                "huesped":    huesped,
                "parte":      "recibido",
                "accion":     "✅ código enviado ahora por Booking.com",
            })
            # Notificación WhatsApp
            try:
                enviar_whatsapp_callmebot(
                    f"✅ ALCHOMES — Parte recibido · Código enviado\n"
                    f"Habitación: {ROOM_CONFIG.get(room_id, {}).get('nombre', hab_nombre)}\n"
                    f"Huésped: {huesped}\n"
                    f"Entrada: {hoy}"
                )
            except Exception as e:
                logger.error(f"[procesar] WhatsApp error: {e}")
        else:
            resultado.append({
                "habitacion": hab_nombre,
                "huesped":    huesped,
                "parte":      "recibido",
                "accion":     f"⚠️ error enviando código: {envio.get('error')}",
            })

    return jsonify({"ok": True, "fecha": hoy, "procesado": resultado})


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
    except GmailAuthError as e:
        logger.error(f"Gmail auth caducado: {e}")
        aviso = (
            "⚠️ ALCHOMES — Error de sistema\n\n"
            "El acceso a Gmail ha caducado.\n"
            "El resumen diario NO se está enviando.\n\n"
            "Renueva el acceso en:\n"
            "hostal-pdf-extractor.onrender.com/oauth/inicio"
        )
        try:
            enviar_whatsapp_callmebot(aviso)
        except Exception as we:
            logger.error(f"Error enviando aviso WhatsApp: {we}")
        return jsonify({"ok": False, "error": str(e), "aviso_enviado": True}), 500
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

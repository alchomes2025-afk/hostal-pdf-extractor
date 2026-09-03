"""
services/whatsapp.py — Envío de WhatsApp vía CallMeBot y sistema
centralizado de alertas.
"""
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

from config import CALLMEBOT_PHONE, CALLMEBOT_API_KEY, CALLMEBOT_PHONE_2, CALLMEBOT_API_KEY_2

logger = logging.getLogger(__name__)


def enviar_whatsapp_callmebot(mensaje):
    """
    Envía un mensaje WhatsApp via CallMeBot a uno o dos números.
    Requiere CALLMEBOT_PHONE y CALLMEBOT_API_KEY en Render.
    Opcionalmente CALLMEBOT_PHONE_2 y CALLMEBOT_API_KEY_2 para el segundo destinatario.
    """
    if not CALLMEBOT_PHONE or not CALLMEBOT_API_KEY:
        raise Exception(
            "Faltan variables de entorno CALLMEBOT_PHONE y/o CALLMEBOT_API_KEY. "
            "Añádelas en Render → Environment."
        )

    def _enviar(phone, apikey):
        url = (
            f"https://api.callmebot.com/whatsapp.php"
            f"?phone={phone}"
            f"&text={quote(mensaje)}"
            f"&apikey={apikey}"
        )
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            raise Exception(f"CallMeBot {phone} respondió {resp.status_code}: {resp.text[:300]}")
        logger.info(f"CallMeBot OK ({phone}): {resp.text[:120]}")
        return resp.text

    resultados = []
    resultados.append(_enviar(CALLMEBOT_PHONE, CALLMEBOT_API_KEY))

    if CALLMEBOT_PHONE_2 and CALLMEBOT_API_KEY_2:
        try:
            resultados.append(_enviar(CALLMEBOT_PHONE_2, CALLMEBOT_API_KEY_2))
        except Exception as e:
            logger.error(f"Error enviando al segundo número: {e}")
            resultados.append(f"ERROR número 2: {e}")

    return " | ".join(resultados)


def alerta(titulo, detalle="", nivel="critico"):
    """
    Sistema centralizado de alertas por WhatsApp.

    nivel:
      "critico"  → 🔴 error que impide el funcionamiento (auth caducada, servicio caído)
      "warning"  → 🟡 degradación parcial (parte no detectado, PIN no configurado)
      "info"     → 🟢 evento notable sin impacto en el servicio
    """
    emoji = {"critico": "🔴", "warning": "🟡", "info": "🟢"}.get(nivel, "⚠️")
    hora  = datetime.now(ZoneInfo('Europe/Madrid')).strftime("%d/%m/%Y %H:%M")
    msg   = f"{emoji} ALCHOMES — {titulo}"
    if detalle:
        msg += f"\n\n{detalle}"
    msg += f"\n\n🕐 {hora}"
    logger.log(
        logging.ERROR if nivel == "critico" else
        logging.WARNING if nivel == "warning" else logging.INFO,
        f"[ALERTA {nivel.upper()}] {titulo}: {detalle}"
    )
    try:
        enviar_whatsapp_callmebot(msg)
    except Exception as e:
        logger.error(f"No se pudo enviar alerta WhatsApp: {e}")


def avisar_error_critico(titulo, detalle):
    """
    Envía un aviso por WhatsApp cuando falla un punto crítico del flujo.
    No lanza excepción si el propio envío de WhatsApp falla.
    """
    alerta(titulo, detalle, nivel="critico")

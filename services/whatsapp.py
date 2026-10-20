"""
services/whatsapp.py — Envío de WhatsApp vía CallMeBot y sistema
centralizado de alertas.
"""
import logging
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

from services import avisos_registro
from config import (
    AVISOS_CLIENTE, AVISOS_EMAIL, CALLMEBOT_PHONE, CALLMEBOT_API_KEY, CALLMEBOT_PHONE_2, CALLMEBOT_API_KEY_2,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)

logger = logging.getLogger(__name__)

NOMBRE_REMITENTE_AVISOS = "Avisos del sistema"   # nombre visible del remitente de los avisos por correo
ETIQUETA_ASUNTO = "[Aviso ALCHOMES] "   # prefijo fijo del asunto de los avisos por correo (para el filtro de Gmail)


def _lista(texto):
    return [x.strip() for x in str(texto or "").replace(";", ",").split(",") if x.strip()]


def cabecera_aviso(mensaje):
    """Línea que dice de qué cliente y propiedad es un aviso: «📍 HostalFácil · ALC Homes · Hostal · La Casa de la Primavera».
    La propiedad se deduce del texto (el hostal tiene «habitaciones»; «Primavera» es la casa); un aviso general del sistema no
    nombra ninguna y sale solo con el cliente. Para varios clientes la etiqueta de propiedad deberá venir ya en cada aviso."""
    t = mensaje.lower()
    propiedades = []
    if "habitaci" in t or "hostal" in t:
        propiedades.append("Hostal")
    if "primavera" in t:
        propiedades.append("La Casa de la Primavera")
    return " · ".join(["📍 HostalFácil", AVISOS_CLIENTE] + propiedades)


def _enviar_telegram(mensaje):
    """Mensaje por el bot de Telegram a cada chat de TELEGRAM_CHAT_ID. Lanza excepción si alguno no se entrega."""
    errores = []
    for chat in _lista(TELEGRAM_CHAT_ID):
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                             json={"chat_id": chat, "text": (cabecera_aviso(mensaje) + "\n\n" + mensaje)[:4000]}, timeout=15)
        try:
            ok = resp.ok and resp.json().get("ok") is True
        except Exception:
            ok = False
        if not ok:
            errores.append(f"chat {chat}: HTTP {resp.status_code} {resp.text[:120]}")
    if errores:
        raise Exception("Telegram no entregó: " + " | ".join(errores))


def _sin_emojis(texto):
    """Quita los emojis de fuera del plano básico (📍, 🏨…): por el Apps Script llegaban al correo rotos («������»).
    Telegram y WhatsApp los llevan bien; solo se limpia el correo. Las tildes y el resto del texto no cambian."""
    return re.sub(r"[\U00010000-\U0010FFFF] ?", "", texto).replace("\ufe0f", "").replace("\u200d", "")


def _enviar_correo_aviso(mensaje):
    """El mensaje por correo a cada dirección de AVISOS_EMAIL. Asunto = «[Aviso ALCHOMES] » + primera línea: la etiqueta
    fija permite un filtro de Gmail que los archive (sin bandeja de entrada) y los deje buscables."""
    from services import puente_plataforma
    from services.email_send import enviar_email
    asunto = (ETIQUETA_ASUNTO + " ".join(_sin_emojis((mensaje.strip().splitlines() or ["Aviso"])[0]).split()))[:110]
    for destino in _lista(AVISOS_EMAIL):
        if puente_plataforma.configurado():   # desde la cuenta de la plataforma (hostalfacil@gmail.com)
            puente_plataforma.correo(destino, asunto, _sin_emojis(cabecera_aviso(mensaje) + "\n\n" + mensaje))
        else:                                  # sin puente: por el buzón de huéspedes, como hasta ahora
            enviar_email(to=destino, subject=asunto, body=_sin_emojis(cabecera_aviso(mensaje) + "\n\n" + mensaje),
                         nombre=NOMBRE_REMITENTE_AVISOS, timeout=30)


def _enviar_respaldos(mensaje):
    """Envía por los canales de respaldo configurados. Devuelve [(canal, error|None)]."""
    resultados = []
    for canal, configurado, enviar in (("Telegram", TELEGRAM_BOT_TOKEN and _lista(TELEGRAM_CHAT_ID), _enviar_telegram),
                                       ("correo", _lista(AVISOS_EMAIL), _enviar_correo_aviso)):
        if not configurado:
            continue
        try:
            enviar(mensaje)
            resultados.append((canal, None))
        except Exception as e:
            logger.error(f"[avisos] Respaldo por {canal} fallido: {e}")
            resultados.append((canal, str(e)))
    return resultados


def enviar_whatsapp_callmebot(mensaje):
    """
    Envía un aviso interno: por WhatsApp (CallMeBot) y, además, por los canales de respaldo configurados (Telegram y/o
    correo). Cuenta como enviado si llega por CUALQUIERA de ellos; si fallan todos lanza la excepción de WhatsApp. Sin
    canales de respaldo se comporta como siempre (solo WhatsApp).
    """
    error_whatsapp, respuesta = None, ""
    try:
        respuesta = _enviar_callmebot(mensaje)
    except Exception as e:
        error_whatsapp = e
    respaldos = _enviar_respaldos(mensaje)
    seguidos = avisos_registro.registrar(mensaje, None if error_whatsapp is None else str(error_whatsapp), respaldos)
    if error_whatsapp is not None:
        if seguidos == avisos_registro.FALLOS_PARA_AVISAR:
            _avisar_callmebot_caido(seguidos)
        if not any(error is None for _, error in respaldos):
            raise error_whatsapp
        logger.error(f"[avisos] WhatsApp falló pero el aviso llegó por respaldo: {error_whatsapp}")
        return "respaldo: " + ", ".join(canal for canal, error in respaldos if error is None)
    return respuesta


def _avisar_callmebot_caido(seguidos):
    """Una sola vez al llegar a FALLOS_PARA_AVISAR fallos seguidos: por los respaldos (no por WhatsApp, que es lo que falla)."""
    try:
        _enviar_respaldos(f"🟡 ALCHOMES — CallMeBot no está entregando WhatsApp: {seguidos} avisos seguidos sin llegar. "
                          "Los avisos siguen llegando por Telegram. Revisa CallMeBot (límite, número desvinculado o servicio caído).")
    except Exception as e:
        logger.error(f"[avisos] No se pudo avisar de que CallMeBot falla: {e}")


def _enviar_callmebot(mensaje):
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
        # CallMeBot responde HTTP 200 también cuando NO envía (límite, número desvinculado…) y entonces lo dice en el texto.
        # Solo cuenta como enviado si confirma que lo ha puesto en cola («Message queued»); si no, es un fallo y quien
        # llama reintenta (los resúmenes lo hacen en el watchdog siguiente) en lugar de darlo por enviado.
        if "queued" not in resp.text.lower():
            raise Exception(f"CallMeBot {phone} no confirmó el envío: {' '.join(resp.text.split())[-300:]}")
        logger.info(f"CallMeBot OK ({phone}): {resp.text[-120:]}")
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

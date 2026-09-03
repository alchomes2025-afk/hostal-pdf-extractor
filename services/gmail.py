"""
services/gmail.py — OAuth y descarga de adjuntos de Gmail (flujo legacy,
sustituido en producción por el polling directo a la API de RPV, pero se
conserva para /extraer con message_id, /probar-ultimo-parte y /resumen).
"""
import re
import base64
import logging
import requests

from config import (
    GmailAuthError,
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN,
)
from services.pdf_extract import procesar_pdf_bytes

logger = logging.getLogger(__name__)


def get_access_token():
    """Obtiene un access token fresco usando el refresh token guardado."""
    refresh_token = GOOGLE_REFRESH_TOKEN
    if not refresh_token:
        raise GmailAuthError("GOOGLE_REFRESH_TOKEN no configurado. Ve a /oauth/inicio para autorizarlo.")
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }, timeout=15)
    if resp.status_code == 400:
        raise GmailAuthError("Token OAuth de Gmail caducado o revocado. Renuévalo en /oauth/inicio")
    resp.raise_for_status()
    return resp.json()["access_token"]


def descargar_adjunto_gmail(message_id, access_token):
    """
    Descarga el primer adjunto PDF de un email de Gmail por su message_id,
    y además el cuerpo de texto del email (para poder extraer el localizador
    del parte, que identifica la habitación real de forma fiable).
    """
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    msg = resp.json()

    def buscar_pdf(parts, nivel=0):
        for i, part in enumerate(parts):
            mime   = part.get("mimeType", "")
            fn     = part.get("filename", "")
            body   = part.get("body", {})
            att_id = body.get("attachmentId", "")
            logger.info(f"  Parte[{nivel}][{i}]: mime={mime} fn={repr(fn)} att={bool(att_id)}")
            es_pdf = (mime == "application/pdf") or (fn and fn.lower().endswith(".pdf"))
            if es_pdf and att_id:
                logger.info(f"  PDF encontrado: {fn}")
                return att_id, fn
            subparts = part.get("parts", [])
            if subparts:
                r = buscar_pdf(subparts, nivel+1)
                if r[0]:
                    return r
        return None, None

    def buscar_cuerpo(parts, nivel=0):
        """Busca la parte text/plain (o si no, text/html) con el cuerpo del email."""
        html_fallback = None
        for part in parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data", "")
            if mime == "text/plain" and data:
                try:
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
                except Exception:
                    pass
            if mime == "text/html" and data and html_fallback is None:
                try:
                    html_fallback = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
                except Exception:
                    pass
            subparts = part.get("parts", [])
            if subparts:
                r = buscar_cuerpo(subparts, nivel+1)
                if r:
                    return r
        return html_fallback

    parts = msg.get("payload", {}).get("parts", [])
    logger.info(f"Buscando PDF en {message_id}, partes: {len(parts)}")
    attachment_id, filename = buscar_pdf(parts)

    # Cuerpo del email (best effort, no debe romper el flujo si falla)
    try:
        cuerpo_texto = buscar_cuerpo(parts) or ""
        cuerpo_texto = re.sub(r"<[^>]+>", " ", cuerpo_texto)  # limpiar HTML si vino de html_fallback
    except Exception as e:
        logger.warning(f"No se pudo extraer cuerpo del email {message_id}: {e}")
        cuerpo_texto = ""

    if not attachment_id:
        raise Exception(f"No se encontró adjunto PDF en el mensaje {message_id}")

    url2 = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}"
    resp2 = requests.get(url2, headers=headers, timeout=30)
    resp2.raise_for_status()
    data_b64 = resp2.json().get("data", "")
    pdf_bytes = base64.urlsafe_b64decode(data_b64 + "==")
    return pdf_bytes, filename, cuerpo_texto


def buscar_message_ids_gmail(access_token, max_results=30):
    """
    Busca en Gmail los emails de partes de viajeros de los últimos 90 días.
    Usa el mismo filtro de asunto que el módulo 1 de Make.com.
    """
    query = 'subject:"Parte de viajeros" has:attachment newer_than:90d'
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"q": query, "maxResults": max_results}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    msgs = resp.json().get("messages", [])
    logger.info(f"Gmail: encontrados {len(msgs)} emails de partes de viajeros")
    return [m["id"] for m in msgs]


def obtener_todas_reservas():
    """
    Extrae datos de todos los partes de viajeros de Gmail (últimos 90 días).
    Gmail devuelve los mensajes del más reciente al más antiguo, así que
    el primer ejemplar de cada (habitacion, fecha_entrada) es el más actual.
    Lanza GmailAuthError si el token OAuth ha caducado.
    """
    try:
        access_token = get_access_token()
    except GmailAuthError:
        raise  # propagar para que /resumen pueda avisar por WhatsApp
    except Exception as e:
        logger.error(f"Error obteniendo access token: {e}")
        return []

    try:
        message_ids = buscar_message_ids_gmail(access_token)
    except Exception as e:
        logger.error(f"Error buscando emails en Gmail: {e}")
        return []

    reservas = []
    claves_vistas = set()  # deduplicar por (habitacion, fecha_entrada)

    for msg_id in message_ids:
        try:
            pdf_bytes, filename, _cuerpo = descargar_adjunto_gmail(msg_id, access_token)
            r = procesar_pdf_bytes(pdf_bytes, filename or "documento.pdf")
            if r["error"]:
                logger.warning(f"PDF {msg_id}: {r['error']}")
                continue
            if not r["fecha_entrada"] or not r["fecha_salida"]:
                logger.warning(f"PDF {msg_id}: fechas no encontradas")
                continue
            clave = (r["habitacion"], r["fecha_entrada"])
            if clave in claves_vistas:
                logger.info(f"Duplicado ignorado: {clave}")
                continue
            claves_vistas.add(clave)
            reservas.append({
                "habitacion":    r["habitacion"],
                "nombre":        r["nombre"],
                "telefono":      r["telefono"],
                "email":         r["email"],
                "fecha_entrada": r["fecha_entrada"],
                "fecha_salida":  r["fecha_salida"],
            })
            logger.info(f"Reserva OK: {r['habitacion']} {r['fecha_entrada']}→{r['fecha_salida']}")
        except Exception as e:
            logger.warning(f"Error procesando mensaje {msg_id}: {e}")

    return reservas

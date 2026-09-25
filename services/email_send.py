"""
services/email_send.py — Envío de email reutilizando el proyecto de Google
Apps Script "ALC Homes — Identificación de reservas" (cuenta
alchomes2025guest@gmail.com, ya autorizado con Gmail vía OAuth).

No se usa SMTP directo (Render bloquea el tráfico saliente en planes
básicos) ni SendGrid (exige verificar el remitente, daba problemas). En su
lugar, ese script tiene un doPost(e) añadido que llama a
GmailApp.sendEmail() — ver memoria [[apps-script-orquestador]].
"""
import logging
import requests

from config import APPS_SCRIPT_EMAIL_URL, APPS_SCRIPT_EMAIL_SECRET

logger = logging.getLogger(__name__)


def enviar_email(to, subject, body):
    """
    Envía un email de texto plano pidiéndoselo al Web App de Apps Script
    (APPS_SCRIPT_EMAIL_URL en Render). El secreto compartido
    (APPS_SCRIPT_EMAIL_SECRET) debe coincidir con el SECRET_ESPERADO puesto
    dentro del script — si no, el script responde {"ok": false}.
    """
    if not APPS_SCRIPT_EMAIL_URL:
        raise Exception("APPS_SCRIPT_EMAIL_URL no configurada en Render")

    resp = requests.post(
        APPS_SCRIPT_EMAIL_URL,
        json={
            "to": to,
            "subject": subject,
            "body": body,
            "secret": APPS_SCRIPT_EMAIL_SECRET,
        },
        timeout=15,
    )
    try:
        data = resp.json()
    except Exception:
        data = {}

    if not resp.ok or not data.get("ok"):
        raise Exception(f"Apps Script email {resp.status_code}: {resp.text[:300]}")
    logger.info(f"[email_send] Email enviado a {to}: {subject}")

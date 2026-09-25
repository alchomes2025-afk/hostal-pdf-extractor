"""
services/email_send.py — Envío de email vía la API HTTP de SendGrid.

Se usa HTTP (no SMTP) porque Render bloquea el tráfico SMTP saliente en los
planes básicos — confirmado 25/09/2026 probando con Gmail SMTP directo
(credenciales correctas, pero "Network is unreachable" al intentar conectar).
"""
import logging
import requests

from config import SENDGRID_API_KEY, SENDGRID_FROM_EMAIL

logger = logging.getLogger(__name__)

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def enviar_email(to, subject, body):
    """
    Envía un email de texto plano vía SendGrid (SENDGRID_API_KEY en Render).
    SENDGRID_FROM_EMAIL debe estar verificado en SendGrid como "Single
    Sender" (Settings → Sender Authentication → Single Sender Verification).
    """
    if not SENDGRID_API_KEY:
        raise Exception("SENDGRID_API_KEY no configurada en Render")

    resp = requests.post(
        SENDGRID_API_URL,
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": SENDGRID_FROM_EMAIL},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
        timeout=15,
    )
    if not resp.ok:
        raise Exception(f"SendGrid {resp.status_code}: {resp.text[:300]}")
    logger.info(f"[email_send] Email enviado a {to}: {subject}")

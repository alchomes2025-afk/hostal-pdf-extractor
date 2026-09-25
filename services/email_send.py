"""
services/email_send.py — Envío de email vía SMTP (Gmail).
"""
import logging
import smtplib
from email.mime.text import MIMEText

from config import EMAIL_SMTP_USER, EMAIL_SMTP_PASSWORD

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def enviar_email(to, subject, body):
    """
    Envía un email de texto plano vía SMTP con la cuenta de Gmail configurada
    (EMAIL_SMTP_USER / EMAIL_SMTP_APP_PASSWORD en Render — esta última es una
    "contraseña de aplicación" de Google, no la contraseña normal).
    """
    if not EMAIL_SMTP_USER or not EMAIL_SMTP_PASSWORD:
        raise Exception("EMAIL_SMTP_USER / EMAIL_SMTP_APP_PASSWORD no configurados en Render")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SMTP_USER
    msg["To"] = to

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(EMAIL_SMTP_USER, EMAIL_SMTP_PASSWORD)
        server.sendmail(EMAIL_SMTP_USER, [to], msg.as_string())
    logger.info(f"[email_send] Email enviado a {to}: {subject}")

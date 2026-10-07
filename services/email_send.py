"""
services/email_send.py — Envío de email reutilizando el proyecto de Google
Apps Script "ALC Homes — Identificación de reservas" (cuenta
alchomes2025guest@gmail.com, ya autorizado con Gmail vía OAuth).

No se usa SMTP directo (Render bloquea el tráfico saliente en planes
básicos): ese script tiene un doPost(e) añadido que llama a
GmailApp.sendEmail() — ver memoria [[apps-script-orquestador]].
"""
import logging
import requests

from config import APPS_SCRIPT_EMAIL_URL, APPS_SCRIPT_EMAIL_SECRET

logger = logging.getLogger(__name__)

# Direcciones de reenvío (alias) que Booking.com y Airbnb dan a cada huésped. Esas
# plataformas solo reenvían al huésped lo que llega por su mensajería o desde el
# correo registrado de la propiedad: lo enviado desde nuestra cuenta de Gmail rebota
# con "Action needed: Your email did not reach the guest" y el huésped no lo recibe
# (verificado el 2026-10-07 en la bandeja de alchomes2025.guest@gmail.com).
DOMINIOS_RELAY_OTA = ("guest.booking.com", "guest.airbnb.com", "m.airbnb.com")


class DestinoNoPermitido(Exception):
    pass


def es_email_relay_ota(email):
    """True si es una dirección de reenvío de Booking.com o Airbnb."""
    return (email or "").rsplit("@", 1)[-1].strip().lower() in DOMINIOS_RELAY_OTA


def enviar_email(to, subject, body):
    """
    Envía un email de texto plano pidiéndoselo al Web App de Apps Script
    (APPS_SCRIPT_EMAIL_URL en Render). El secreto compartido
    (APPS_SCRIPT_EMAIL_SECRET) debe coincidir con el SECRET_ESPERADO puesto
    dentro del script — si no, el script responde {"ok": false}.
    No envía a direcciones de reenvío de Booking/Airbnb (ver DOMINIOS_RELAY_OTA).
    """
    if es_email_relay_ota(to):
        raise DestinoNoPermitido("dirección de reenvío de Booking/Airbnb: el correo rebotaría")
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

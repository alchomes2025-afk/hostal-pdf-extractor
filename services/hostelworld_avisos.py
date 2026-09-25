"""
services/hostelworld_avisos.py — Aviso por email del check-in online para
huéspedes de Hostelworld.

Hostelworld, a diferencia de Booking.com/Airbnb, no permite configurar
plantillas de mensajes preprogramados en Beds24 — así que sin esto, esos
huéspedes nunca reciben el enlace a la web de check-in antes de llegar.

Se envía UN día antes de la llegada, por email (Beds24 sí da el email del
huésped en estas reservas, aunque no permita automatizar el mensaje).

Lleva en Firestore (system_state/hostelworld_checkin_emails) el conjunto de
book_id ya avisados — sin reinicio diario, porque a cada reserva solo le
corresponde un único día de aviso (el anterior a su llegada), no hace falta
volver a comprobarla una vez enviado.
"""
import logging
from datetime import date, timedelta

import config
from services.beds24 import obtener_bookings_dia_beds24
from services.email_send import enviar_email

logger = logging.getLogger(__name__)

CHECKIN_URL = "https://alc-homes-checkin.web.app/"


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("hostelworld_checkin_emails")


def _leer_avisados():
    """book_id (como str) que ya recibieron el email — para siempre, no por día."""
    ref = _doc_ref()
    if ref is None:
        return set()
    try:
        doc = ref.get()
        if not doc.exists:
            return set()
        return set(str(b) for b in (doc.to_dict() or {}).get("book_ids", []))
    except Exception as e:
        logger.error(f"[hostelworld_avisos] Error leyendo estado en Firestore: {e}")
        return set()


def _marcar_avisado(book_id):
    ref = _doc_ref()
    if ref is None:
        return
    try:
        actuales = _leer_avisados() | {str(book_id)}
        ref.set({"book_ids": sorted(actuales)})
    except Exception as e:
        logger.error(f"[hostelworld_avisos] Error guardando estado en Firestore: {e}")


def _cuerpo_email():
    return (
        "¡Gracias por reservar en ALC HOMES!\n\n"
        "Es obligatorio hacer el check-in online en el siguiente enlace (recuerde que si son dos "
        "personas, deben rellenar la documentación de ambos huéspedes). Una vez haya completado el "
        "check-in online, en este mismo enlace encontrará también los códigos de acceso al "
        "establecimiento, el nombre de su habitación y el código de entrada a ésta, así como un "
        "asistente virtual que le ayudará en todo lo que necesite, tanto en el proceso de check-in "
        "como a lo largo de su estancia. Por favor, asegúrese de haber preguntado primero al "
        "asistente virtual antes de hacer uso del teléfono de información y asistencia:\n\n"
        f"{CHECKIN_URL}\n\n"
        "Si no puede ver el enlace, escríbanos a:\n\n"
        "alchomes2025.guest@gmail.com\n\n"
        "indicando en el asunto su nombre completo. En unos minutos le responderemos con el enlace "
        "de la web.\n\n"
        "Puede comunicarse con nosotros 24h, vía mensajes dentro de la plataforma de Booking, o por "
        "el teléfono y WhatsApp que aparece en su reserva, en la plataforma de Booking y en el "
        "enlace adjunto.\n\n"
        "El horario de entrada es a partir de las 15h y el acceso al establecimiento es por "
        "códigos, que tendrá a su disposición a partir de las 15:00 el día de su llegada en nuestra "
        "web, una vez haya completado el check-in online.\n\n"
        "Esperamos que sea todo de su agrado.\n\n"
        "Por favor, indíquenos su hora de llegada estimada.\n\n"
        "----------------------------------------------------------------------------\n\n"
        "Thank you for booking with ALC HOMES!\n\n"
        "Completing the online check-in at the following link is mandatory (please note that if "
        "there are two guests, you must fill out the documentation for both). Once you have "
        "completed the online check-in, this same link will also provide you with the property "
        "access codes, your room name and door code, as well as a virtual assistant to help you "
        "with anything you need — whether during the check-in process or throughout your stay. "
        "Please make sure to consult the virtual assistant before using the information and "
        "assistance phone line:\n\n"
        f"{CHECKIN_URL}\n\n"
        "If you can't see the link, please email us at:\n\n"
        "alchomes2025.guest@gmail.com\n\n"
        "With your full name in the subject line. We'll reply with the link in few minutes.\n\n"
        "You can contact us 24 hours a day via messages on the Booking platform, or by phone and "
        "WhatsApp using the number listed in your reservation, on the Booking platform, and in the "
        "link above.\n\n"
        "Check-in begins at 3:00 PM, and access to the property is via codes, which will be "
        "available on our website from 3:00 PM on your arrival day, once you have completed the "
        "online check-in.\n\n"
        "We hope everything is to your liking.\n\n"
        "Please let us know your estimated time of arrival."
    )


def enviar_avisos_checkin_hostelworld():
    """
    Consulta los check-ins de MAÑANA en Beds24 (todas las propiedades) y
    envía por email el enlace de check-in a los de canal Hostelworld que
    todavía no lo hayan recibido.

    Pensada para llamarse desde /watchdog (cada 15 min) — no lanza excepción
    hacia arriba: cualquier fallo se loguea y no debe bloquear el resto del
    watchdog.
    """
    manana_iso = (date.today() + timedelta(days=1)).isoformat()
    entradas = obtener_bookings_dia_beds24(manana_iso, tipo="checkin")
    hostelworld = [e for e in entradas if "hostelworld" in (e.get("canal") or "").strip().lower()]
    if not hostelworld:
        return

    avisados = _leer_avisados()
    for e in hostelworld:
        book_id = str(e.get("book_id"))
        if not book_id or book_id in avisados:
            continue
        email = e.get("email")
        if not email:
            logger.warning(f"[hostelworld_avisos] Reserva {book_id} (Hostelworld) sin email — no se puede avisar")
            continue
        try:
            enviar_email(
                to=email,
                subject="Gracias por reservar en ALC Homes / Thank you for your reservation with ALC Homes",
                body=_cuerpo_email(),
            )
            _marcar_avisado(book_id)
        except Exception as ex:
            logger.error(f"[hostelworld_avisos] Error enviando email a {email} (reserva {book_id}): {ex}")

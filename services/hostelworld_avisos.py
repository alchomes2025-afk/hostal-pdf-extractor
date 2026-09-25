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


def _cuerpo_email(nombre_habitacion):
    return (
        f"¡Hola!\n\n"
        f"Mañana es tu día de llegada a ALC Homes ({nombre_habitacion}).\n\n"
        f"Por favor, completa tu registro de entrada online antes de llegar, "
        f"en el siguiente enlace:\n\n"
        f"{CHECKIN_URL}\n\n"
        f"Introduce tu nombre completo (o el número de reserva) para acceder a tu reserva.\n\n"
        f"¡Te esperamos!\nALC Homes"
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
                subject="Tu check-in online — ALC Homes",
                body=_cuerpo_email(e.get("nombre_habitacion", "tu habitación")),
            )
            _marcar_avisado(book_id)
        except Exception as ex:
            logger.error(f"[hostelworld_avisos] Error enviando email a {email} (reserva {book_id}): {ex}")

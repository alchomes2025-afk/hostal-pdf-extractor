"""
services/registro_completado_avisos.py — Aviso por email de que el registro
de viajeros ya se ha completado, para TODOS los huéspedes (cualquier
plataforma), indicando que ya pueden volver a la web a por los códigos.

A diferencia del aviso de Hostelworld (services/hostelworld_avisos.py, que
avisa ANTES de la llegada con el enlace de check-in porque esa plataforma no
tiene mensajería automática), este aviso se dispara DESPUÉS: en cuanto el
parte de viajero queda registrado en RPV para una reserva próxima.

Se comprueban las llegadas de hoy hasta VENTANA_DIAS días por delante (en
una sola llamada por propiedad a Beds24, ver obtener_bookings_rango_beds24)
porque un huésped puede enviar el parte con bastante antelación respecto a
su llegada, no solo el mismo día.

Lleva en Firestore (system_state/registro_completado_emails) el conjunto de
book_id ya avisados — igual que en hostelworld_avisos.py.

El email se envía en un único idioma según el país del huésped — mismo
criterio que hostelworld_avisos.py (España/Hispanoamérica → castellano,
resto → inglés).
"""
import logging
from datetime import date, timedelta

import config
from services.beds24 import obtener_bookings_rango_beds24
from services.rpv import obtener_partes_recibidos_hoy
from services.email_send import enviar_email

logger = logging.getLogger(__name__)

CHECKIN_URL = "https://alc-homes-checkin.web.app/"
VENTANA_DIAS = 14

ROOM_ID_PRIMAVERA = "720841"

PAISES_HISPANOHABLANTES = {
    "ES", "MX", "AR", "CO", "PE", "VE", "CL", "EC", "GT", "CU", "BO",
    "DO", "HN", "PY", "SV", "NI", "CR", "PA", "UY", "PR",
}


def _idioma_email(country):
    if country and str(country).strip().upper() in PAISES_HISPANOHABLANTES:
        return "es"
    return "en"


def _nombre_propiedad(room_id):
    return "La Casa de la Primavera" if room_id == ROOM_ID_PRIMAVERA else "ALC Homes Self Check-in"


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("registro_completado_emails")


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
        logger.error(f"[registro_completado_avisos] Error leyendo estado en Firestore: {e}")
        return set()


def _marcar_avisado(book_id):
    ref = _doc_ref()
    if ref is None:
        return
    try:
        actuales = _leer_avisados() | {str(book_id)}
        ref.set({"book_ids": sorted(actuales)})
    except Exception as e:
        logger.error(f"[registro_completado_avisos] Error guardando estado en Firestore: {e}")


def _cuerpo_email_es(nombre, nombre_propiedad):
    return (
        f"Estimado/a {nombre},\n\n"
        f"Ya ha completado el registro de viajeros en {nombre_propiedad}.\n\n"
        "Vuelva a entrar en nuestra web a partir de las 15:00 horas del día de su "
        "check-in, para obtener los códigos de entrada al establecimiento y el "
        "resto de información de interés para su estancia:\n\n"
        f"{CHECKIN_URL}\n\n"
        "Atentamente,\nEquipo ALC Homes"
    )


def _cuerpo_email_en(nombre, nombre_propiedad):
    return (
        f"Dear {nombre},\n\n"
        f"You have now completed the guest registration for {nombre_propiedad}.\n\n"
        "Please return to our website from 3:00 PM on your check-in day to obtain "
        "the property access codes and other useful information for your stay:\n\n"
        f"{CHECKIN_URL}\n\n"
        "Kind regards,\nALC Homes Team"
    )


def enviar_avisos_registro_completado():
    """
    Consulta las llegadas de hoy a hoy+VENTANA_DIAS (todas las propiedades,
    una sola llamada por propiedad) y, cruzando con los partes ya recibidos
    en RPV, envía el aviso de "ya puede volver a la web a por sus códigos" a
    las reservas que aún no lo hayan recibido.

    Pensada para llamarse desde /watchdog (cada 15 min) — no lanza excepción
    hacia arriba: cualquier fallo se loguea y no debe bloquear el resto del
    watchdog.
    """
    partes = obtener_partes_recibidos_hoy()
    if not partes:
        return

    hoy = date.today()
    hasta = hoy + timedelta(days=VENTANA_DIAS)
    entradas = obtener_bookings_rango_beds24(hoy.isoformat(), hasta.isoformat(), tipo="checkin")
    if not entradas:
        return

    avisados = _leer_avisados()
    for e in entradas:
        room_id = e.get("room_id")
        arrival = e.get("arrival")
        if (room_id, arrival) not in partes:
            continue
        book_id = str(e.get("book_id"))
        if not book_id or book_id in avisados:
            continue
        email = e.get("email")
        if not email:
            logger.warning(f"[registro_completado_avisos] Reserva {book_id} sin email — no se puede avisar")
            continue

        nombre = e.get("huesped") or "Huésped"
        nombre_propiedad = _nombre_propiedad(room_id)
        idioma = _idioma_email(e.get("country"))
        if idioma == "es":
            subject = f"Registro completado — {nombre_propiedad}: vuelva a la web para sus códigos"
            body = _cuerpo_email_es(nombre, nombre_propiedad)
        else:
            subject = f"Registration complete — {nombre_propiedad}: return to our website for your codes"
            body = _cuerpo_email_en(nombre, nombre_propiedad)

        try:
            enviar_email(to=email, subject=subject, body=body)
            _marcar_avisado(book_id)
        except Exception as ex:
            logger.error(f"[registro_completado_avisos] Error enviando email a {email} (reserva {book_id}): {ex}")

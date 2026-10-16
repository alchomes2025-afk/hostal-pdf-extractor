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
from datetime import timedelta

import config
from services.beds24 import clave_grupo_entrada, obtener_bookings_rango_beds24
from services.rpv import obtener_estado_partes
from services.email_send import enviar_email, es_email_relay_ota
from services.acceso_enlace import url_checkin
from services.fechas import hoy_madrid

logger = logging.getLogger(__name__)

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


def _marcar_avisado(*claves):
    """Anota como avisados los book_id (y la clave del grupo) a los que ya se ha escrito."""
    ref = _doc_ref()
    if ref is None:
        return
    try:
        actuales = _leer_avisados() | {str(c) for c in claves if c}
        ref.set({"book_ids": sorted(actuales)})
    except Exception as e:
        logger.error(f"[registro_completado_avisos] Error guardando estado en Firestore: {e}")


def _cuerpo_email_es(nombre, nombre_propiedad, url, habitaciones=1):
    return (
        f"Estimado/a {nombre},\n\n"
        + (f"Ya se ha completado el registro de viajeros de al menos una de las {habitaciones} habitaciones de su reserva en {nombre_propiedad}. "
           "Cada habitación tiene su propio registro y su propio código: en el mismo enlace verá el estado de todas.\n\n" if habitaciones > 1
           else f"Ya ha completado el registro de viajeros en {nombre_propiedad}.\n\n")
        + "Vuelva a entrar en nuestra web a partir de las 15:00 horas del día de su "
        "check-in, para obtener los códigos de entrada al establecimiento y el "
        "resto de información de interés para su estancia:\n\n"
        f"{url}\n\n"
        "Atentamente,\nEquipo ALC Homes"
    )


def _cuerpo_email_en(nombre, nombre_propiedad, url, habitaciones=1):
    return (
        f"Dear {nombre},\n\n"
        + (f"The guest registration for at least one of the {habitaciones} rooms of your booking at {nombre_propiedad} is now complete. "
           "Each room has its own registration and its own door code: on the same link you will see the status of all of them.\n\n" if habitaciones > 1
           else f"You have now completed the guest registration for {nombre_propiedad}.\n\n")
        + "Please return to our website from 3:00 PM on your check-in day to obtain "
        "the property access codes and other useful information for your stay:\n\n"
        f"{url}\n\n"
        "Kind regards,\nALC Homes Team"
    )


def _candidatos_registro_completado():
    """
    Calcula qué reservas recibirían el aviso ahora mismo (parte recibido en
    RPV + todavía no avisadas), sin enviar nada ni tocar Firestore. Separado
    de enviar_avisos_registro_completado() para poder reutilizarlo también
    desde un endpoint de diagnóstico de solo lectura.

    Devuelve una lista de dicts: {book_id, room_id, arrival, email, idioma,
    subject, body, huesped, nombre_propiedad}.
    """
    estados, _ = obtener_estado_partes(max_age=10 * 60)
    completados = {k for k, v in estados.items() if v["completado"]}
    if not completados:
        return []

    hoy = hoy_madrid()
    hasta = hoy + timedelta(days=VENTANA_DIAS)
    entradas = obtener_bookings_rango_beds24(hoy.isoformat(), hasta.isoformat(), tipo="checkin")
    if not entradas:
        return []

    # Una reserva de varias habitaciones llega a Beds24 como una reserva por habitación (mismo huésped). Se avisa UNA vez por
    # reserva, en cuanto se completa el parte de cualquiera de sus habitaciones; el enlace personal lleva a todas.
    avisados = _leer_avisados()
    grupos = {}
    for e in entradas:
        grupos.setdefault(clave_grupo_entrada(e), []).append(e)
    candidatos = []
    for clave, miembros in grupos.items():
        ids = {str(m.get("book_id")) for m in miembros}
        if not clave or clave in avisados or ids & avisados:
            continue
        if not any((m.get("room_id"), m.get("arrival")) in completados for m in miembros):
            continue
        e = next((m for m in miembros if str(m.get("book_id")) == clave), miembros[0])   # la principal si está
        book_id, room_id, arrival = str(e.get("book_id")), e.get("room_id"), e.get("arrival")
        email = e.get("email")
        if not email:
            logger.warning(f"[registro_completado_avisos] Reserva {book_id} sin email — no se puede avisar")
            continue
        if es_email_relay_ota(email):
            # Booking/Airbnb rechazan el correo (ver services/email_send.py): no se envía
            # ni se marca como avisado, por si más adelante se avisa por su mensajería.
            continue

        nombre = e.get("huesped") or "Huésped"
        nombre_propiedad = _nombre_propiedad(room_id)
        idioma = _idioma_email(e.get("country"))
        n = len(miembros)
        if idioma == "es":
            subject = f"Registro completado — {nombre_propiedad}: vuelva a la web para sus códigos"
            body = _cuerpo_email_es(nombre, nombre_propiedad, url_checkin(book_id, idioma), n)
        else:
            subject = f"Registration complete — {nombre_propiedad}: return to our website for your codes"
            body = _cuerpo_email_en(nombre, nombre_propiedad, url_checkin(book_id, idioma), n)

        candidatos.append({
            "book_id": book_id, "room_id": room_id, "arrival": arrival, "grupo": clave, "book_ids": sorted(ids), "habitaciones": n,
            "email": email, "huesped": nombre, "nombre_propiedad": nombre_propiedad,
            "idioma": idioma, "subject": subject, "body": body,
        })
    return candidatos


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
    for c in _candidatos_registro_completado():
        try:
            enviar_email(to=c["email"], subject=c["subject"], body=c["body"])
            _marcar_avisado(c["grupo"], *c["book_ids"])
        except Exception as ex:
            logger.error(f"[registro_completado_avisos] Error enviando email a {c['email']} (reserva {c['book_id']}): {ex}")

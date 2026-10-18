"""
services/sin_mensajeria_avisos.py — Aviso por WhatsApp de reservas cuyo huésped NO puede recibir mensajes por la
plataforma, para que se le contacte a mano (o se prepare la llegada sin enlace de check-in).

Caso detectado (2026-10-17): reservas de Expedia que llegan por su red de afiliados / distribución B2B
(Beds24 las trae con referer «Expedia Affiliate Network»). En Partner Central, esas reservas muestran «Esta
reserva se hizo en un sitio web en el que no se pueden enviar mensajes»: el huésped no recibe ni los mensajes
programados de Beds24 ni el enlace de check-in. Beds24 solo da un alias @m.expediapartnercentral.com y un teléfono
ficticio, así que el aviso lista lo que haya para que se pueda localizar al huésped por otra vía.

Se comprueban las llegadas de hoy a VENTANA_DIAS días por delante (2 llamadas a Beds24, como
registro_completado_avisos). Dedupe por book_id en Firestore (system_state/sin_mensajeria_avisos), para siempre.
"""
import logging
from datetime import timedelta

import config
from services.beds24 import obtener_bookings_rango_beds24
from services.fechas import hoy_madrid
from services.whatsapp import alerta

logger = logging.getLogger(__name__)

VENTANA_DIAS = 14

# Fragmentos (en minúsculas) del campo `referer` de Beds24 que indican que la plataforma no admite mensajes.
REFERERS_SIN_MENSAJES = ("affiliate network", "b2b")


def _sin_mensajeria(e):
    """Motivo (str) si el huésped no puede recibir mensajes por la plataforma; None si sí puede."""
    referer = (e.get("referer") or "").strip().lower()
    if any(f in referer for f in REFERERS_SIN_MENSAJES):
        return f"reserva por «{e.get('referer')}»: la plataforma no admite mensajes"
    return None


def _telefono_legible(*valores):
    """Teléfono(s) con dígitos reales; los ficticios de las OTA («11  111111111») se indican como tales."""
    reales, ficticios = [], False
    for v in valores:
        v = (v or "").strip()
        digitos = "".join(c for c in v if c.isdigit())
        if not digitos:
            continue
        if len(set(digitos)) <= 2 and len(digitos) >= 8:
            ficticios = True
        elif v not in reales:
            reales.append(v)
    if reales:
        return ", ".join(reales)
    return "no facilitado (la plataforma da uno ficticio)" if ficticios else "no facilitado"


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("sin_mensajeria_avisos")


def _leer_avisados():
    """book_id (str) ya avisados, o None si Firestore no está disponible (para no avisar en bucle)."""
    ref = _doc_ref()
    if ref is None:
        return None
    try:
        doc = ref.get()
        return set(str(b) for b in ((doc.to_dict() or {}).get("book_ids", []) if doc.exists else []))
    except Exception as ex:
        logger.error(f"[sin_mensajeria] Error leyendo estado en Firestore: {ex}")
        return None


def _marcar_avisado(avisados, book_id):
    try:
        _doc_ref().set({"book_ids": sorted(avisados | {str(book_id)})})
    except Exception as ex:
        logger.error(f"[sin_mensajeria] Error guardando estado en Firestore: {ex}")


def texto_aviso(e, motivo):
    n = e.get("nombre_huesped") or {}
    nombre = f"{n.get('nombre', '')} {n.get('apellidos', '')}".strip() or e.get("huesped") or "sin nombre"
    plataforma = e.get("canal") or "Desconocido"
    if e.get("referer") and e.get("referer") != plataforma:
        plataforma += f" ({e.get('referer')})"
    return (
        f"Huésped: {nombre}\n"
        f"Plataforma: {plataforma}\n"
        f"Nº de reserva: {e.get('api_reference') or '—'} (Beds24: {e.get('book_id')})\n"
        f"Teléfono: {_telefono_legible(e.get('telefono'), e.get('movil'))}\n"
        f"Email: {e.get('email') or 'no facilitado'}\n"
        f"Habitación: {e.get('nombre_habitacion', '?')}\n"
        f"Llegada: {e.get('arrival')} · Salida: {e.get('departure')}\n\n"
        f"Motivo: {motivo}. No recibirá el enlace de check-in ni los mensajes automáticos: hay que contactarle por otra vía."
    )


def avisar_reservas_sin_mensajeria():
    """Pensada para /watchdog (cada 15 min). No lanza excepción: cualquier fallo se loguea."""
    hoy = hoy_madrid()
    entradas = obtener_bookings_rango_beds24(hoy.isoformat(), (hoy + timedelta(days=VENTANA_DIAS)).isoformat(), tipo="checkin")
    candidatas = [(e, m) for e in entradas for m in [_sin_mensajeria(e)] if m]
    if not candidatas:
        return
    avisados = _leer_avisados()
    if avisados is None:
        return
    for e, motivo in candidatas:
        book_id = str(e.get("book_id"))
        if book_id in avisados:
            continue
        alerta("⚠️ Huésped sin mensajería de plataforma", texto_aviso(e, motivo), nivel="warning")
        _marcar_avisado(avisados, book_id)
        avisados.add(book_id)

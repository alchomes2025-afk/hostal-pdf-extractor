"""
services/rpv_sin_importar.py — Aviso de WhatsApp cuando una reserva de Beds24 lleva
demasiado tiempo sin aparecer en RPV.

RPV importa las reservas de Beds24 con cierto retraso (integración nativa "b24",
frecuencia por confirmar; antes, el iCal una vez al día), así que que una reserva
recién creada no conste todavía es lo normal y NO se avisa. Se avisa cuando pasan
HORAS_NORMALES_SIN_RPV (services/resumen.py) desde que se creó o se modificó la
reserva y RPV sigue sin conocerla: entonces algo falla (iCal caído, integración
parada, reserva que RPV no ha entendido) y el huésped no podría hacer su parte.

Solo se comprueban las entradas de hoy a hoy+VENTANA_DIAS-1 (la ventana que
devuelve RPV) de las habitaciones que tienen cuenta de RPV. Si RPV no responde
para una habitación NO se avisa (no se sabe si consta o no; eso ya lo cubre el
watchdog). Se avisa una sola vez por reserva: dedupe por book_id en Firestore
(system_state/rpv_sin_importar), podado por fecha de llegada.
"""
import logging
from datetime import datetime, timedelta, timezone

import config
from services.beds24 import obtener_bookings_rango_beds24
from services.fechas import hoy_madrid
from services.resumen import HORAS_NORMALES_SIN_RPV, _hace
from services.rpv import TTL_SEGUNDO_PLANO, VENTANA_DIAS, habitaciones_cubiertas, obtener_estado_partes
from services.whatsapp import enviar_whatsapp_callmebot

logger = logging.getLogger(__name__)


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("rpv_sin_importar")


def _leer_avisados():
    """{book_id (str): fecha de llegada ISO} de las reservas ya avisadas. None si
    Firestore no está disponible o falla (sin dedupe se repetiría el aviso cada 15 min)."""
    ref = _doc_ref()
    if ref is None:
        return None
    try:
        doc = ref.get()
        return {str(k): v for k, v in ((doc.to_dict() or {}).get("avisados") or {}).items()} if doc.exists else {}
    except Exception as e:
        logger.error(f"[rpv_sin_importar] Error leyendo estado en Firestore: {e}")
        return None


def _guardar_avisados(avisados, hoy_iso):
    ref = _doc_ref()
    try:
        ref.set({"avisados": {k: v for k, v in avisados.items() if v >= hoy_iso}})
        return True
    except Exception as e:
        logger.error(f"[rpv_sin_importar] Error guardando estado en Firestore: {e}")
        return False


def _horas_sin_cambios(entrada, ahora_utc):
    """Horas desde la última vez que la reserva se creó o modificó (la más reciente de
    las dos); None si Beds24 no da ninguna fecha legible."""
    horas = [h for h in (_hace(entrada.get("creada"), ahora_utc), _hace(entrada.get("modificada"), ahora_utc)) if h is not None]
    return min(horas) if horas else None


def candidatas_sin_importar(ahora_utc=None):
    """Reservas que RPV debería conocer ya y no conoce, sin enviar nada ni tocar Firestore:
    lista de dicts {book_id, nombre_habitacion, huesped, canal, arrival, horas}. Separada del
    envío para poder verla desde el endpoint de diagnóstico."""
    ahora_utc = ahora_utc or datetime.now(timezone.utc)
    estados, sin_verificar = obtener_estado_partes(max_age=TTL_SEGUNDO_PLANO)
    cubiertas = habitaciones_cubiertas() - sin_verificar
    if not cubiertas:
        return []
    hoy = hoy_madrid()
    entradas = obtener_bookings_rango_beds24(hoy.isoformat(), (hoy + timedelta(days=VENTANA_DIAS - 1)).isoformat(), tipo="checkin")
    candidatas = []
    for e in entradas:
        if e["room_id"] not in cubiertas or (e["room_id"], e["arrival"]) in estados:
            continue
        if str(e.get("status") or "").lower() == "black":   # bloqueo de fechas, no es una reserva
            continue
        horas = _horas_sin_cambios(e, ahora_utc)
        if horas is None or horas < HORAS_NORMALES_SIN_RPV:
            continue
        candidatas.append({"book_id": str(e.get("book_id")), "nombre_habitacion": e["nombre_habitacion"],
                           "huesped": e.get("huesped", "?"), "canal": e.get("canal", "Desconocido"),
                           "arrival": e["arrival"], "horas": horas})
    return sorted(candidatas, key=lambda c: (c["arrival"], c["nombre_habitacion"]))


def _antiguedad(horas):
    return f"{horas} h" if horas < 48 else f"{horas // 24} días"


def mensaje_aviso(nuevas):
    plural = len(nuevas) > 1
    lineas = [f"⚠️ ALCHOMES — {'Reservas' if plural else 'Reserva'} que RPV no ha importado", ""]
    for c in nuevas:
        llegada = datetime.fromisoformat(c["arrival"]).strftime("%d/%m")
        lineas.append(f"• {c['nombre_habitacion']} · {c['huesped']} · {c['canal']} · llega el {llegada} · sin cambios desde hace {_antiguedad(c['horas'])}")
    lineas += ["",
               f"{'No constan' if plural else 'No consta'} en RPV pasadas más de {HORAS_NORMALES_SIN_RPV} h, lo normal es que ya estén. "
               f"Mientras no estén, {'esos huéspedes no pueden' if plural else 'ese huésped no puede'} hacer el parte.",
               "Qué hacer: mirar si hay otro aviso del watchdog (iCal o RPV caído); si no, revisar la integración en RPV o crear la reserva a mano allí."]
    return "\n".join(lineas)


def avisar_reservas_sin_importar():
    """Avisa por WhatsApp (un solo mensaje) de las reservas que RPV sigue sin conocer pasado
    el tiempo normal, una vez por reserva. Para llamarse desde /watchdog; no lanza excepción
    hacia arriba."""
    avisados = _leer_avisados()
    if avisados is None:
        return
    candidatas = candidatas_sin_importar()
    nuevas = [c for c in candidatas if c["book_id"] not in avisados]
    if not nuevas:
        return
    try:
        enviar_whatsapp_callmebot(mensaje_aviso(nuevas))
    except Exception as e:
        logger.error(f"[rpv_sin_importar] No se pudo enviar el aviso, se reintenta en el próximo watchdog: {e}")
        return
    avisados.update({c["book_id"]: c["arrival"] for c in nuevas})
    _guardar_avisados(avisados, hoy_madrid().isoformat())

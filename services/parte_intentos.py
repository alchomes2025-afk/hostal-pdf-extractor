"""
services/parte_intentos.py — Qué aviso ve en la web de check-in un huésped que quiere rellenar el parte de
viajeros cuando su reserva todavía no está en RPV.

Si RPV no conoce la reserva, el huésped no puede hacer el parte. En vez de mandarle al enlace de RPV sin
más, la web le dice qué hacer, con estas reglas (decididas por Adrián el 2026-10-08, a la espera de medir
cuánto tarda RPV en recibir las reservas):
  - Si llega hoy y ya son las 15:00 o más: «llame al teléfono de atención al cliente» (podría estar entrando).
  - Si no: «vuelva a intentarlo pasados unos minutos»; y si entre el primer intento en que vio ese mensaje
    y uno posterior ha pasado más de 1 hora: «llame al teléfono de atención al cliente».
Solo se avisa si el huésped puede hacer el parte ya (llega hoy o mañana: RPV lo permite desde el día anterior).
Si RPV no responde no se sabe si la reserva consta, así que no se dice nada.

El primer intento de cada reserva se guarda en Firestore (system_state/parte_intentos) y en memoria (el
backend corre con un solo worker); si Firestore falla, solo en memoria. Se olvida cuando la reserva aparece en RPV.
"""
import logging
from datetime import datetime, time as dtime, timedelta, timezone

import config
from services.rpv import reserva_en_rpv

logger = logging.getLogger(__name__)

HORA_CHECKIN = dtime(15, 0)
ESPERA_MAX = timedelta(hours=1)
CONSERVAR = timedelta(days=3)

_intentos = None   # {book_id: ISO UTC del primer intento}, cargado de Firestore la primera vez


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("parte_intentos")


def _cargar():
    global _intentos
    if _intentos is None:
        _intentos = {}
        ref = _doc_ref()
        if ref is not None:
            try:
                doc = ref.get()
                _intentos = {str(k): v for k, v in ((doc.to_dict() or {}).get("intentos") or {}).items()} if doc.exists else {}
            except Exception as e:
                logger.error(f"[parte_intentos] Error leyendo Firestore: {e}")
    return _intentos


def _guardar(ahora_utc):
    intentos = _cargar()
    for book_id, iso in list(intentos.items()):   # los muy antiguos ya no sirven
        try:
            if ahora_utc - datetime.fromisoformat(iso) > CONSERVAR:
                del intentos[book_id]
        except ValueError:
            del intentos[book_id]
    ref = _doc_ref()
    if ref is None:
        return
    try:
        ref.set({"intentos": intentos})
    except Exception as e:
        logger.error(f"[parte_intentos] Error guardando en Firestore: {e}")


def _primer_intento(book_id, ahora_utc):
    """Hora (UTC) del primer intento de esa reserva; si no hay (o el dato guardado no vale), es ahora."""
    intentos = _cargar()
    try:
        primero = datetime.fromisoformat(intentos[book_id])
        return primero if primero.tzinfo else primero.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        intentos[book_id] = ahora_utc.isoformat()
        _guardar(ahora_utc)
        return ahora_utc


def _olvidar(book_id, ahora_utc):
    if _cargar().pop(book_id, None) is not None:
        _guardar(ahora_utc)


def aviso_parte_no_disponible(book_id, room_id, arrival_iso, ahora_madrid):
    """
    None (nada que avisar), "reintentar" (vuelva a intentarlo pasados unos minutos) o "llamar" (llame al teléfono
    de atención al cliente) para un huésped cuyo parte está pendiente. `ahora_madrid` es un datetime con zona.
    """
    en_rpv = reserva_en_rpv(room_id, arrival_iso)
    ahora_utc = ahora_madrid.astimezone(timezone.utc)
    book_id = str(book_id)
    if en_rpv is True:
        _olvidar(book_id, ahora_utc)
    if en_rpv is not False:
        return None
    dias_para_llegar = (datetime.fromisoformat(arrival_iso).date() - ahora_madrid.date()).days
    if dias_para_llegar > 1:   # aún no puede hacer el parte: no hay nada que decirle
        return None
    if dias_para_llegar == 0 and ahora_madrid.time().replace(tzinfo=None) >= HORA_CHECKIN:
        return "llamar"
    return "llamar" if ahora_utc - _primer_intento(book_id, ahora_utc) > ESPERA_MAX else "reintentar"

"""
services/rpv.py — Consultas a la API de registroparteviajeros.com para
verificar si el parte de viajero de una reserva ya fue completado.

Todas las llamadas a RPV pasan por obtener_registros(), con una copia en
memoria por propiedad (el backend corre con 1 worker de gunicorn, así que es
compartida por todas las peticiones). Motivo: RPV respondió 429 (demasiadas
peticiones) en oct 2026, cuando el watchdog le hacía 12 llamadas en ráfaga
cada 15 min (chequeo de salud + aviso de registro completado) más las de la
web de check-in. Con la copia:
  - Las tareas de segundo plano (watchdog, resúmenes, registro completado)
    aceptan datos de hasta TTL_SEGUNDO_PLANO → RPV se consulta como mucho
    cada ~30 min desde el watchdog.
  - La web de check-in acepta el dato en copia solo si dice "parte
    recibido" (un parte enviado no desaparece); si no, vuelve a preguntar,
    como mucho una vez por minuto y propiedad.
  - Si una llamada falla, se usa la última copia buena aunque sea antigua,
    para no dejar a un huésped sin sus códigos por un fallo puntual de RPV.
  - Tras un 429 no se llama a RPV durante PAUSA_TRAS_429 (o lo que diga su
    cabecera Retry-After), para no alargar el bloqueo.
"""
import logging
import time

import requests

from config import RPV_API_KEY, RPV_API_URL, RPV_PROPERTY_MAP, RPV_API_KEY_MAP

logger = logging.getLogger(__name__)

TTL_SEGUNDO_PLANO = 25 * 60
TTL_CHECKIN = 60
PAUSA_TRAS_429 = 10 * 60

# RPV confirmó el 2026-10-05 que el límite es de 10 peticiones por minuto (y
# que lo ampliarían si hace falta). Nos quedamos por debajo: si ya hemos hecho
# LLAMADAS_POR_MINUTO en el último minuto, no se llama y se usa la última copia.
LLAMADAS_POR_MINUTO = 8

_copias = {}              # prop_id -> (epoch de la última llamada buena, registros)
_pausa_hasta = 0.0        # epoch hasta el que no se llama a RPV tras un 429
_llamadas_recientes = []  # epoch de las últimas llamadas reales a RPV


class _LimiteLocal(Exception):
    pass


def _pedir_a_rpv(prop_id, key):
    """Una llamada real a RPV. Devuelve la lista de registros o lanza una
    excepción con un mensaje legible (los textos de 429 empiezan por "429"
    para que el watchdog los distinga)."""
    global _pausa_hasta
    ahora = time.time()
    if ahora < _pausa_hasta:
        raise Exception("429 — en pausa tras límite de peticiones de RPV")
    _llamadas_recientes[:] = [t for t in _llamadas_recientes if ahora - t < 60]
    if len(_llamadas_recientes) >= LLAMADAS_POR_MINUTO:
        raise _LimiteLocal("429 — límite local de peticiones a RPV (máx. 10/min); se reintenta en un minuto")
    _llamadas_recientes.append(ahora)
    resp = requests.get(
        RPV_API_URL,
        headers={"Authorization": f"Bearer {key}", "accept": "application/json"},
        params={"propiedad": prop_id},
        timeout=10,
    )
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "")
        segundos = int(retry_after) if retry_after.isdigit() else PAUSA_TRAS_429
        _pausa_hasta = time.time() + segundos
        raise Exception(f"429 — límite de peticiones de RPV (pausa de {segundos // 60} min)")
    if resp.status_code == 401:
        raise Exception("API key inválida (401)")
    if resp.status_code == 403:
        raise Exception("Acceso denegado (403) — revisa que la API key usada tenga permiso sobre esta propiedad")
    if resp.status_code == 404:
        raise Exception(f"Propiedad no encontrada: {prop_id}")
    resp.raise_for_status()
    data = resp.json()
    # La API puede devolver un dict único o una lista
    return data if isinstance(data, list) else [data]


def obtener_registros(room_id, max_age):
    """
    Registros de RPV de la propiedad de esta habitación. Cada registro:
      { "reserva": { "fecha_entrada": "YYYY-MM-DD", ... }, "huespedes": {...} }

    max_age: segundos de antigüedad aceptables de la copia en memoria (0 =
    preguntar siempre a RPV).

    Devuelve (registros, error): error es None si los datos son buenos y
    recientes, o el texto del fallo si la llamada falló — en ese caso
    registros es la última copia buena (o [] si no la hay).
    """
    prop_id = RPV_PROPERTY_MAP.get(room_id)
    key = RPV_API_KEY_MAP.get(room_id) or RPV_API_KEY
    if not prop_id or not key:
        return [], "sin prop_id o API key de RPV para esta habitación"

    copia = _copias.get(prop_id)
    if copia and time.time() - copia[0] < max_age:
        return copia[1], None
    try:
        registros = _pedir_a_rpv(prop_id, key)
    except _LimiteLocal as e:
        # No es un fallo de RPV: solo hemos frenado nosotros. Con copia (aunque
        # sea más vieja que max_age) se usa tal cual y sin error; sin copia, se
        # devuelve el motivo.
        if copia:
            return copia[1], None
        return [], str(e)
    except Exception as e:
        logger.error(f"[RPV] Error consultando {prop_id} (room {room_id}): {e}")
        return (copia[1] if copia else []), str(e)
    _copias[prop_id] = (time.time(), registros)
    return registros, None


def _tiene_parte(registros, fecha_entrada_iso):
    return any((r.get("reserva") or {}).get("fecha_entrada", "") == fecha_entrada_iso for r in registros)


def parte_recibido_para(room_id, fecha_entrada_iso):
    """
    True si el parte de viajero de esta habitación y fecha de entrada ya
    está en RPV. Lo usa la web de check-in, así que un "no" se vuelve a
    comprobar contra RPV (como mucho una vez por minuto), mientras que un
    "sí" en la copia se da por bueno sin llamar.

    Usa la cuenta de RPV correcta para esa habitación (RPV_API_KEY_MAP),
    ya que algunas propiedades (ej. La Casa de la Primavera) tienen su
    propia cuenta de RPV, distinta de la del hostal.
    """
    if room_id not in RPV_PROPERTY_MAP:
        logger.warning(f"[check-in] room_id {room_id} no tiene prop_id en RPV_PROPERTY_MAP")
        return False

    registros, _ = obtener_registros(room_id, max_age=TTL_SEGUNDO_PLANO)
    if not _tiene_parte(registros, fecha_entrada_iso):
        registros, _ = obtener_registros(room_id, max_age=TTL_CHECKIN)

    if _tiene_parte(registros, fecha_entrada_iso):
        logger.info(f"[check-in] Parte RECIBIDO vía RPV API: room={room_id} fecha={fecha_entrada_iso}")
        return True
    logger.info(f"[check-in] Parte PENDIENTE: room={room_id} fecha={fecha_entrada_iso}")
    return False


def obtener_partes_con_estado(max_age=TTL_SEGUNDO_PLANO):
    """
    Devuelve (recibidos, sin_verificar):
      - recibidos: set de (room_id, fecha_entrada_iso) de los partes ya
        completados en RPV, de todas las habitaciones de RPV_PROPERTY_MAP.
        Incluye cualquier fecha que devuelva RPV, no solo hoy.
      - sin_verificar: set de room_id cuya consulta a RPV ha fallado (429,
        caída...). Para esas habitaciones "no consta parte" NO significa
        "pendiente": puede haberse enviado y no verse. Un parte que sí está
        en `recibidos` sigue siendo válido aunque el dato sea algo antiguo.
    """
    recibidos, sin_verificar = set(), set()
    if not RPV_API_KEY:
        logger.warning("[resumen] RPV_API_KEY no configurada — no se pueden verificar partes")
        return recibidos, set(RPV_PROPERTY_MAP)

    for room_id in RPV_PROPERTY_MAP:
        registros, error = obtener_registros(room_id, max_age=max_age)
        if error is not None:
            sin_verificar.add(room_id)
        for reg in registros:
            fecha = (reg.get("reserva") or {}).get("fecha_entrada", "")
            if fecha:
                recibidos.add((room_id, fecha))
    return recibidos, sin_verificar


def obtener_partes_recibidos_hoy(max_age=TTL_SEGUNDO_PLANO):
    """Solo el set de partes recibidos — ver obtener_partes_con_estado."""
    return obtener_partes_con_estado(max_age)[0]

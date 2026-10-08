"""
services/rpv.py — Estado del parte de viajeros de cada reserva, consultando el
endpoint GET /api/v1/partes de registroparteviajeros.com.

Ese endpoint (disponible desde 2026-10-07) devuelve, por rango de fechas de
ENTRADA y sin datos personales, si el parte de cada reserva está pendiente,
parcial, programado o comunicado — incluidas las entradas futuras. Sustituye a
GET /api/v1/usuarios, que solo devolvía los huéspedes con entrada de HOY y por
eso no veía los partes enviados con antelación.

Diseño:
  - Una llamada por CUENTA de RPV (hostal y La Casa de la Primavera tienen cada
    una su API key), sin el parámetro `propiedad`: devuelve todas las
    propiedades de la cuenta. Ventana de hoy a hoy+VENTANA_DIAS-1 (la API
    admite 31 días como máximo).
  - Copia en memoria por cuenta (el backend corre con 1 worker de gunicorn, así
    que la comparten todas las peticiones): RPV recomienda no consultar más de
    cada 5–15 min. La web de check-in acepta como bueno un "parte completado" en
    copia (un parte completado no se deshace salvo que el propietario aumente
    los huéspedes previstos) y, si no consta, vuelve a preguntar como mucho una
    vez por minuto.
  - Si una llamada falla se usa la última copia buena, para no dejar a un
    huésped sin sus códigos por un fallo puntual de RPV.
  - Límites de RPV: 20 peticiones/min POR API KEY. Nos quedamos en
    LLAMADAS_POR_MINUTO por cuenta; si hay un 429 se respeta Retry-After.
  - La API key no se escribe nunca en logs ni en mensajes de error.
"""
import logging
import time
from datetime import timedelta

import requests

from config import RPV_API_KEY, RPV_API_KEY_MAP, RPV_PARTES_URL, RPV_PROPERTY_MAP, ROOM_CONFIG
from services.fechas import hoy_madrid

logger = logging.getLogger(__name__)

TTL_SEGUNDO_PLANO = 10 * 60
TTL_CHECKIN = 60
VENTANA_DIAS = 30            # hoy .. hoy+29 (RPV admite 31 días por petición)
PAUSA_TRAS_429 = 2 * 60      # si el 429 no trae Retry-After
LLAMADAS_POR_MINUTO = 16     # por cuenta; el límite real de RPV es 20

_copias = {}         # api_key -> {"t": epoch de la última llamada buena, "partes": [...]}
_pausa_hasta = {}    # api_key -> epoch hasta el que no se llama tras un 429
_llamadas = {}       # api_key -> [epoch de las últimas llamadas reales]

# Para elegir entre varios partes de la misma reserva (completado manda; luego estado).
_PRIORIDAD_ESTADO = {"comunicado": 4, "programado": 3, "parcial": 2, "pendiente": 1}


class _LimiteLocal(Exception):
    pass


def _cuentas():
    """{api_key: [room_id, ...]} — una API key es una cuenta de RPV."""
    cuentas = {}
    for room_id in RPV_PROPERTY_MAP:
        key = RPV_API_KEY_MAP.get(room_id) or RPV_API_KEY
        if key:
            cuentas.setdefault(key, []).append(room_id)
    return cuentas


def habitaciones_cubiertas():
    """Habitaciones que tienen cuenta de RPV (con API key): las únicas cuyas reservas
    debe conocer RPV."""
    return {r for rooms in _cuentas().values() for r in rooms}


def _etiqueta(rooms):
    nombres = [ROOM_CONFIG.get(r, {}).get("nombre", r) for r in rooms]
    return nombres[0] if len(nombres) == 1 else f"{nombres[0]} y {len(nombres) - 1} más"


def _pedir_partes(key, url=None):
    """Una llamada real a /partes de esa cuenta. Devuelve la lista `partes` o lanza
    una excepción con un mensaje legible (los de 429 empiezan por "429" para que el
    watchdog los distinga de un fallo real)."""
    ahora = time.time()
    if ahora < _pausa_hasta.get(key, 0.0):
        raise Exception("429 — en pausa tras límite de peticiones de RPV")
    recientes = [t for t in _llamadas.get(key, []) if ahora - t < 60]
    if len(recientes) >= LLAMADAS_POR_MINUTO:
        _llamadas[key] = recientes
        raise _LimiteLocal("429 — límite local de peticiones a RPV (máx. 20/min por cuenta); se reintenta en un minuto")
    recientes.append(ahora)
    _llamadas[key] = recientes

    hoy = hoy_madrid()
    resp = requests.get(
        url or RPV_PARTES_URL,
        headers={"Authorization": f"Bearer {key}", "accept": "application/json"},
        params={"desde": hoy.isoformat(), "hasta": (hoy + timedelta(days=VENTANA_DIAS - 1)).isoformat()},
        timeout=10,
    )
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "")
        segundos = int(retry_after) if retry_after.isdigit() else PAUSA_TRAS_429
        _pausa_hasta[key] = time.time() + segundos
        raise Exception(f"429 — límite de peticiones de RPV (pausa de {max(1, segundos // 60)} min)")
    if resp.status_code == 401:
        raise Exception("API key inválida (401)")
    if resp.status_code == 403:
        raise Exception("Acceso denegado (403) — alguna propiedad no existe o no es de esta cuenta")
    if resp.status_code == 404:
        raise Exception("Endpoint de partes no encontrado (404)")
    if resp.status_code == 400:
        try:
            motivo = str(resp.json().get("message", ""))[:120]
        except ValueError:
            motivo = ""
        raise Exception(f"RPV rechazó la petición (400): {motivo}")
    resp.raise_for_status()
    data = resp.json()
    partes = data.get("partes") if isinstance(data, dict) else None
    if not isinstance(partes, list):
        raise Exception("respuesta inesperada de RPV (sin lista 'partes')")
    return partes


def _partes_cuenta(key, max_age, etiqueta):
    """(partes, error) de una cuenta. error es None si los datos son buenos y
    recientes; si no, el texto del fallo, y `partes` es la última copia buena (o [])."""
    copia = _copias.get(key)
    if copia and time.time() - copia["t"] < max_age:
        return copia["partes"], None
    try:
        partes = _pedir_partes(key)
    except _LimiteLocal as e:
        # No es un fallo de RPV: solo hemos frenado nosotros. Con copia (aunque sea
        # más vieja que max_age) se usa tal cual y sin error; sin copia, el motivo.
        if copia:
            return copia["partes"], None
        return [], str(e)
    except Exception as e:
        logger.error(f"[RPV] Error consultando la cuenta {etiqueta}: {e}")
        return (copia["partes"] if copia else []), str(e)
    _copias[key] = {"t": time.time(), "partes": partes}
    return partes, None


def _rango(item):
    return (10 if item["completado"] else 0) + _PRIORIDAD_ESTADO.get(item["estado"], 0)


def _normalizar(partes):
    """{(room_id, fecha_entrada): {estado, completado, comunicado, registrados, previstos}}.
    Las propiedades que no están en RPV_PROPERTY_MAP se ignoran; si hay varios partes
    para la misma habitación y fecha de entrada, manda el más avanzado."""
    prop_a_room = {prop: room for room, prop in RPV_PROPERTY_MAP.items()}
    estados = {}
    for p in partes:
        room = prop_a_room.get(p.get("propiedad_id"))
        fecha = p.get("fecha_entrada")
        if not room or not fecha:
            continue
        item = {
            "estado": p.get("estado") or "pendiente",
            "completado": bool(p.get("parte_completado")),
            "comunicado": bool(p.get("comunicado_autoridades")),
            "registrados": p.get("huespedes_registrados"),
            "previstos": p.get("huespedes_previstos"),
        }
        previo = estados.get((room, fecha))
        if previo is None or _rango(item) > _rango(previo):
            estados[(room, fecha)] = item
    return estados


def obtener_estado_partes(max_age=TTL_SEGUNDO_PLANO, rooms=None):
    """
    Devuelve (estados, sin_verificar):
      - estados: {(room_id, fecha_entrada_iso): {"estado", "completado", "comunicado",
        "registrados", "previstos"}} de las reservas que RPV conoce, de hoy a +29 días.
      - sin_verificar: set de room_id cuya cuenta no se ha podido consultar (429,
        caída, sin API key…). Para esas habitaciones "no consta" NO significa
        "pendiente"; un parte completado que sí está en `estados` sigue siendo válido
        aunque el dato sea algo antiguo.

    max_age: antigüedad máxima aceptable de la copia en memoria, en segundos.
    rooms: si se indica, solo se consultan las cuentas de esas habitaciones.
    """
    estados, sin_verificar = {}, set()
    cuentas = _cuentas()
    con_key = {r for rs in cuentas.values() for r in rs}
    sin_verificar.update(r for r in RPV_PROPERTY_MAP if r not in con_key)
    for key, cuarto in cuentas.items():
        if rooms is not None and not (set(rooms) & set(cuarto)):
            continue
        partes, error = _partes_cuenta(key, max_age, _etiqueta(cuarto))
        if error is not None:
            sin_verificar.update(cuarto)
        estados.update(_normalizar(partes))
    return estados, sin_verificar


def parte_recibido_para(room_id, fecha_entrada_iso):
    """
    True si el parte de viajeros de esta habitación y fecha de entrada está
    completado en RPV (todos los huéspedes). Lo usa la web de check-in: un "sí" en la
    copia se da por bueno sin llamar; un "no" se vuelve a comprobar contra RPV (como
    mucho una vez por minuto). Funciona también para entradas futuras: el huésped
    puede hacer el parte desde el día anterior.
    """
    if room_id not in RPV_PROPERTY_MAP:
        logger.warning(f"[check-in] room_id {room_id} no tiene prop_id en RPV_PROPERTY_MAP")
        return False

    for max_age in (TTL_SEGUNDO_PLANO, TTL_CHECKIN):
        estados, _ = obtener_estado_partes(max_age, rooms=[room_id])
        if (estados.get((room_id, fecha_entrada_iso)) or {}).get("completado"):
            logger.info(f"[check-in] Parte COMPLETADO vía RPV: room={room_id} fecha={fecha_entrada_iso}")
            return True
    logger.info(f"[check-in] Parte pendiente: room={room_id} fecha={fecha_entrada_iso}")
    return False


def reserva_en_rpv(room_id, fecha_entrada_iso):
    """
    ¿Conoce RPV la reserva de esta habitación con esta fecha de entrada (aunque su parte siga pendiente)?
    True si consta; False si RPV responde y no la tiene (aún no la ha importado); None si no se puede saber
    (habitación sin cuenta de RPV o RPV no responde). Como parte_recibido_para: un «sí» en la copia se da por
    bueno y un «no» se vuelve a comprobar contra RPV como mucho una vez por minuto.
    """
    if room_id not in RPV_PROPERTY_MAP:
        return None
    for max_age in (TTL_SEGUNDO_PLANO, TTL_CHECKIN):
        estados, sin_verificar = obtener_estado_partes(max_age, rooms=[room_id])
        if (room_id, fecha_entrada_iso) in estados:
            return True
        if room_id in sin_verificar:
            return None
    return False


def comprobar_cuentas(max_age=TTL_SEGUNDO_PLANO):
    """[(etiqueta, error|None)] — una entrada por cuenta de RPV, para el chequeo de
    salud del watchdog. Usa la misma copia que el resto, así que no añade llamadas."""
    return [(_etiqueta(rooms), _partes_cuenta(key, max_age, _etiqueta(rooms))[1])
            for key, rooms in _cuentas().items()]


def diagnostico(entorno="real"):
    """
    Consulta FRESCA (sin copia) a cada cuenta, en producción ("real") o en el sandbox
    ("pre", datos ficticios), y devuelve un resumen sin datos personales: lo que usa
    /diag-rpv. Cuenta para el límite de llamadas.
    """
    url = RPV_PARTES_URL if entorno == "real" else RPV_PARTES_URL.replace("/api/v1/", "/api/pre/v1/")
    nombres = {prop: ROOM_CONFIG.get(room, {}).get("nombre", room) for room, prop in RPV_PROPERTY_MAP.items()}
    resultado = []
    for key, rooms in _cuentas().items():
        cuenta = {"cuenta": _etiqueta(rooms)}
        try:
            partes = _pedir_partes(key, url=url)
        except Exception as e:
            cuenta["error"] = str(e)
            resultado.append(cuenta)
            continue
        por_estado = {}
        for p in partes:
            por_estado[p.get("estado")] = por_estado.get(p.get("estado"), 0) + 1
        cuenta.update({
            "error": None, "total": len(partes), "por_estado": por_estado,
            "propiedades_desconocidas": sorted({p.get("propiedad_id") for p in partes} - set(nombres)),
            "partes": [{
                "habitacion": nombres.get(p.get("propiedad_id"), p.get("propiedad_id")),
                "entrada": p.get("fecha_entrada"), "salida": p.get("fecha_salida"),
                "estado": p.get("estado"), "completado": p.get("parte_completado"),
                "comunicado": p.get("comunicado_autoridades"),
                "registrados": p.get("huespedes_registrados"), "previstos": p.get("huespedes_previstos"),
                "completado_en": p.get("completado_en"),
            } for p in sorted(partes, key=lambda x: (x.get("fecha_entrada") or "", x.get("propiedad_id") or ""))],
        })
        resultado.append(cuenta)
    return resultado

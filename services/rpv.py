"""
services/rpv.py — Consultas a la API de registroparteviajeros.com para
verificar si el parte de viajero de una reserva ya fue completado.
"""
import logging
import requests

from config import RPV_API_KEY, RPV_API_URL, RPV_PROPERTY_MAP

logger = logging.getLogger(__name__)


def _consultar_rpv_propiedad(prop_id):
    """
    Llama a la API de registroparteviajeros.com para una propiedad concreta.
    Devuelve la lista de registros (cada uno con reserva + huespedes) o [].

    Estructura de respuesta:
      [ { "reserva": { "fecha_entrada": "YYYY-MM-DD", "fecha_salida": "...", ... },
          "huespedes": { "huesped": [...] } }, ... ]
    """
    if not RPV_API_KEY or not prop_id:
        return []
    try:
        resp = requests.get(
            RPV_API_URL,
            headers={"Authorization": f"Bearer {RPV_API_KEY}", "accept": "application/json"},
            params={"propiedad": prop_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # La API puede devolver un dict único o una lista
        return data if isinstance(data, list) else [data]
    except Exception as e:
        logger.error(f"[RPV] Error consultando {prop_id}: {e}")
        return []


def parte_recibido_para(room_id, fecha_entrada_iso):
    """
    Verifica si el parte de viajero fue completado para esta habitación
    y fecha de entrada, consultando directamente la API de
    registroparteviajeros.com (sin depender de Gmail).

    La presencia de un registro con reserva.fecha_entrada coincidente
    es suficiente para confirmar que el huésped completó el proceso.
    """
    prop_id = RPV_PROPERTY_MAP.get(room_id)
    if not prop_id:
        logger.warning(f"[check-in] room_id {room_id} no tiene prop_id en RPV_PROPERTY_MAP")
        return False

    registros = _consultar_rpv_propiedad(prop_id)
    for reg in registros:
        reserva = reg.get("reserva", {})
        if reserva.get("fecha_entrada", "") == fecha_entrada_iso:
            logger.info(
                f"[check-in] Parte RECIBIDO vía RPV API: "
                f"room={room_id} fecha={fecha_entrada_iso}"
            )
            return True

    logger.info(f"[check-in] Parte PENDIENTE: room={room_id} fecha={fecha_entrada_iso}")
    return False


def obtener_partes_recibidos_hoy():
    """
    Devuelve un set de (room_id, fecha_entrada_iso) de los partes de viajeros
    ya completados, consultando directamente la API de registroparteviajeros.com
    para las 5 habitaciones.

    Reemplaza la versión anterior basada en Gmail (que requería
    GOOGLE_REFRESH_TOKEN y caducaba con frecuencia).
    """
    recibidos = set()
    if not RPV_API_KEY:
        logger.warning("[resumen] RPV_API_KEY no configurada — no se pueden verificar partes")
        return recibidos

    for room_id, prop_id in RPV_PROPERTY_MAP.items():
        registros = _consultar_rpv_propiedad(prop_id)
        for reg in registros:
            reserva = reg.get("reserva", {})
            fecha = reserva.get("fecha_entrada", "")
            if fecha:
                recibidos.add((room_id, fecha))
                logger.info(f"[resumen] Parte recibido: room={room_id} fecha={fecha}")

    return recibidos

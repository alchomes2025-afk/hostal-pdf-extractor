"""
services/beds24.py — Autenticación con Beds24, búsqueda de reservas y envío
del código de puerta vía Booking.com Messages.
"""
import logging
import re
import unicodedata
import requests
from datetime import date, timedelta

from config import BEDS24_REFRESH_TOKEN, BEDS24_API_BASE, BEDS24_PROPERTY_ID, BEDS24_PROPERTY_IDS, ROOM_CONFIG
from services.whatsapp import avisar_error_critico

logger = logging.getLogger(__name__)


def get_beds24_access_token():
    """Intercambia el refresh token de Beds24 por un access token válido (24h)."""
    if not BEDS24_REFRESH_TOKEN:
        raise Exception("BEDS24_REFRESH_TOKEN no configurado en Render.")
    resp = requests.get(
        f"{BEDS24_API_BASE}/authentication/token",
        headers={"refreshToken": BEDS24_REFRESH_TOKEN, "accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["token"]


# ── Helper: búsqueda recursiva del número de Booking.com ─────────────────

def _ref_en_booking(obj, ref_norm, _path=""):
    """
    Busca ref_norm recursivamente en TODOS los campos del objeto de reserva
    devuelto por Beds24.

    Beds24 almacena el número de confirmación de Booking.com en un campo
    cuyo nombre exacto varía según versión de API (externalId, guestCode,
    channelBookingId…). Esta búsqueda lo encuentra sea cual sea el campo.

    Devuelve el path del campo donde se halló (útil para el log) o None.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            r = _ref_en_booking(v, ref_norm, f"{_path}.{k}" if _path else k)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            r = _ref_en_booking(item, ref_norm, f"{_path}[{i}]")
            if r is not None:
                return r
    elif obj is not None:
        if str(obj).strip().lower() == ref_norm:
            return _path or "raíz"
    return None


# ── Función principal: buscar reserva por número de Booking.com ──────────

def buscar_booking_por_ref(booking_ref):
    """
    Busca en Beds24 la reserva cuyo número de confirmación de Booking.com
    coincida con booking_ref (el número que el huésped ve en su app/email).

    Estrategia:
      1. Obtiene TODAS las reservas con arrival en [-5 días, +180 días].
         Para 5 habitaciones, son ~30-40 objetos como máximo.
      2. Por cada reserva activa, busca booking_ref en todos sus campos
         de forma recursiva (_ref_en_booking).
      3. Loguea en qué campo exacto encontró la coincidencia.
         (Útil para saber qué campo usa tu versión de Beds24 si
         algún día necesitas optimizar la búsqueda.)

    Devuelve el dict completo de la reserva, o None si no se encuentra.
    """
    if not booking_ref or not booking_ref.strip():
        return None

    try:
        token = get_beds24_access_token()
    except Exception as e:
        logger.error(f"[check-in] Beds24 auth error en buscar_booking_por_ref: {e}")
        return None

    hoy   = date.today()
    desde = (hoy - timedelta(days=5)).isoformat()
    hasta = (hoy + timedelta(days=180)).isoformat()

    ref_norm = booking_ref.strip().lower()

    # Recorre TODAS las propiedades configuradas (hostal + La Casa de la Primavera).
    # El huésped no indica de qué propiedad es su reserva, así que hay que
    # comprobarlas todas hasta encontrar una coincidencia.
    for property_id in BEDS24_PROPERTY_IDS:
        try:
            resp = requests.get(
                f"{BEDS24_API_BASE}/bookings",
                headers={"token": token, "accept": "application/json"},
                params={"propertyId": property_id,
                        "arrivalFrom": desde, "arrivalTo": hasta},
                timeout=20,
            )
            resp.raise_for_status()
            bookings = resp.json().get("data", [])
            logger.info(f"[check-in] Beds24 property {property_id}: {len(bookings)} reservas en rango {desde}→{hasta}")
        except Exception as e:
            logger.error(f"[check-in] Error consultando Beds24 (property {property_id}): {e}")
            continue

        for b in bookings:
            if str(b.get("status", "")).lower() == "cancelled":
                continue
            campo = _ref_en_booking(b, ref_norm)
            if campo is not None:
                logger.info(
                    f"[check-in] Reserva encontrada: ref='{booking_ref}' "
                    f"en campo '{campo}' → book_id={b.get('id')} room={b.get('roomId')} property={property_id}"
                )
                return b

    logger.warning(f"[check-in] ref='{booking_ref}': no encontrado en ninguna propiedad ({BEDS24_PROPERTY_IDS}).")
    return None


def _normalizar_nombre(texto):
    """Minúsculas, sin acentos, solo letras — para comparar nombres tolerando
    mayúsculas/tildes/orden. Devuelve el conjunto de palabras (no la cadena),
    así "Juan Perez" y "Perez Juan" comparan igual."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^a-zA-Z ]", " ", t).lower()
    return set(w for w in t.split() if w)


def buscar_booking_por_nombre(nombre_query, dias_atras=2, dias_adelante=4):
    """
    Busca una reserva por el nombre del huésped, para cuando el número de
    reserva no sirve (p. ej. La Casa de la Primavera recibe reservas de
    Booking, Airbnb y Holidu, cada plataforma con su propio formato de
    referencia — no hay un único campo fiable para buscar por número).

    Solo tiene sentido como respaldo de buscar_booking_por_ref(), NUNCA como
    búsqueda principal: se restringe deliberadamente a una ventana estrecha
    de días de llegada (por defecto hoy-2 a hoy+4) porque el huésped recibe
    el enlace de check-in el día antes o el mismo día de su llegada — así se
    evita comparar contra reservas de todo el año, que sería mucho más
    propenso a coincidencias ambiguas entre huéspedes con nombres parecidos.

    Comparación tolerante a mayúsculas/acentos/orden de palabras (ver
    _normalizar_nombre), pero NO tolera erratas de escritura — para eso
    existe el escenario de Make con Groq, pensado para el canal de email,
    no para este backend.

    Devuelve (booking, ambiguo): booking es el dict de Beds24 si hay
    exactamente una coincidencia, o None si no hay ninguna o si hay más de
    una (en cuyo caso ambiguo=True, para poder distinguir "no encontrado"
    de "hacen falta más datos para desambiguar").
    """
    query_tokens = _normalizar_nombre(nombre_query)
    if not query_tokens:
        return None, False

    try:
        token = get_beds24_access_token()
    except Exception as e:
        logger.error(f"[check-in] Beds24 auth error en buscar_booking_por_nombre: {e}")
        return None, False

    hoy   = date.today()
    desde = (hoy - timedelta(days=dias_atras)).isoformat()
    hasta = (hoy + timedelta(days=dias_adelante)).isoformat()

    candidatos = []
    for property_id in BEDS24_PROPERTY_IDS:
        try:
            resp = requests.get(
                f"{BEDS24_API_BASE}/bookings",
                headers={"token": token, "accept": "application/json"},
                params={"propertyId": property_id,
                        "arrivalFrom": desde, "arrivalTo": hasta},
                timeout=20,
            )
            resp.raise_for_status()
            bookings = resp.json().get("data", [])
        except Exception as e:
            logger.error(f"[check-in] Error consultando Beds24 por nombre (property {property_id}): {e}")
            continue

        for b in bookings:
            if str(b.get("status", "")).lower() == "cancelled":
                continue
            nombre_huesped = _extraer_nombre_huesped_beds24(b)
            candidato_tokens = _normalizar_nombre(nombre_huesped)
            if not candidato_tokens:
                continue
            # Coincide si uno de los dos conjuntos de palabras contiene al otro
            # entero — tolera que el huésped escriba solo nombre+apellido de
            # una reserva con nombre completo más largo, y viceversa.
            if query_tokens <= candidato_tokens or candidato_tokens <= query_tokens:
                candidatos.append(b)

    if len(candidatos) == 1:
        b = candidatos[0]
        logger.info(
            f"[check-in] Reserva encontrada por nombre: query='{nombre_query}' "
            f"→ book_id={b.get('id')} room={b.get('roomId')}"
        )
        return b, False

    if len(candidatos) > 1:
        logger.warning(
            f"[check-in] Nombre ambiguo: query='{nombre_query}' encontró "
            f"{len(candidatos)} reservas en el rango {desde}→{hasta}."
        )
        return None, True

    logger.warning(f"[check-in] nombre='{nombre_query}': no encontrado en el rango {desde}→{hasta}.")
    return None, False


# Nombre de habitación en el formato original (el que usa registroparteviajeros.com
# y el que se mostraba antes en el resumen de WhatsApp), para no cambiar el
# formato del mensaje aunque los datos ahora vengan de Beds24.
ROOM_ID_DISPLAY_NAME = {
    "702397": "Habitación Simple 1",
    "702398": "Habitación Simple 2",
    "702399": "Habitación Simple 3",
    "702396": "Habitación Doble 4",
    "702395": "Habitación Deluxe 5",
}


def _extraer_nombre_huesped_beds24(booking):
    """Beds24 puede devolver el nombre del huésped en distintos campos según
    la versión/canal. Probamos varias claves habituales antes de rendirnos."""
    guest = booking.get("guest") or {}
    nombre = guest.get("firstName") or booking.get("firstName") or booking.get("guestFirstName") or ""
    apellido = guest.get("lastName") or booking.get("lastName") or booking.get("guestLastName") or ""
    nombre_completo = f"{nombre} {apellido}".strip()
    return nombre_completo or booking.get("guestName") or "Huésped sin nombre"


def obtener_bookings_dia_beds24(fecha_iso, tipo="checkin"):
    """
    Consulta en Beds24 las reservas con entrada (arrival) o salida (departure)
    en una fecha concreta (fecha_iso formato YYYY-MM-DD). tipo: "checkin" o "checkout".

    IMPORTANTE: los parámetros de filtro por fecha de la API (arrivalFrom/
    arrivalTo, departureFrom/departureTo) no son fiables al 100% — se detectó
    que en algunos casos Beds24 devuelve reservas de fechas no solicitadas.
    Por eso, además de pasar el filtro en la petición, SIEMPRE se vuelve a
    filtrar explícitamente en Python comparando el campo real 'arrival' o
    'departure' de cada reserva contra fecha_iso de forma exacta.

    Recorre TODAS las propiedades de BEDS24_PROPERTY_IDS (hostal + La Casa
    de la Primavera) — si solo se consultara BEDS24_PROPERTY_ID, el resumen
    diario nunca mostraría las entradas/salidas de la segunda propiedad.

    Devuelve una lista de dicts: {room_id, nombre_habitacion, huesped, book_id}.
    Si falla la consulta (token, red, etc.) devuelve lista vacía y loguea el error,
    para no romper el resumen diario por un problema puntual de Beds24.
    """
    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        logger.error(f"[CONTROL] Beds24 auth falló al consultar reservas de {tipo} ({fecha_iso}): {e}")
        return []

    campo_fecha = "arrival" if tipo == "checkin" else "departure"

    resultado = []
    for property_id in BEDS24_PROPERTY_IDS:
        params = {"propertyId": property_id}
        if tipo == "checkin":
            params["arrivalFrom"] = fecha_iso
            params["arrivalTo"] = fecha_iso
        else:
            params["departureFrom"] = fecha_iso
            params["departureTo"] = fecha_iso

        try:
            resp = requests.get(
                f"{BEDS24_API_BASE}/bookings",
                headers={"token": access_token, "accept": "application/json"},
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as e:
            logger.error(f"[CONTROL] Error consultando reservas de {tipo} para {fecha_iso} (property {property_id}): {e}")
            continue

        descartadas_por_fecha = 0
        for b in data:
            # Filtrado explícito: NO confiar en que la API ya haya filtrado bien.
            if b.get(campo_fecha) != fecha_iso:
                descartadas_por_fecha += 1
                continue
            if str(b.get("status", "")).lower() == "cancelled":
                continue
            room_id = str(b.get("roomId", ""))
            if not room_id:
                continue
            resultado.append({
                "room_id": room_id,
                "nombre_habitacion": ROOM_ID_DISPLAY_NAME.get(room_id, ROOM_CONFIG.get(room_id, {}).get("nombre", f"Room {room_id}")),
                "huesped": _extraer_nombre_huesped_beds24(b),
                "book_id": b.get("id"),
            })

        if descartadas_por_fecha:
            logger.info(
                f"obtener_bookings_dia_beds24({tipo}, {fecha_iso}, property {property_id}): la API devolvió "
                f"{len(data)} reservas, {descartadas_por_fecha} descartadas por no "
                f"coincidir la fecha exacta de '{campo_fecha}' (filtro de la API no fiable)."
            )
    return resultado

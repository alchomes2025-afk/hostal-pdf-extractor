"""
services/beds24.py — Autenticación con Beds24, búsqueda de reservas y envío
del código de puerta vía Booking.com Messages.
"""
import logging
import requests
from datetime import date, timedelta

from config import BEDS24_REFRESH_TOKEN, BEDS24_API_BASE, BEDS24_PROPERTY_ID, BEDS24_PROPERTY_IDS, ROOM_CONFIG
from services.whatsapp import avisar_error_critico
from services.rooms import detectar_room_id

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


def buscar_booking_id_beds24(access_token, room_id, fecha_entrada):
    """
    Busca la reserva en Beds24 para una habitación y fecha de entrada concretas.

    IMPORTANTE: los parámetros de filtro por fecha de la API (checkInFrom/
    checkInTo) NO funcionan de forma fiable — se comprobó que Beds24 los
    ignora y devuelve reservas de cualquier fecha. Por eso aquí SIEMPRE se
    filtra también en nuestro propio código, comparando el campo real
    'arrival' de cada reserva contra fecha_entrada de forma exacta, en vez
    de confiar en que el primer resultado (data[0]) sea el correcto.

    Devuelve el bookId de la reserva cuya fecha de 'arrival' coincide
    exactamente, o None si no encuentra ninguna coincidencia real.
    """
    resp = requests.get(
        f"{BEDS24_API_BASE}/bookings",
        headers={"token": access_token, "accept": "application/json"},
        params={
            "propertyId": BEDS24_PROPERTY_ID,
            "roomId": room_id,
            "arrivalFrom": fecha_entrada,
            "arrivalTo": fecha_entrada,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])

    # Verificación explícita: solo aceptamos una reserva cuya fecha de
    # 'arrival' coincida EXACTAMENTE con fecha_entrada, y cuyo status no
    # sea 'cancelled'. No confiamos en el orden ni en el filtro de la API.
    for b in data:
        if b.get("arrival") == fecha_entrada and str(b.get("status", "")).lower() != "cancelled":
            return b.get("id")

    logger.warning(
        f"buscar_booking_id_beds24: ninguna reserva de room {room_id} tiene "
        f"arrival exacto {fecha_entrada} (la API devolvió {len(data)} resultados "
        f"sin filtrar correctamente por fecha)"
    )
    return None


def construir_mensaje_codigo(room_id, nombre_cliente=None, version_minima=False):
    """Construye el mensaje bilingüe de bienvenida + código de puerta.
    Si version_minima=True, omite el enlace del asistente virtual y el
    teléfono — útil para diagnosticar si Booking.com está bloqueando el
    mensaje por contener enlaces/teléfonos no aprobados en su Extranet."""
    cfg = ROOM_CONFIG.get(room_id, {})
    nombre_hab = cfg.get("nombre", "")
    pin = cfg.get("pin")
    saludo_en = f"{nombre_cliente} " if nombre_cliente else ""

    bloque_contacto_es = (
        "" if version_minima else
        "\n\nPara cualquier duda o consulta, puede tener respuesta inmediata en nuestro asistente virtual: "
        "https://app-asistente-virtual-alc-homes.vercel.app/\n\n"
        "Puede comunicarse con nosotros 24h, vía mensajes dentro de la plataforma de booking, "
        "o puede contactarnos por el teléfono oficial +34 622 38 35 87 las 24 horas del día\n"
    )
    bloque_contacto_en = (
        "" if version_minima else
        "\n\nFor any questions or inquiries, you can get an immediate response from our virtual assistant: "
        "https://app-asistente-virtual-alc-homes.vercel.app/\n\n"
        "You can communicate with us 24 hours a day via messages within the booking platform or by calling "
        "our official number at +34 622 38 35 87. "
    )

    if room_id == "702397":  # Playa Lanuza — sin código actualmente
        return (
            "Bienvenido a Alc Homes Alicante.\n"
            "Nos encontrará en la Calle Camino de Ronda, 1 (Alicante). "
            "Para abrir la puerta de la calle, introduzca el código de entrada y empuje la puerta. "
            "Código de entrada: 130773# (asegúrese de marcar los seis números y la #)\n\n"
            f"Su habitación es {nombre_hab}\n"
            "No funciona el código, disculpe las molestias. La habitación estará abierta y la llave en la mesita de noche\n\n"
            "WIFI: ALCHOMES\n"
            "CONTRASEÑA: Alchomes2025"
            f"{bloque_contacto_es}"
            "Alc Homes le desea una agradable estancia.\n"
            "_________\n"
            "Welcome to Alc Homes Alicante.\n"
            "We are located at Calle Camino de Ronda, 1 (Alicante). To open the street entrance, enter the access "
            "code and push the door. Entry code: 130773# (make sure to dial the six numbers and the #)\n\n"
            f"Your room is {nombre_hab}.\n"
            "The code is not working, sorry for the inconvenience: The room will be unlocked, and the key will be on the nightstand.\n\n"
            "WIFI: ALCHOMES\n"
            "PASSWORD: Alchomes2025"
            f"{bloque_contacto_en}"
            "Alc Homes wishes you a pleasant stay."
        )

    return (
        "Bienvenido a Alc Homes Alicante.\n"
        "Nos encontrará en la Calle Camino de Ronda, 1 (Alicante). "
        "Para abrir la puerta de la calle, introduzca el código de entrada y empuje la puerta. "
        "Código de entrada: 130773# (asegúrese de marcar los seis números y la #)\n\n"
        f"Su habitación es {nombre_hab}. Su código es {pin or 'PIN NO CONFIGURADO'}. "
        "Para cerrar la puerta desde fuera, pulse el triángulo\n\n"
        "WIFI: ALCHOMES\n"
        "CONTRASEÑA: Alchomes2025"
        f"{bloque_contacto_es}"
        "Alc Homes le desea una agradable estancia.\n"
        "_________\n"
        f"{saludo_en}Welcome to Alc Homes Alicante.\n"
        "We are located at Calle Camino de Ronda, 1 (Alicante). To open the street entrance, enter the access "
        "code and push the door. Entry code: 130773# (make sure to dial the six numbers and the #)\n\n"
        f"Your room is {nombre_hab}. Your code is {pin or 'PIN NOT CONFIGURED'}. To lock the door from the outside, press the triangle.\n\n"
        "WIFI: ALCHOMES\n"
        "PASSWORD: Alchomes2025"
        f"{bloque_contacto_en}"
        "Alc Homes wishes you a pleasant stay."
    )


def enviar_codigo_por_room_id(room_id, fecha_entrada, nombre_cliente=None):
    """
    Envía el código de puerta vía Beds24/Booking.com Messages para una
    habitación y fecha conocidas, sin necesitar el PDF del parte.

    Antes de enviar, comprueba el historial de mensajes de la reserva en
    Beds24: si ya hay un mensaje nuestro (contiene el código 130773#),
    no lo reenvía para evitar duplicados.

    Devuelve dict con: enviado, ya_enviado, book_id, error.
    """
    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        avisar_error_critico("Fallo auth Beds24 (procesar-partes)", str(e))
        return {"enviado": False, "error": str(e)}

    book_id = buscar_booking_id_beds24(access_token, room_id, fecha_entrada)
    if not book_id:
        avisar_error_critico(
            "Reserva no encontrada (procesar-partes)",
            f"room={room_id} fecha={fecha_entrada}"
        )
        return {"enviado": False, "error": "reserva no encontrada en Beds24"}

    # Comprobar si ya enviamos el código (el mensaje siempre contiene "130773#")
    try:
        r = requests.get(
            f"{BEDS24_API_BASE}/bookings/messages",
            headers={"token": access_token, "accept": "application/json"},
            params={"bookingId": book_id},
            timeout=10,
        )
        mensajes = r.json().get("data", []) if r.ok else []
        if any("130773" in str(m.get("message", "")) for m in mensajes):
            logger.info(f"[procesar] Código ya enviado a booking {book_id} — omitiendo")
            return {"enviado": False, "ya_enviado": True, "book_id": book_id}
    except Exception as e:
        logger.warning(f"[procesar] No se pudo verificar historial de mensajes: {e}")

    mensaje = construir_mensaje_codigo(room_id, nombre_cliente)
    try:
        resp = requests.post(
            f"{BEDS24_API_BASE}/bookings/messages",
            headers={"token": access_token, "accept": "application/json",
                     "Content-Type": "application/json"},
            json=[{"bookingId": book_id, "message": mensaje, "sendEmail": False}],
            timeout=20,
        )
        resp.raise_for_status()
        logger.info(f"[procesar] Código enviado a booking {book_id} (room {room_id})")
        return {"enviado": True, "book_id": book_id}
    except Exception as e:
        avisar_error_critico(
            "Error enviando código (procesar-partes)",
            f"room={room_id} booking={book_id}: {e}"
        )
        return {"enviado": False, "error": str(e)}


def enviar_codigo_puerta_beds24(
    habitacion_texto, texto_completo, fecha_entrada, nombre_cliente=None, dry_run=False, version_minima=False, enviar_tambien_email=False):
    """
    Detecta la habitación, busca la reserva en Beds24 y envía el mensaje con el
    código de puerta a través de Booking.com Messages (POST /bookings/messages).
    Si dry_run=True, hace todo el proceso (detección + búsqueda de reserva +
    construcción del mensaje) pero NO llama al POST real que envía el mensaje
    al cliente — útil para probar sin molestar a huéspedes reales.
    Devuelve un dict con el resultado para poder loguear/depurar sin romper /extraer.
    """
    resultado = {"enviado": False, "dry_run": dry_run, "room_id": None, "book_id": None,
                 "mensaje_generado": None, "error": None}

    room_id = detectar_room_id(habitacion_texto, texto_completo)
    if not room_id:
        resultado["error"] = f"No se pudo detectar la habitación Beds24 a partir de: {habitacion_texto!r}"
        if not dry_run:
            avisar_error_critico(
                "No se pudo detectar la habitación",
                f"Parte recibido con habitación '{habitacion_texto}' pero no coincide con "
                f"ninguna de las 5 habitaciones configuradas. No se envió el código de puerta.\n"
                f"Revísalo manualmente cuanto antes."
            )
        return resultado
    resultado["room_id"] = room_id

    if not fecha_entrada:
        resultado["error"] = "Falta fecha_entrada, no se puede localizar la reserva en Beds24"
        if not dry_run:
            avisar_error_critico(
                "Falta fecha de entrada en un parte",
                f"Habitación '{habitacion_texto}' — no se pudo extraer la fecha de entrada del PDF. "
                f"No se envió el código de puerta. Revísalo manualmente."
            )
        return resultado

    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        resultado["error"] = f"No se pudo autenticar con Beds24: {e}"
        if not dry_run:
            avisar_error_critico(
                "Fallo de autenticación con Beds24",
                f"No se pudo obtener el access token de Beds24 ({e}). "
                f"Revisa BEDS24_REFRESH_TOKEN en Render — puede haber caducado."
            )
        return resultado

    try:
        book_id = buscar_booking_id_beds24(access_token, room_id, fecha_entrada)
        if not book_id:
            resultado["error"] = f"No se encontró reserva en Beds24 para room {room_id} / entrada {fecha_entrada}"
            if not dry_run:
                avisar_error_critico(
                    "Reserva no encontrada en Beds24",
                    f"Habitación {ROOM_CONFIG.get(room_id, {}).get('nombre', room_id)}, "
                    f"entrada {fecha_entrada} — no hay ninguna reserva en Beds24 con esa fecha exacta. "
                    f"Puede que la reserva aún no se haya sincronizado, o que la fecha del parte "
                    f"no coincida con la de Booking.com. No se envió el código de puerta."
                )
            return resultado
        resultado["book_id"] = book_id

        mensaje = construir_mensaje_codigo(room_id, nombre_cliente, version_minima=version_minima)
        resultado["mensaje_generado"] = mensaje

        if dry_run:
            logger.info(f"[DRY RUN] Se enviaría a booking {book_id} (room {room_id}), pero no se envía de verdad.")
            resultado["enviado"] = False
            return resultado

        resp = requests.post(
            f"{BEDS24_API_BASE}/bookings/messages",
            headers={"token": access_token, "accept": "application/json", "Content-Type": "application/json"},
            json=[{"bookingId": book_id, "message": mensaje, "sendEmail": enviar_tambien_email}],
            timeout=20,
        )
        resp.raise_for_status()
        respuesta_json = resp.json()
        resultado["respuesta_beds24"] = respuesta_json

        # La API puede devolver 200 OK a nivel HTTP pero reportar un error
        # por elemento dentro del cuerpo de la respuesta (patrón típico de
        # endpoints que procesan en lote). Lo comprobamos explícitamente
        # en vez de asumir éxito solo porque no hubo excepción HTTP.
        item_ok = True
        item_error = None
        if isinstance(respuesta_json, list) and respuesta_json:
            primer_item = respuesta_json[0]
            if isinstance(primer_item, dict):
                if primer_item.get("success") is False:
                    item_ok = False
                    item_error = primer_item.get("errors") or primer_item.get("error") or primer_item
        elif isinstance(respuesta_json, dict):
            if respuesta_json.get("success") is False:
                item_ok = False
                item_error = respuesta_json.get("errors") or respuesta_json.get("error") or respuesta_json

        if not item_ok:
            resultado["enviado"] = False
            resultado["error"] = f"Beds24 aceptó la petición pero reportó un error al crear el mensaje: {item_error}"
            avisar_error_critico(
                "Código de puerta NO enviado (rechazado por Beds24/Booking)",
                f"Booking {book_id}, habitación {ROOM_CONFIG.get(room_id, {}).get('nombre', room_id)}. "
                f"Error: {item_error}\nEnvía el código manualmente por Booking.com Messages."
            )
            return resultado

        resultado["enviado"] = True
        logger.info(f"Beds24: código enviado a booking {book_id} (room {room_id}) — respuesta: {respuesta_json}")
    except Exception as e:
        resultado["error"] = str(e)
        if not dry_run:
            avisar_error_critico(
                "Error inesperado enviando el código de puerta",
                f"Habitación {ROOM_CONFIG.get(room_id, {}).get('nombre', room_id)}, entrada {fecha_entrada}. "
                f"Error: {e}\nEnvía el código manualmente por Booking.com Messages."
            )

    return resultado


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

    Devuelve una lista de dicts: {room_id, nombre_habitacion, huesped, book_id}.
    Si falla la consulta (token, red, etc.) devuelve lista vacía y loguea el error,
    para no romper el resumen diario por un problema puntual de Beds24.
    """
    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        logger.error(f"[CONTROL] Beds24 auth falló al consultar reservas de {tipo} ({fecha_iso}): {e}")
        return []

    params = {"propertyId": BEDS24_PROPERTY_ID}
    campo_fecha = "arrival" if tipo == "checkin" else "departure"
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
        logger.error(f"[CONTROL] Error consultando reservas de {tipo} para {fecha_iso}: {e}")
        return []

    resultado = []
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
            f"obtener_bookings_dia_beds24({tipo}, {fecha_iso}): la API devolvió "
            f"{len(data)} reservas, {descartadas_por_fecha} descartadas por no "
            f"coincidir la fecha exacta de '{campo_fecha}' (filtro de la API no fiable)."
        )
    return resultado

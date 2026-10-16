"""
routes/checkin.py — Endpoints para la web de check-in estática (Firebase):
estado de la reserva y diagnóstico de los 3 estados de prueba.
"""
import logging
import requests
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from flask import Blueprint, request, jsonify

from config import (
    API_TOKEN, TEST_TOKEN, ROOM_CONFIG, RPV_LINKS, TEST_BOOKINGS,
    BEDS24_API_BASE, BEDS24_PROPERTY_ID,
)
from services.acceso_enlace import crear_token, verificar_token
from services.beds24 import (
    buscar_booking_por_id, buscar_booking_por_ref, buscar_booking_por_nombre, reservas_del_grupo, reservas_encadenadas,
    get_beds24_access_token, _extraer_nombre_huesped_beds24,
)
from services.limites import ip_cliente, permitir
from services.parte_intentos import aviso_parte_no_disponible
from services.rpv import parte_comprobado_estancia_iniciada, parte_recibido_para

logger = logging.getLogger(__name__)
checkin_bp = Blueprint("checkin", __name__)


HORA_CHECKIN  = dtime(15, 0)   # Check-in a las 15:00
HORA_CHECKOUT = dtime(12, 0)   # Check-out hasta las 12:00
# De más a menos «activo»: el estado del grupo es el primero de esta lista que tenga alguna de sus habitaciones
PRIORIDAD_ESTADOS = ["staying", "checkin_pending", "pre_checkin", "pending_early", "expired"]


def _estado_reserva(booking, ahora_madrid):
    """
    Estado de UNA reserva (una habitación) a la hora dada: los campos que dependen del momento y del parte en RPV
    (estado, parte_submitted, pin_available, pin, rpv_link y, cuando procede, aviso_parte). None si las fechas no son válidas.
    """
    room_id   = str(booking.get("roomId", ""))
    arrival   = booking.get("arrival", "")
    cfg       = ROOM_CONFIG.get(room_id, {})
    hoy        = ahora_madrid.date()
    hora_local = ahora_madrid.time().replace(tzinfo=None)

    try:
        arrival_date   = date.fromisoformat(arrival)
        departure_date = date.fromisoformat(booking.get("departure", ""))
    except (ValueError, TypeError):
        return None

    # ── 1. Estancia finalizada (después de las 12:00 del día de salida) ──
    if hoy > departure_date or (hoy == departure_date and hora_local >= HORA_CHECKOUT):
        return {"estado": "expired", "parte_submitted": False, "pin_available": False, "pin": None, "rpv_link": None}

    # ── 2. Ya alojado (entre check-in y check-out, no es el día de llegada) ──
    # Regla de Adrián: nadie entra sin haber rellenado el parte. Pasado el día de llegada (p. ej. quien
    # llega de madrugada) solo se NIEGA el código si RPV confirma que el parte NO está completado; si no
    # se puede comprobar (RPV no responde, la reserva no aparece…) se da, para no dejar fuera a un huésped
    # por un fallo puntual. El huésped ve el enlace de RPV y el código aparece en cuanto lo completa.
    if hoy > arrival_date:
        try:
            parte_pendiente = parte_comprobado_estancia_iniciada(room_id, arrival) == "pendiente"
        except Exception as e:   # la comprobación nunca debe impedir el check-in
            logger.error(f"[check-in] No se pudo comprobar el parte de la estancia iniciada: {e}")
            parte_pendiente = False
        if parte_pendiente:
            return {"estado": "checkin_pending", "parte_submitted": False, "pin_available": False, "pin": None,
                    "rpv_link": RPV_LINKS.get(room_id), "aviso_parte": None}
        return {"estado": "staying", "parte_submitted": True, "pin_available": True, "pin": cfg.get("pin"), "rpv_link": None}

    # ── 3. Verificar parte via API de RPV (válido para el día de hoy o días previos) ──
    parte_enviado = parte_recibido_para(room_id, arrival)
    aviso_parte = None
    if not parte_enviado:
        try:
            aviso_parte = aviso_parte_no_disponible(booking.get("id"), room_id, arrival, ahora_madrid)
        except Exception as e:   # el aviso es un extra: nunca debe impedir el check-in
            logger.error(f"[check-in] No se pudo calcular el aviso del parte: {e}")

    # ── 4. Antes del día de check-in, o el día de llegada antes de las 15:00 ──
    if hoy < arrival_date or hora_local < HORA_CHECKIN:
        # Parte enviado: mensaje de espera; si no, enlace de RPV
        return {"estado": "pending_early" if parte_enviado else "pre_checkin", "parte_submitted": parte_enviado,
                "pin_available": False, "pin": None,
                "rpv_link": RPV_LINKS.get(room_id) if not parte_enviado else None, "aviso_parte": aviso_parte}

    # ── 5. A partir de las 15:00 del día de check-in ──
    if parte_enviado:
        return {"estado": "staying", "parte_submitted": True, "pin_available": True, "pin": cfg.get("pin"), "rpv_link": None}
    return {"estado": "checkin_pending", "parte_submitted": False, "pin_available": False, "pin": None,
            "rpv_link": RPV_LINKS.get(room_id), "aviso_parte": aviso_parte}


@checkin_bp.route("/check-in", methods=["GET"])
def check_in_status():
    """
    GET /check-in?ref=<numero_de_reserva_o_nombre_completo>
    GET /check-in?t=<token>   (enlace personal firmado, ver services/acceso_enlace.py: no hace falta teclear el nombre)

    Endpoint para la web de check-in estática alojada en Firebase.
    El huésped introduce el número de su reserva (de cualquier canal, no
    solo Booking.com) — o, si eso falla, su nombre completo: no hay un
    campo de "número de reserva" fiable y universal en La Casa de la
    Primavera, que recibe reservas de Booking, Airbnb y Holidu, cada
    plataforma con su propio formato.

    Flujo:
      1. Busca la reserva en Beds24 por número de reserva, recorriendo
         todas las propiedades configuradas (BEDS24_PROPERTY_IDS)
         → obtiene habitación, nombre del huésped, fechas
      1b. Si no se encuentra por número, reintenta interpretando `ref` como
          nombre del huésped (buscar_booking_por_nombre) — restringido a una
          ventana estrecha de días de llegada para evitar ambigüedad entre
          huéspedes con nombres parecidos.
      2. Verifica vía la API de registroparteviajeros.com (polling directo,
         sin Gmail) si se recibió el parte
      3. Responde con:
         - parte_submitted: false  →  rpv_link  (enlace a registroparteviajeros.com)
         - parte_submitted: true   →  pin        (código de puerta de Render env vars)

    Ejemplo respuesta (parte pendiente):
    {
      "ok": true,
      "room_id": "702396",
      "room_name": "Playa de la Fossá",
      "guest_name": "María García",
      "arrival": "2026-07-20",
      "departure": "2026-07-22",
      "parte_submitted": false,
      "rpv_link": "https://app.registroparteviajeros.com/propiedad/MRgkbMeAt7",
      "pin": null
    }

    Ejemplo respuesta (parte recibido):
    { ...mismo esquema..., "parte_submitted": true, "rpv_link": null, "pin": "191199" }

    Con el parte pendiente y la llegada hoy o mañana, la respuesta lleva además `aviso_parte`: null (RPV conoce la
    reserva, o no se puede saber), "reintentar" (RPV aún no la tiene: volver a probar pasados unos minutos) o "llamar"
    (llamar al teléfono de atención al cliente) — reglas en services/parte_intentos.py.
    """
    # Freno al abuso (probar nombres a ritmo de máquina); holgado: la web consulta cada 45 s y varios huéspedes
    # pueden compartir la IP de la wifi.
    if not permitir([(f"check-in:{ip_cliente()}", 90, 60)]):
        return jsonify({"ok": False, "error": "demasiadas_peticiones"}), 429

    token_acceso = request.args.get("t", "").strip()
    if token_acceso:
        # Enlace personal: identifica la reserva sin teclear el nombre (el id sale de un token firmado).
        book_id_token = verificar_token(token_acceso)
        booking = buscar_booking_por_id(book_id_token) if book_id_token else None
        if not booking:
            return jsonify({"ok": False, "error": "enlace_no_valido"}), 404
    else:
        ref = request.args.get("ref", "").strip()
        if not ref:
            return jsonify({"ok": False, "error": "ref_required"}), 400

        # ── Reservas ficticias de prueba ──────────────────────────────────────
        if ref in TEST_BOOKINGS:
            t = TEST_BOOKINGS[ref]
            return jsonify({"ok": True, **t})

        booking = buscar_booking_por_ref(ref)
        if not booking:
            # Fallback: puede que `ref` sea el nombre del huésped, no un número
            # de reserva (necesario en La Casa de la Primavera, con reservas de
            # Booking/Airbnb/Holidu y sin un campo de número unificado).
            booking, ambiguo = buscar_booking_por_nombre(ref)
            if ambiguo:
                return jsonify({"ok": False, "error": "nombre_ambiguo"}), 409
        if not booking:
            return jsonify({"ok": False, "error": "no_encontrado"}), 404

    ahora_madrid = datetime.now(ZoneInfo('Europe/Madrid'))
    estado = _estado_reserva(booking, ahora_madrid)
    if estado is None:
        return jsonify({"ok": False, "error": "fechas_invalidas"}), 500

    room_id = str(booking.get("roomId", ""))
    base = {
        "ok": True, "room_id": room_id, "room_name": ROOM_CONFIG.get(room_id, {}).get("nombre", ""),
        "guest_name": _extraer_nombre_huesped_beds24(booking),
        "arrival": booking.get("arrival", ""), "departure": booking.get("departure", ""),
        "book_id": booking.get("id"),
        "acceso": crear_token(booking.get("id")),   # enlace personal: la web lo recuerda para la próxima vez (None si no hay clave)
    }
    respuesta = {**base, **estado}

    # ── Reserva de varias habitaciones: una reserva de Beds24 por habitación, mismo huésped ──
    # Los campos de arriba son los de la habitación encontrada (así la web antigua sigue funcionando); `habitaciones` trae
    # TODAS, cada una con su propio estado, parte y código, y `estado_grupo` el más avanzado de todas.
    try:
        miembros = reservas_del_grupo(booking)
    except Exception as e:   # un fallo al buscar el resto de habitaciones nunca debe impedir el check-in de esta
        logger.error(f"[check-in] No se pudo consultar el grupo de habitaciones: {e}")
        miembros = [booking]
    if len(miembros) > 1:
        habitaciones = []
        for m in miembros:
            est = estado if str(m.get("id")) == str(booking.get("id")) else _estado_reserva(m, ahora_madrid)
            if est is None:
                continue
            rid = str(m.get("roomId", ""))
            habitaciones.append({"book_id": m.get("id"), "room_id": rid, "room_name": ROOM_CONFIG.get(rid, {}).get("nombre", ""),
                                 "arrival": m.get("arrival", ""), "departure": m.get("departure", ""), **est})
        if len(habitaciones) > 1:
            respuesta["habitaciones"] = habitaciones
            respuesta["estado_grupo"] = min((h["estado"] for h in habitaciones), key=lambda e: PRIORIDAD_ESTADOS.index(e) if e in PRIORIDAD_ESTADOS else 99)

    # ── Otras reservas vigentes del mismo huésped (reservas encadenadas) ──
    # Quien encadena dos reservas puede registrar la siguiente desde el día antes: la web le muestra las dos y le deja
    # pasar de una a otra con su enlace personal, sin quedarse atrapado en la que tiene recordada en el dispositivo.
    try:
        otras = reservas_encadenadas(booking)
    except Exception as e:   # es un extra: nunca debe impedir el check-in
        logger.error(f"[check-in] No se pudieron buscar las otras reservas del huésped: {e}")
        otras = []
    if otras:
        respuesta["otras_reservas"] = [{"book_id": b.get("id"), "acceso": crear_token(b.get("id")), "arrival": b.get("arrival"),
                                        "departure": b.get("departure"),
                                        "room_name": ROOM_CONFIG.get(str(b.get("roomId", "")), {}).get("nombre", "")} for b in otras]
    return jsonify(respuesta)


@checkin_bp.route("/diagnostico", methods=["GET"])
def diagnostico_checkin():
    """
    Devuelve hasta 3 números de reserva reales para probar la web de check-in.

    GET /diagnostico?token=<TOKEN>

    Los 3 estados buscados son exactamente:
      1. sin_parte     → parte NO enviado (pendiente de registro)
      2. parte_enviado → parte enviado pero aún no es hora de check-in (antes del día o antes de 15:00)
      3. check_in_ok   → parte enviado Y ya es hora de check-in (o huésped ya alojado)
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    MADRID_TZ     = ZoneInfo('Europe/Madrid')
    HORA_CHECKIN  = dtime(15, 0)
    HORA_CHECKOUT = dtime(12, 0)

    ahora_madrid = datetime.now(MADRID_TZ)
    hoy          = ahora_madrid.date()
    hora_local   = ahora_madrid.time().replace(tzinfo=None)

    try:
        token_beds = get_beds24_access_token()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Beds24 auth: {e}"}), 500

    desde = (hoy - timedelta(days=3)).isoformat()
    hasta = (hoy + timedelta(days=30)).isoformat()

    try:
        resp = requests.get(
            f"{BEDS24_API_BASE}/bookings",
            headers={"token": token_beds, "accept": "application/json"},
            params={"propertyId": BEDS24_PROPERTY_ID,
                    "arrivalFrom": desde, "arrivalTo": hasta},
            timeout=20,
        )
        resp.raise_for_status()
        bookings = resp.json().get("data", [])
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error Beds24: {e}"}), 500

    ejemplos = {"sin_parte": None, "parte_enviado": None, "check_in_ok": None}

    for b in bookings:
        if str(b.get("status", "")).lower() == "cancelled":
            continue
        if all(v is not None for v in ejemplos.values()):
            break

        arrival   = b.get("arrival", "")
        departure = b.get("departure", "")
        room_id   = str(b.get("roomId", ""))
        api_ref   = b.get("apiReference", "")

        if not arrival or not departure or not api_ref:
            continue

        try:
            arrival_date   = date.fromisoformat(arrival)
            departure_date = date.fromisoformat(departure)
        except ValueError:
            continue

        # Ignorar estancias ya finalizadas
        if hoy > departure_date or (hoy == departure_date and hora_local >= HORA_CHECKOUT):
            continue

        cfg   = ROOM_CONFIG.get(room_id, {})
        parte = None  # solo consultamos RPV si es necesario

        def info(estado_label, parte_ok):
            return {
                "referencia":    api_ref,
                "habitacion":    cfg.get("nombre", room_id),
                "arrival":       arrival,
                "departure":     departure,
                "parte_enviado": parte_ok,
                "descripcion":   {
                    "sin_parte":    "Parte NO enviado — web mostrará enlace RPV",
                    "parte_enviado":"Parte enviado pero aún no es hora de check-in — web mostrará mensaje de espera",
                    "check_in_ok": "Parte enviado y hora de check-in alcanzada — web mostrará bienvenida + PIN",
                }.get(estado_label, ""),
            }

        # ── Huésped ya alojado (antes del check-out) → check_in_ok seguro ──
        if hoy > arrival_date:
            if ejemplos["check_in_ok"] is None:
                ejemplos["check_in_ok"] = info("check_in_ok", True)
            continue

        # ── Arrivals futuros o de hoy: consultar RPV ──
        parte = parte_recibido_para(room_id, arrival)

        es_antes_de_check_in = (hoy < arrival_date) or (hoy == arrival_date and hora_local < HORA_CHECKIN)

        if parte and not es_antes_de_check_in:
            # Parte enviado + hora OK → check_in_ok
            if ejemplos["check_in_ok"] is None:
                ejemplos["check_in_ok"] = info("check_in_ok", True)
        elif parte and es_antes_de_check_in:
            # Parte enviado pero demasiado pronto → parte_enviado
            if ejemplos["parte_enviado"] is None:
                ejemplos["parte_enviado"] = info("parte_enviado", True)
        else:
            # Sin parte → sin_parte
            if ejemplos["sin_parte"] is None:
                ejemplos["sin_parte"] = info("sin_parte", False)

    encontrados = {k: v for k, v in ejemplos.items() if v is not None}
    no_encontrados = [k for k, v in ejemplos.items() if v is None]

    return jsonify({
        "ok":            True,
        "hora_local":    ahora_madrid.strftime("%Y-%m-%d %H:%M:%S (Europe/Madrid)"),
        "nota":          "Introduce el campo 'referencia' en alc-homes-checkin.web.app para probar cada estado",
        "ejemplos":      encontrados,
        "no_disponibles": no_encontrados or None,
    })

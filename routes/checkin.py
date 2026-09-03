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
from services.beds24 import (
    buscar_booking_por_ref, buscar_booking_por_nombre,
    get_beds24_access_token, _extraer_nombre_huesped_beds24,
)
from services.rpv import parte_recibido_para

logger = logging.getLogger(__name__)
checkin_bp = Blueprint("checkin", __name__)


@checkin_bp.route("/check-in", methods=["GET"])
def check_in_status():
    """
    GET /check-in?ref=<numero_de_reserva_o_nombre_completo>

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
    """
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

    room_id    = str(booking.get("roomId", ""))
    arrival    = booking.get("arrival", "")
    departure  = booking.get("departure", "")
    guest_name = _extraer_nombre_huesped_beds24(booking)
    cfg        = ROOM_CONFIG.get(room_id, {})

    # Hora local española (CET/CEST) para comparar con horarios del hostal
    MADRID_TZ     = ZoneInfo('Europe/Madrid')
    HORA_CHECKIN  = dtime(15, 0)   # Check-in a las 15:00
    HORA_CHECKOUT = dtime(12, 0)   # Check-out hasta las 12:00

    ahora_madrid = datetime.now(MADRID_TZ)
    hoy          = ahora_madrid.date()
    hora_local   = ahora_madrid.time().replace(tzinfo=None)

    try:
        arrival_date   = date.fromisoformat(arrival)
        departure_date = date.fromisoformat(departure)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "fechas_invalidas"}), 500

    base = {
        "ok": True, "room_id": room_id, "room_name": cfg.get("nombre", ""),
        "guest_name": guest_name, "arrival": arrival, "departure": departure,
        "book_id": booking.get("id"),
    }

    # ── 1. Estancia finalizada (después de las 12:00 del día de salida) ──
    if hoy > departure_date or (hoy == departure_date and hora_local >= HORA_CHECKOUT):
        return jsonify({**base,
            "estado": "expired", "parte_submitted": False,
            "pin_available": False, "pin": None, "rpv_link": None,
        })

    # ── 2. Ya alojado (entre check-in y check-out, no es el día de llegada) ──
    # La API de RPV solo devuelve huéspedes del día actual, por lo que para días
    # posteriores al de llegada asumimos que el parte fue enviado (ya están dentro).
    if hoy > arrival_date:
        return jsonify({**base,
            "estado": "staying", "parte_submitted": True,
            "pin_available": True, "pin": cfg.get("pin"), "rpv_link": None,
        })

    # ── 3. Verificar parte via API de RPV (válido para el día de hoy o días previos) ──
    parte_enviado = parte_recibido_para(room_id, arrival)

    # ── 4. Antes del día de check-in ────────────────────────────────────────
    if hoy < arrival_date:
        if parte_enviado:
            # Parte enviado con antelación: mostrar mensaje de espera
            estado = "pending_early"
        else:
            # Parte pendiente: mostrar enlace RPV
            estado = "pre_checkin"
        return jsonify({**base,
            "estado": estado, "parte_submitted": parte_enviado,
            "pin_available": False, "pin": None,
            "rpv_link": RPV_LINKS.get(room_id) if not parte_enviado else None,
        })

    # ── 5. Día de check-in (hoy == arrival_date) ────────────────────────────
    if hora_local < HORA_CHECKIN:
        # Antes de las 15:00 del día de llegada
        if parte_enviado:
            estado = "pending_early"   # Parte OK → "vuelve a las 15:00 de hoy"
        else:
            estado = "pre_checkin"     # Sin parte → enlace RPV
        return jsonify({**base,
            "estado": estado, "parte_submitted": parte_enviado,
            "pin_available": False, "pin": None,
            "rpv_link": RPV_LINKS.get(room_id) if not parte_enviado else None,
        })

    # A partir de las 15:00 del día de check-in
    if parte_enviado:
        return jsonify({**base,
            "estado": "staying", "parte_submitted": True,
            "pin_available": True, "pin": cfg.get("pin"), "rpv_link": None,
        })
    else:
        return jsonify({**base,
            "estado": "checkin_pending", "parte_submitted": False,
            "pin_available": False, "pin": None,
            "rpv_link": RPV_LINKS.get(room_id),
        })


@checkin_bp.route("/diagnostico", methods=["GET"])
def diagnostico_checkin():
    """
    Devuelve hasta 3 números de reserva reales para probar la web de check-in.

    GET /diagnostico?token=Alchomes2025

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

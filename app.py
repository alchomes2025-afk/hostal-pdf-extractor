import os
import io
import re
import base64
import logging
import requests
import json
from flask import Flask, request, jsonify, render_template_string, redirect
from flask_cors import CORS
from pypdf import PdfReader, PdfWriter
import pdfplumber
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import quote

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class GmailAuthError(Exception):
    """Se lanza cuando el refresh token de Gmail ha caducado o es inválido."""
    pass


PDF_PASSWORD         = os.environ.get("PDF_PASSWORD", "Alchomes2025")
API_TOKEN            = os.environ.get("API_TOKEN", "")
TEST_TOKEN           = os.environ.get("TEST_TOKEN", "test1234")
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
REDIRECT_URI         = os.environ.get("REDIRECT_URI", "https://hostal-pdf-extractor.onrender.com/oauth/callback")
# ── WhatsApp CallMeBot ──────────────────────────────────────────────────────
# CALLMEBOT_PHONE  : número en formato internacional sin '+' (ej: 34644597897)
# CALLMEBOT_API_KEY: obtenida enviando "I allow callmebot to send me messages"
#                   al número +34 644 59 78 97 por WhatsApp
CALLMEBOT_PHONE     = os.environ.get("CALLMEBOT_PHONE", "")
CALLMEBOT_API_KEY   = os.environ.get("CALLMEBOT_API_KEY", "")
CALLMEBOT_PHONE_2   = os.environ.get("CALLMEBOT_PHONE_2", "")
CALLMEBOT_API_KEY_2 = os.environ.get("CALLMEBOT_API_KEY_2", "")

# ── Beds24 API (envío de código de puerta vía Booking.com Messages) ────────
# BEDS24_REFRESH_TOKEN se obtiene una vez intercambiando un invite code
# (Settings > Marketplace > API en Beds24) por GET /authentication/setup
BEDS24_REFRESH_TOKEN = os.environ.get("BEDS24_REFRESH_TOKEN", "")
BEDS24_PROPERTY_ID   = os.environ.get("BEDS24_PROPERTY_ID", "339751")
BEDS24_API_BASE      = "https://beds24.com/api/v2"

# PINs de acceso por habitación (se editan solo aquí, en Render → Environment)
PIN_HABITACION_2 = os.environ.get("PIN_HABITACION_2", "")  # Playa del Albir
PIN_HABITACION_3 = os.environ.get("PIN_HABITACION_3", "")  # Cala del Moraig
PIN_DOBLE        = os.environ.get("PIN_DOBLE", "")         # Playa de la Fossá
PIN_DELUXE       = os.environ.get("PIN_DELUXE", "")        # Cala Coveta Fumá

# Configuración de las 5 habitaciones: roomId de Beds24 → nombre + PIN + palabras clave
# para detectar a qué habitación corresponde un parte de viajero (buscando en el
# nombre del archivo y en el texto extraído del PDF, sin acentos gracias a normalizar()).
ROOM_CONFIG = {
    "702397": {"nombre": "Playa Lanuza",       "pin": None,             "keywords": ["lanuza"]},
    "702398": {"nombre": "Playa del Albir",    "pin": PIN_HABITACION_2, "keywords": ["albir"]},
    "702399": {"nombre": "Cala del Moraig",    "pin": PIN_HABITACION_3, "keywords": ["moraig"]},
    "702396": {"nombre": "Playa de la Fossá",  "pin": PIN_DOBLE,        "keywords": ["fossa"]},
    "702395": {"nombre": "Cala Coveta Fumá",   "pin": PIN_DELUXE,       "keywords": ["coveta", "fuma"]},
}

HABITACIONES = {
    "habitacion simple 1": "Habitación Simple 1",
    "habitacion simple 2": "Habitación Simple 2",
    "habitacion simple 3": "Habitación Simple 3",
    "habitacion doble 1":  "Habitación Doble 1",
    "habitacion doble 2":  "Habitación Doble 2",
    "habitacion doble 3":  "Habitación Doble 3",
    "habitacion doble 4":  "Habitación Doble 4",
    "habitacion doble 5":  "Habitación Doble 5",
    "habitacion deluxe 1": "Habitación Deluxe 1",
    "habitacion deluxe 2": "Habitación Deluxe 2",
    "habitacion deluxe 3": "Habitación Deluxe 3",
    "habitacion deluxe 4": "Habitación Deluxe 4",
    "habitacion deluxe 5": "Habitación Deluxe 5",
}
# ════════════════════════════════════════════════════════════════════════════
# MÓDULO CHECK-IN WEB  ·  Pegar justo DESPUÉS del bloque ROOM_CONFIG
# ════════════════════════════════════════════════════════════════════════════
#
# Reutiliza sin modificar:
#   get_beds24_access_token, obtener_partes_recibidos_hoy,
#   detectar_room_id, _extraer_nombre_huesped_beds24, ROOM_CONFIG,
#   GmailAuthError
#
# El asistente virtual llama a Groq DIRECTAMENTE desde el browser (igual
# que el app en Vercel), por lo que NO se añade ningún proxy /chat aquí.
# ════════════════════════════════════════════════════════════════════════════

# ── Links de registroparteviajeros.com por room_id de Beds24 ─────────────
RPV_LINKS = {
    "702397": "https://app.registroparteviajeros.com/propiedad/hliPDoDTb9",  # Hab 1 · Playa Lanuza
    "702398": "https://app.registroparteviajeros.com/propiedad/CgbPrarDLi",  # Hab 2 · Playa del Albir
    "702399": "https://app.registroparteviajeros.com/propiedad/d1ydBdUSOr",  # Hab 3 · Cala del Moraig
    "702396": "https://app.registroparteviajeros.com/propiedad/MRgkbMeAt7",  # Hab 4 · Playa de la Fossá
    "702395": "https://app.registroparteviajeros.com/propiedad/YQGCsngJaN",  # Hab 5 · Cala Coveta Fumá
}

# ── Configuración: API de registroparteviajeros ───────────────────────
RPV_API_KEY = os.environ.get("RPV_API_KEY", "")
RPV_API_URL = "https://app.registroparteviajeros.com/api/v1/usuarios"

# Mapeo: room_id de Beds24 → prop_id real de registroparteviajeros
# (se obtiene de la sección API → Código de Propiedad del panel de RPV)
RPV_PROPERTY_MAP = {
    "702397": "prop_W17mVBrVyrGinzWt4VvGmw",   # Playa Lanuza        (Hab 1)
    "702398": "prop_W17mVBrVyrGinzWs_jN1-Q",   # Playa del Albir     (Hab 2)
    "702399": "prop_W17mVBrVyrGinzWj80wzxw",    # Cala del Moraig     (Hab 3)
    "702396": "prop_W17mVBrVyrGinzWivGS55Q",    # Playa de la Fossá   (Hab 4)
    "702395": "prop_W17mVBrVyrGinzWhTOByeA",    # Cala Coveta Fumá    (Hab 5)
}

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

    try:
        resp = requests.get(
            f"{BEDS24_API_BASE}/bookings",
            headers={"token": token, "accept": "application/json"},
            params={"propertyId": BEDS24_PROPERTY_ID,
                    "arrivalFrom": desde, "arrivalTo": hasta},
            timeout=20,
        )
        resp.raise_for_status()
        bookings = resp.json().get("data", [])
        logger.info(f"[check-in] Beds24: {len(bookings)} reservas en rango {desde}→{hasta}")
    except Exception as e:
        logger.error(f"[check-in] Error consultando Beds24: {e}")
        return None

    ref_norm = booking_ref.strip().lower()

    for b in bookings:
        if str(b.get("status", "")).lower() == "cancelled":
            continue
        campo = _ref_en_booking(b, ref_norm)
        if campo is not None:
            logger.info(
                f"[check-in] Reserva encontrada: ref='{booking_ref}' "
                f"en campo '{campo}' → book_id={b.get('id')} room={b.get('roomId')}"
            )
            return b

    # Log de diagnóstico: ayuda a ver qué campos devuelve Beds24
    if bookings:
        logger.warning(
            f"[check-in] ref='{booking_ref}' no encontrado. "
            f"Campos del primer booking: {list(bookings[0].keys())}"
        )
    else:
        logger.warning(f"[check-in] ref='{booking_ref}': sin reservas en el rango.")
    return None


# ── Función: verificar parte de viajero ──────────────────────────────────

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


GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL    = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL_PRI  = "llama-3.3-70b-versatile"
GROQ_MODEL_FALL = "llama-3.1-8b-instant"


@app.route("/chat", methods=["POST"])
def chat_proxy():
    """
    POST /chat
    Body: { "system": "...", "messages": [...] }

    Proxy a Groq para el asistente virtual de la web de check-in Firebase.
    La clave GROQ_API_KEY permanece en Render (nunca expuesta en el HTML ni en GitHub).
    Intenta primero con el modelo principal; si falla (429/503) cae al modelo de respaldo.
    """
    if not GROQ_API_KEY:
        return jsonify({"ok": False, "error": "GROQ_API_KEY no configurada en Render"}), 500

    data     = request.get_json(force=True) or {}
    messages = data.get("messages", [])
    system   = data.get("system", "")

    if system:
        messages = [{"role": "system", "content": system}] + messages

    def llamar_groq(model):
        return requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": messages,
                  "max_tokens": 600, "temperature": 0.3},
            timeout=30,
        )

    try:
        resp = llamar_groq(GROQ_MODEL_PRI)
        if resp.status_code in (429, 503):
            logger.warning(f"[chat] {GROQ_MODEL_PRI} → {resp.status_code}, probando fallback")
            resp = llamar_groq(GROQ_MODEL_FALL)
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"].strip()
        return jsonify({"ok": True, "reply": reply})
    except Exception as e:
        logger.error(f"[chat] Error Groq: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Reservas de prueba ficticias ─────────────────────────────────────────
# Números que siempre devuelven un estado concreto para poder probar la web
# sin depender de reservas reales ni del horario actual.
TEST_BOOKINGS = {
    "9999000001": {
        "estado":        "pre_checkin",
        "parte_submitted": False,
        "pin_available": False,
        "guest_name":    "Hermann Müller",
        "room_id":       "702398",
        "room_name":     "Playa del Albir",
        "arrival":       "2026-07-20",
        "departure":     "2026-07-22",
        "pin":           None,
        "rpv_link":      RPV_LINKS.get("702398"),
    },
    "9999000002": {
        "estado":        "pending_early",
        "parte_submitted": True,
        "pin_available": False,
        "guest_name":    "Hermann Müller",
        "room_id":       "702398",
        "room_name":     "Playa del Albir",
        "arrival":       "2026-07-20",
        "departure":     "2026-07-22",
        "pin":           None,
        "rpv_link":      None,
    },
    "9999000003": {
        "estado":        "staying",
        "parte_submitted": True,
        "pin_available": True,
        "guest_name":    "Hermann Müller",
        "room_id":       "702398",
        "room_name":     "Playa del Albir",
        "arrival":       "2026-07-20",
        "departure":     "2026-07-22",
        "pin":           ROOM_CONFIG.get("702398", {}).get("pin", "XXXXXX"),
        "rpv_link":      None,
    },
}


# ── Endpoint: GET /check-in ───────────────────────────────────────────────

@app.route("/check-in", methods=["GET"])
def check_in_status():
    """
    GET /check-in?ref=<numero_confirmacion_booking_com>

    Endpoint para la web de check-in estática alojada en Firebase.
    El huésped introduce el número de su confirmación de Booking.com.

    Flujo:
      1. Busca la reserva en Beds24 por número de Booking.com
         → obtiene habitación, nombre del huésped, fechas
      2. Verifica en Gmail (vía Make/Render) si se recibió el parte
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
        return jsonify({"ok": False, "error": "no_encontrado"}), 404

    room_id    = str(booking.get("roomId", ""))
    arrival    = booking.get("arrival", "")
    departure  = booking.get("departure", "")
    guest_name = _extraer_nombre_huesped_beds24(booking)
    cfg        = ROOM_CONFIG.get(room_id, {})

    # Hora local española (CET/CEST) para comparar con horarios del hostal
    MADRID_TZ     = ZoneInfo('Europe/Madrid')
    HORA_CHECKIN  = dtime(15, 0)   # Check-in a las 15:00
    HORA_CHECKOUT = dtime(11, 0)   # Check-out hasta las 11:00

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
    }

    # ── 1. Estancia finalizada (después de las 11:00 del día de salida) ──
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


# ── Endpoint: GET /diagnostico ────────────────────────────────────────────────

@app.route("/diagnostico", methods=["GET"])
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
    HORA_CHECKOUT = dtime(11, 0)

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


TEST_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test — Hostal PDF Extractor</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1.5rem}
  .card{background:#fff;border-radius:12px;border:1px solid #e0e0e0;padding:2rem;width:100%;max-width:580px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
  h1{font-size:1.2rem;font-weight:600;margin-bottom:.25rem}
  .sub{font-size:.85rem;color:#666;margin-bottom:1.75rem}
  label{display:block;font-size:.82rem;font-weight:500;color:#444;margin-bottom:.35rem}
  input[type=text],input[type=file]{width:100%;padding:.55rem .75rem;border:1px solid #d0d0d0;border-radius:8px;font-size:.9rem;margin-bottom:1.1rem}
  input[type=file]{padding:.4rem .5rem;cursor:pointer}
  button{width:100%;padding:.7rem;background:#5c2d91;color:#fff;border:none;border-radius:8px;font-size:.95rem;font-weight:500;cursor:pointer}
  button:disabled{background:#aaa;cursor:not-allowed}
  .result{margin-top:1.5rem;padding:1.25rem;border-radius:8px;font-size:.875rem}
  .result.ok{background:#f0faf4;border:1px solid #a3d9b5}
  .result.err{background:#fff5f5;border:1px solid #f5b8b8}
  .field{display:flex;justify-content:space-between;padding:.45rem 0;border-bottom:1px solid #eee}
  .field:last-child{border-bottom:none}
  .field .key{color:#555;font-weight:500}
  .field .val{color:#222;text-align:right;max-width:65%;word-break:break-all}
  .field .val.ok{color:#1a7a3f;font-weight:600}
  .field .val.err{color:#c0392b;font-weight:600}
  .spinner{display:none;text-align:center;margin-top:1.2rem;color:#888;font-size:.85rem}
  details{margin-top:.75rem}
  details summary{cursor:pointer;font-size:.8rem;color:#666;padding:.3rem 0}
  .raw{background:#f7f7f7;border:1px solid #e0e0e0;border-radius:6px;padding:.75rem;font-size:.75rem;font-family:monospace;white-space:pre-wrap;word-break:break-all;max-height:220px;overflow-y:auto;margin-top:.4rem}
</style>
</head>
<body>
<div class="card">
  <h1>🏨 Hostal PDF Extractor — Test</h1>
  <p class="sub">Sube un parte de viajero para comprobar que los datos se extraen correctamente.</p>
  <label>Contraseña de acceso al test</label>
  <input type="text" id="token" placeholder="test1234" />
  <label>Archivo PDF (parte de viajero)</label>
  <input type="file" id="pdffile" accept=".pdf" />
  <button id="btn" onclick="enviar()">Analizar PDF</button>
  <div class="spinner" id="spin">⏳ Procesando…</div>
  <div id="out"></div>
</div>
<script>
async function enviar() {
  const token = document.getElementById('token').value.trim();
  const fileInput = document.getElementById('pdffile');
  const out = document.getElementById('out'); const btn = document.getElementById('btn');
  const spin = document.getElementById('spin');
  out.innerHTML = '';
  if (!token) { alert('Introduce la contraseña'); return; }
  if (!fileInput.files.length) { alert('Selecciona un PDF'); return; }
  const file = fileInput.files[0];
  const reader = new FileReader();
  reader.onload = async function(e) {
    const b64 = e.target.result.split(',')[1];
    btn.disabled = true; spin.style.display = 'block';
    try {
      const resp = await fetch('/extraer-test', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ pdf_base64: b64, pdf_filename: file.name, token: token })
      });
      const data = await resp.json();
      if (data.ok) {
        const fmt = (v) => v || '<span style="color:#c0392b">⚠ No encontrado</span>';
        out.innerHTML = `<div class="result ok">
          <div class="field"><span class="key">Estado</span><span class="val ok">✅ Correcto</span></div>
          <div class="field"><span class="key">Habitación</span><span class="val">${fmt(data.habitacion)}</span></div>
          <div class="field"><span class="key">Email</span><span class="val">${fmt(data.email)}</span></div>
          <div class="field"><span class="key">Fecha entrada</span><span class="val">${fmt(data.fecha_entrada)}</span></div>
          <div class="field"><span class="key">Fecha salida</span><span class="val">${fmt(data.fecha_salida)}</span></div>
        </div>
        <details><summary>🔍 Texto extraído del PDF</summary><div class="raw">${escHtml(data.texto_extraido||'')}</div></details>
        <details><summary>{ } JSON completo</summary><div class="raw">${escHtml(JSON.stringify(data,null,2))}</div></details>`;
      } else {
        out.innerHTML = `<div class="result err"><div class="field"><span class="key">Error</span><span class="val err">${escHtml(data.error)}</span></div></div>`;
      }
    } catch(err) {
      out.innerHTML = `<div class="result err"><div class="field"><span class="key">Error de red</span><span class="val">${escHtml(String(err))}</span></div></div>`;
    } finally { btn.disabled = false; spin.style.display = 'none'; }
  };
  reader.readAsDataURL(file);
}
function escHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
</script></body></html>"""


# ── OAuth helpers ──────────────────────────────────────────────────────────────

def get_access_token():
    """Obtiene un access token fresco usando el refresh token guardado."""
    refresh_token = GOOGLE_REFRESH_TOKEN
    if not refresh_token:
        raise GmailAuthError("GOOGLE_REFRESH_TOKEN no configurado. Ve a /oauth/inicio para autorizarlo.")
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }, timeout=15)
    if resp.status_code == 400:
        raise GmailAuthError("Token OAuth de Gmail caducado o revocado. Renuévalo en /oauth/inicio")
    resp.raise_for_status()
    return resp.json()["access_token"]


def descargar_adjunto_gmail(message_id, access_token):
    """
    Descarga el primer adjunto PDF de un email de Gmail por su message_id,
    y además el cuerpo de texto del email (para poder extraer el localizador
    del parte, que identifica la habitación real de forma fiable).
    """
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    msg = resp.json()

    def buscar_pdf(parts, nivel=0):
        for i, part in enumerate(parts):
            mime   = part.get("mimeType", "")
            fn     = part.get("filename", "")
            body   = part.get("body", {})
            att_id = body.get("attachmentId", "")
            logger.info(f"  Parte[{nivel}][{i}]: mime={mime} fn={repr(fn)} att={bool(att_id)}")
            es_pdf = (mime == "application/pdf") or (fn and fn.lower().endswith(".pdf"))
            if es_pdf and att_id:
                logger.info(f"  PDF encontrado: {fn}")
                return att_id, fn
            subparts = part.get("parts", [])
            if subparts:
                r = buscar_pdf(subparts, nivel+1)
                if r[0]:
                    return r
        return None, None

    def buscar_cuerpo(parts, nivel=0):
        """Busca la parte text/plain (o si no, text/html) con el cuerpo del email."""
        html_fallback = None
        for part in parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data", "")
            if mime == "text/plain" and data:
                try:
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
                except Exception:
                    pass
            if mime == "text/html" and data and html_fallback is None:
                try:
                    html_fallback = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
                except Exception:
                    pass
            subparts = part.get("parts", [])
            if subparts:
                r = buscar_cuerpo(subparts, nivel+1)
                if r:
                    return r
        return html_fallback

    parts = msg.get("payload", {}).get("parts", [])
    logger.info(f"Buscando PDF en {message_id}, partes: {len(parts)}")
    attachment_id, filename = buscar_pdf(parts)

    # Cuerpo del email (best effort, no debe romper el flujo si falla)
    try:
        cuerpo_texto = buscar_cuerpo(parts) or ""
        cuerpo_texto = re.sub(r"<[^>]+>", " ", cuerpo_texto)  # limpiar HTML si vino de html_fallback
    except Exception as e:
        logger.warning(f"No se pudo extraer cuerpo del email {message_id}: {e}")
        cuerpo_texto = ""

    if not attachment_id:
        raise Exception(f"No se encontró adjunto PDF en el mensaje {message_id}")

    url2 = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}"
    resp2 = requests.get(url2, headers=headers, timeout=30)
    resp2.raise_for_status()
    data_b64 = resp2.json().get("data", "")
    pdf_bytes = base64.urlsafe_b64decode(data_b64 + "==")
    return pdf_bytes, filename, cuerpo_texto


# ── PDF helpers ────────────────────────────────────────────────────────────────

def normalizar(texto):
    t = texto.lower()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")]:
        t = t.replace(a, b)
    t = t.replace("habitación","habitacion")
    t = re.sub(r"[^a-z0-9 ]","",t).strip()
    return t

def habitacion_desde_nombre_archivo(pdf_filename):
    nombre_base = pdf_filename.replace(".pdf", "")
    nombre_base = re.sub(r"[_\s]+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$", "", nombre_base)
    nombre_base = re.sub(r"[_\s]+\d{4}[-/]\d{2}[-/]\d{2}$", "", nombre_base)
    nombre_base = nombre_base.replace("_", " ").strip()
    m = re.search(r"(Habitaci[oó]n\s+\S+(?:\s+\d+)?)", nombre_base, re.IGNORECASE)
    if m:
        hab = m.group(1).strip()
        hab = re.sub(r"[Hh]abitacion", "Habitación", hab, flags=re.IGNORECASE)
        return hab
    return nombre_base.title()

def parsear_fecha(texto_fecha):
    texto_fecha = texto_fecha.strip()
    m = re.match(r"^(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})$", texto_fecha)
    if m:
        try: return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError: pass
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", texto_fecha)
    if m:
        try: return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError: pass
    return None

def extraer_fechas_por_etiqueta(texto):
    entrada = salida = None
    patrones = [
        (re.compile(r"fecha\s*de?\s*entrada[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})", re.I), "entrada"),
        (re.compile(r"check[\s\-]?in[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})", re.I), "entrada"),
        (re.compile(r"fecha\s*de?\s*salida[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})", re.I), "salida"),
        (re.compile(r"check[\s\-]?out[:\s]+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4}|\d{4}-\d{2}-\d{2})", re.I), "salida"),
    ]
    for patron, destino in patrones:
        m = patron.search(texto)
        if m:
            d = parsear_fecha(m.group(1))
            if d:
                if destino == "entrada" and entrada is None: entrada = d.isoformat()
                elif destino == "salida" and salida is None: salida = d.isoformat()
    return entrada, salida

def _limpiar_nombre(s):
    """Limpia ruido al final de un nombre capturado por regex."""
    s = re.split(r"\s{2,}|\t", s)[0].strip()
    s = re.sub(r"\d+", "", s).strip()
    s = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ\s\-']", "", s).strip()
    return s.title() if len(s) >= 3 else None

def extraer_nombre_completo(texto):
    """
    Extrae el nombre completo del parte de viajero.
    Prueba múltiples formatos que usa registroparteviajeros.com.
    """
    # Formato 1: "Nombre y apellidos: ..."
    m = re.search(r"nombre\s+y\s+apellidos?\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][^\n\r]{2,50})", texto, re.I)
    if m: return _limpiar_nombre(m.group(1))

    # Formato 2: "Nombre completo: ..."
    m = re.search(r"nombre\s+completo\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][^\n\r]{2,50})", texto, re.I)
    if m: return _limpiar_nombre(m.group(1))

    # Formato 3: Nombre + Primer apellido + Segundo apellido (campos separados)
    m_n  = re.search(r"(?:^|\n)\s*nombre\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]{1,25})", texto, re.I | re.M)
    m_a1 = re.search(r"primer\s+apellido\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]{1,30})", texto, re.I)
    m_a2 = re.search(r"segundo\s+apellido\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]{0,30})", texto, re.I)
    if m_n:
        partes = [m_n.group(1).strip()]
        if m_a1: partes.append(m_a1.group(1).strip())
        if m_a2 and m_a2.group(1).strip(): partes.append(m_a2.group(1).strip())
        nombre = " ".join(partes)
        return nombre.title() if len(nombre) >= 3 else None

    # Formato 4: "Apellidos: ... Nombre: ..."
    m_ap = re.search(r"apellidos?\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][^\n\r]{2,40})", texto, re.I)
    m_n2 = re.search(r"(?:^|\n)\s*nombre\s*[:\s]+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]{1,25})", texto, re.I | re.M)
    if m_n2 and m_ap:
        nombre = f"{m_n2.group(1).strip()} {_limpiar_nombre(m_ap.group(1)) or ''}".strip()
        return nombre.title() if len(nombre) >= 3 else None

    return None

def extraer_telefono(texto):
    """
    Extrae número de teléfono del parte de viajero.
    Busca primero por etiqueta, luego por patrón de 9 dígitos españoles.
    """
    # Por etiqueta
    patrones_etiqueta = [
        re.compile(r"tel[eé]fono\s*[:\s]+([\+\d][\d\s\-]{7,18})", re.I),
        re.compile(r"m[oó]vil\s*[:\s]+([\+\d][\d\s\-]{7,18})", re.I),
        re.compile(r"\btel[\.:\s]+([\+\d][\d\s\-]{7,18})", re.I),
        re.compile(r"phone\s*[:\s]+([\+\d][\d\s\-]{7,18})", re.I),
    ]
    for patron in patrones_etiqueta:
        m = patron.search(texto)
        if m:
            tel = re.sub(r"[\s\-\.]", "", m.group(1)).strip()
            if 9 <= len(tel) <= 15:
                return tel
    return None


# ── Beds24: detección de habitación + envío de código por Booking.com ──────

# Nombre de habitación tal como aparece SIEMPRE en registroparteviajeros.com
# (fijo por propiedad, a diferencia del localizador que es único por cada reserva)
# → roomId real de Beds24. Esta es la correspondencia estable a usar para detectar
# la habitación real.
NOMBRE_FIJO_ROOM_MAP = {
    "habitacion simple 1": "702397",  # Playa Lanuza
    "habitacion simple 2": "702398",  # Playa del Albir
    "habitacion simple 3": "702399",  # Cala del Moraig
    "habitacion doble 4":  "702396",  # Playa de la Fossá
    "habitacion deluxe 5": "702395",  # Cala Coveta Fumá
}


def extraer_localizador(texto):
    """Extrae el localizador de registroparteviajeros.com, ej: 'vbZ-O7Pr'.
    OJO: el localizador es único por RESERVA, no identifica la habitación de
    forma fiable (cada parte tiene uno distinto). Se conserva la función por si
    hace falta para otros usos, pero NO se usa para detectar la habitación."""
    m = re.search(r"localizador\s+es\s+([A-Za-z0-9\-_]{4,20})", texto or "", re.I)
    return m.group(1) if m else None


def detectar_room_id(habitacion_texto, texto_completo):
    """
    Detecta el roomId de Beds24 (702395-702399) a partir del nombre de
    habitación tal como lo usa registroparteviajeros.com (ej. "Habitación
    Simple 2"), que es fijo por propiedad y no cambia entre reservas.
    Como respaldo, también prueba por palabras clave del nombre real
    (lanuza, albir, moraig, fossa, coveta) por si algún día cambia el naming.
    """
    texto_normalizado = normalizar(habitacion_texto or "")

    # 1º: nombre fijo exacto (ej. "habitacion simple 2")
    for nombre_fijo, room_id in NOMBRE_FIJO_ROOM_MAP.items():
        if nombre_fijo in texto_normalizado:
            logger.info(f"Habitación detectada por nombre fijo '{nombre_fijo}' → room {room_id}")
            return room_id

    # 2º: palabras clave del nombre real, por si aparecen en el PDF o el email
    texto_buscar = normalizar((habitacion_texto or "") + " " + (texto_completo or ""))
    for room_id, cfg in ROOM_CONFIG.items():
        for kw in cfg["keywords"]:
            if kw in texto_buscar:
                logger.info(f"Habitación detectada por palabra clave '{kw}' → room {room_id}")
                return room_id
    return None


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


def avisar_error_critico(titulo, detalle):
    """
    Envía un aviso por WhatsApp cuando falla un punto crítico del flujo
    (autenticación Beds24, habitación no detectada, reserva no encontrada,
    envío de mensaje fallido, etc.). No lanza excepción si el propio envío
    de WhatsApp falla — solo lo loguea, para no romper el flujo principal.
    """
    mensaje = f"⚠️ ALCHOMES — {titulo}\n\n{detalle}"
    logger.error(f"[CONTROL] {titulo}: {detalle}")
    try:
        enviar_whatsapp_callmebot(mensaje)
    except Exception as e:
        logger.error(f"No se pudo enviar el aviso de error crítico por WhatsApp: {e}")


def enviar_codigo_puerta_beds24(habitacion_texto, texto_completo, fecha_entrada, nombre_cliente=None, dry_run=False, version_minima=False, enviar_tambien_email=False):
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


def procesar_pdf_bytes(pdf_bytes, pdf_filename, incluir_texto=False):
    resultado = {"habitacion": habitacion_desde_nombre_archivo(pdf_filename),
                 "nombre": None, "telefono": None,
                 "email": None, "fecha_entrada": None, "fecha_salida": None,
                 "texto_extraido": None, "error": None}
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            if reader.decrypt(PDF_PASSWORD) == 0:
                resultado["error"] = "Contraseña incorrecta"; return resultado
        writer = PdfWriter()
        for page in reader.pages: writer.add_page(page)
        buf = io.BytesIO(); writer.write(buf); buf.seek(0)
    except Exception as e:
        resultado["error"] = f"Error desencriptando: {e}"; return resultado
    try:
        partes = []
        with pdfplumber.open(buf) as pdf:
            for p in pdf.pages:
                t = p.extract_text()
                if t: partes.append(t)
        texto = "\n".join(partes)
    except Exception as e:
        resultado["error"] = f"Error extrayendo texto: {e}"; return resultado
    if incluir_texto: resultado["texto_extraido"] = texto
    emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", texto)
    validos = [e for e in emails if "registroparteviajeros" not in e.lower()]
    if validos: resultado["email"] = validos[0]
    resultado["nombre"]   = extraer_nombre_completo(texto)
    resultado["telefono"] = extraer_telefono(texto)
    entrada, salida = extraer_fechas_por_etiqueta(texto)
    resultado["fecha_entrada"] = entrada
    resultado["fecha_salida"] = salida
    if not entrada or not salida:
        m_gen = re.search(r"generado[:\s]+(\d{1,2}/\d{2}/\d{4})", texto, re.I)
        fecha_gen = parsear_fecha(m_gen.group(1)) if m_gen else None
        todas = re.findall(r"\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})\b", texto)
        fechas_iso = []
        for dia, mes, anyo in todas:
            try:
                d = date(int(anyo), int(mes), int(dia))
                if fecha_gen and d == fecha_gen: continue
                fechas_iso.append(d.isoformat())
            except ValueError: continue
        vistas = set(); fechas_unicas = []
        for f in fechas_iso:
            if f not in vistas: vistas.add(f); fechas_unicas.append(f)
        if not entrada and len(fechas_unicas) >= 1: resultado["fecha_entrada"] = fechas_unicas[0]
        if not salida  and len(fechas_unicas) >= 2: resultado["fecha_salida"]  = fechas_unicas[1]
    if resultado["fecha_entrada"] and resultado["fecha_salida"]:
        if resultado["fecha_salida"] < resultado["fecha_entrada"]:
            resultado["fecha_entrada"], resultado["fecha_salida"] = resultado["fecha_salida"], resultado["fecha_entrada"]
    return resultado


# ── Resumen diario WhatsApp ────────────────────────────────────────────────────

def buscar_message_ids_gmail(access_token, max_results=30):
    """
    Busca en Gmail los emails de partes de viajeros de los últimos 90 días.
    Usa el mismo filtro de asunto que el módulo 1 de Make.com.
    """
    query = 'subject:"Parte de viajeros" has:attachment newer_than:90d'
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"q": query, "maxResults": max_results}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    msgs = resp.json().get("messages", [])
    logger.info(f"Gmail: encontrados {len(msgs)} emails de partes de viajeros")
    return [m["id"] for m in msgs]


def obtener_todas_reservas():
    """
    Extrae datos de todos los partes de viajeros de Gmail (últimos 90 días).
    Gmail devuelve los mensajes del más reciente al más antiguo, así que
    el primer ejemplar de cada (habitacion, fecha_entrada) es el más actual.
    Lanza GmailAuthError si el token OAuth ha caducado.
    """
    try:
        access_token = get_access_token()
    except GmailAuthError:
        raise  # propagar para que /resumen pueda avisar por WhatsApp
    except Exception as e:
        logger.error(f"Error obteniendo access token: {e}")
        return []

    try:
        message_ids = buscar_message_ids_gmail(access_token)
    except Exception as e:
        logger.error(f"Error buscando emails en Gmail: {e}")
        return []

    reservas = []
    claves_vistas = set()  # deduplicar por (habitacion, fecha_entrada)

    for msg_id in message_ids:
        try:
            pdf_bytes, filename, _cuerpo = descargar_adjunto_gmail(msg_id, access_token)
            r = procesar_pdf_bytes(pdf_bytes, filename or "documento.pdf")
            if r["error"]:
                logger.warning(f"PDF {msg_id}: {r['error']}")
                continue
            if not r["fecha_entrada"] or not r["fecha_salida"]:
                logger.warning(f"PDF {msg_id}: fechas no encontradas")
                continue
            clave = (r["habitacion"], r["fecha_entrada"])
            if clave in claves_vistas:
                logger.info(f"Duplicado ignorado: {clave}")
                continue
            claves_vistas.add(clave)
            reservas.append({
                "habitacion":    r["habitacion"],
                "nombre":        r["nombre"],
                "telefono":      r["telefono"],
                "email":         r["email"],
                "fecha_entrada": r["fecha_entrada"],
                "fecha_salida":  r["fecha_salida"],
            })
            logger.info(f"Reserva OK: {r['habitacion']} {r['fecha_entrada']}→{r['fecha_salida']}")
        except Exception as e:
            logger.warning(f"Error procesando mensaje {msg_id}: {e}")

    return reservas


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


def generar_mensaje_resumen(hora_str=None):
    """
    Genera el resumen diario para WhatsApp.
    - ENTRADAS HOY y SALIDAS HOY se obtienen directamente de Beds24 (reservas
      reales), no de los emails de partes recibidos — así el resumen refleja
      quién debe entrar/salir hoy según la reserva, independientemente de si
      ya ha rellenado el parte o no.
    - Para cada ENTRADA se indica además si el parte de viajero ya se ha
      recibido (cruzando con los emails procesados) o si sigue pendiente.
    """
    hoy = date.today()
    hoy_iso = hoy.isoformat()
    if hora_str is None:
        hora_str = datetime.now().strftime("%H")

    # Punto de control: verificar autenticación con Beds24 antes de consultar
    # las reservas del día. Si falla, avisamos por WhatsApp además de que el
    # resumen seguirá generándose (con listas vacías) para no bloquear el envío.
    try:
        get_beds24_access_token()
    except Exception as e:
        avisar_error_critico(
            "Fallo de autenticación con Beds24 (resumen diario)",
            f"No se pudo conectar con Beds24 para generar el resumen de hoy ({e}). "
            f"El resumen se enviará sin entradas/salidas hasta que se resuelva. "
            f"Revisa BEDS24_REFRESH_TOKEN en Render."
        )

    entradas_beds24 = obtener_bookings_dia_beds24(hoy_iso, tipo="checkin")
    salidas_beds24  = obtener_bookings_dia_beds24(hoy_iso, tipo="checkout")
    partes_recibidos = obtener_partes_recibidos_hoy()

    hoy_fmt = hoy.strftime("%d/%m/%Y")
    lineas = [f"🏨 ALCHOMES — {hoy_fmt} · {hora_str}:00h"]

    lineas.append("\n✅ ENTRADAS HOY:")
    if entradas_beds24:
        for e in entradas_beds24:
            parte_ok = (e["room_id"], hoy_iso) in partes_recibidos
            estado = "📄 parte recibido" if parte_ok else "⚠️ parte PENDIENTE"
            lineas.append(f"• {e['nombre_habitacion']} ({estado})")
    else:
        lineas.append("• (ninguna)")

    lineas.append("\n🚪 SALIDAS HOY:")
    if salidas_beds24:
        for s in salidas_beds24:
            lineas.append(f"• {s['nombre_habitacion']}")
    else:
        lineas.append("• (ninguna)")

    return "\n".join(lineas)


def enviar_whatsapp_callmebot(mensaje):
    """
    Envía un mensaje WhatsApp via CallMeBot a uno o dos números.
    Requiere CALLMEBOT_PHONE y CALLMEBOT_API_KEY en Render.
    Opcionalmente CALLMEBOT_PHONE_2 y CALLMEBOT_API_KEY_2 para el segundo destinatario.
    """
    if not CALLMEBOT_PHONE or not CALLMEBOT_API_KEY:
        raise Exception(
            "Faltan variables de entorno CALLMEBOT_PHONE y/o CALLMEBOT_API_KEY. "
            "Añádelas en Render → Environment."
        )

    def _enviar(phone, apikey):
        url = (
            f"https://api.callmebot.com/whatsapp.php"
            f"?phone={phone}"
            f"&text={quote(mensaje)}"
            f"&apikey={apikey}"
        )
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            raise Exception(f"CallMeBot {phone} respondió {resp.status_code}: {resp.text[:300]}")
        logger.info(f"CallMeBot OK ({phone}): {resp.text[:120]}")
        return resp.text

    resultados = []
    resultados.append(_enviar(CALLMEBOT_PHONE, CALLMEBOT_API_KEY))

    if CALLMEBOT_PHONE_2 and CALLMEBOT_API_KEY_2:
        try:
            resultados.append(_enviar(CALLMEBOT_PHONE_2, CALLMEBOT_API_KEY_2))
        except Exception as e:
            logger.error(f"Error enviando al segundo número: {e}")
            resultados.append(f"ERROR número 2: {e}")

    return " | ".join(resultados)


# ── Rutas ──────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    token_ok = bool(GOOGLE_REFRESH_TOKEN)
    callmebot_ok = bool(CALLMEBOT_PHONE and CALLMEBOT_API_KEY)
    return jsonify({
        "status": "ok",
        "servicio": "Hostal PDF Extractor",
        "gmail_autorizado": token_ok,
        "callmebot_configurado": callmebot_ok,
    }), 200

@app.route("/test", methods=["GET"])
def test_page():
    return render_template_string(TEST_PAGE)

@app.route("/oauth/inicio", methods=["GET"])
def oauth_inicio():
    """Redirige a Google para autorizar acceso a Gmail."""
    from urllib.parse import urlencode
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         "https://www.googleapis.com/auth/gmail.readonly",
        "access_type":   "offline",
        "prompt":        "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return redirect(url)

@app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Google redirige aquí con el código de autorización."""
    code  = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"<h2>Error: {error}</h2>", 400
    if not code:
        return "<h2>No se recibió código de autorización</h2>", 400
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=15)
    tokens = resp.json()
    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        return f"<h2>Error obteniendo refresh_token</h2><pre>{json.dumps(tokens, indent=2)}</pre>", 400
    return f"""<h2>✅ Autorización completada</h2>
    <p>Copia este valor y añádelo en Render como variable de entorno:</p>
    <p><strong>GOOGLE_REFRESH_TOKEN</strong></p>
    <pre style="background:#f0f0f0;padding:1rem;border-radius:8px;word-break:break-all">{refresh_token}</pre>
    <p>Una vez guardado en Render, el servidor podrá acceder a Gmail automáticamente.</p>"""

@app.route("/debug-mensaje", methods=["POST"])
def debug_mensaje():
    """Devuelve la estructura completa del mensaje para depuración."""
    data = request.get_json(force=True)
    if data.get("token") != TEST_TOKEN:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    try:
        access_token = get_access_token()
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{data['message_id']}"
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        msg = resp.json()
        def resumir_partes(parts, nivel=0):
            resultado = []
            for p in parts:
                item = {
                    "mimeType": p.get("mimeType",""),
                    "filename": p.get("filename",""),
                    "hasAttachmentId": bool(p.get("body",{}).get("attachmentId")),
                    "bodySize": p.get("body",{}).get("size",0),
                }
                subparts = p.get("parts",[])
                if subparts:
                    item["parts"] = resumir_partes(subparts, nivel+1)
                resultado.append(item)
            return resultado
        payload = msg.get("payload",{})
        estructura = {
            "mimeType": payload.get("mimeType",""),
            "filename": payload.get("filename",""),
            "hasAttachmentId": bool(payload.get("body",{}).get("attachmentId")),
            "parts": resumir_partes(payload.get("parts",[]))
        }
        return jsonify({"ok": True, "estructura": estructura}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/extraer-test", methods=["POST"])
def extraer_test():
    data = request.get_json(force=True)
    if data.get("token") != TEST_TOKEN:
        return jsonify({"ok": False, "error": "Contraseña incorrecta"}), 401
    if "pdf_base64" not in data:
        return jsonify({"ok": False, "error": "Falta pdf_base64"}), 400
    try:
        pdf_bytes = base64.b64decode(data["pdf_base64"])
    except Exception as e:
        return jsonify({"ok": False, "error": f"Base64 inválido: {e}"}), 400
    r = procesar_pdf_bytes(pdf_bytes, data.get("pdf_filename", "documento.pdf"), incluir_texto=True)
    if r["error"]: return jsonify({"ok": False, "error": r["error"]}), 500
    return jsonify({"ok": True, "habitacion": r["habitacion"], "email": r["email"],
                    "fecha_entrada": r["fecha_entrada"], "fecha_salida": r["fecha_salida"],
                    "texto_extraido": r["texto_extraido"]}), 200

@app.route("/extraer", methods=["POST"])
def extraer():
    """
    Modo A (message_id): { "message_id": "...", "token": "..." }
    Modo B (base64):     { "pdf_base64": "...", "pdf_filename": "...", "token": "..." }
    """
    data = request.get_json(force=True)
    if API_TOKEN and data.get("token") != API_TOKEN:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    pdf_filename = data.get("pdf_filename", "documento.pdf")
    cuerpo_email = ""

    if "message_id" in data:
        try:
            access_token = get_access_token()
            pdf_bytes, fn, cuerpo_email = descargar_adjunto_gmail(data["message_id"], access_token)
            if fn: pdf_filename = fn
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    elif "pdf_base64" in data:
        try:
            pdf_bytes = base64.b64decode(data["pdf_base64"])
        except Exception as e:
            return jsonify({"ok": False, "error": f"Base64 inválido: {e}"}), 400
    else:
        return jsonify({"ok": False, "error": "Falta message_id o pdf_base64"}), 400

    r = procesar_pdf_bytes(pdf_bytes, pdf_filename, incluir_texto=True)
    if r["error"]: return jsonify({"ok": False, "error": r["error"]}), 500

    logger.info(f"OK → hab={r['habitacion']} email={r['email']} entrada={r['fecha_entrada']} salida={r['fecha_salida']}")

    # Envío del código de puerta a través de Booking.com Messages (Beds24)
    # Combinamos el texto del PDF con el cuerpo del email, ya que el localizador
    # (identificador fiable de la habitación real) viene en el cuerpo del email.
    texto_para_detectar = (r.get("texto_extraido") or "") + "\n" + cuerpo_email
    beds24_resultado = enviar_codigo_puerta_beds24(
        habitacion_texto=r["habitacion"],
        texto_completo=texto_para_detectar,
        fecha_entrada=r["fecha_entrada"],
        nombre_cliente=r.get("nombre"),
    )

    return jsonify({"ok": True, "habitacion": r["habitacion"], "email": r["email"],
                    "fecha_entrada": r["fecha_entrada"], "fecha_salida": r["fecha_salida"],
                    "codigo_puerta": beds24_resultado}), 200


@app.route("/resumen", methods=["GET", "POST"])
def resumen_whatsapp():
    """
    Genera y (opcionalmente) envía el resumen diario por WhatsApp via CallMeBot.

    GET  (on-demand / test sin enviar):
        /resumen?token=Alchomes2025&enviar=0
        /resumen?token=Alchomes2025&enviar=1&hora=09

    POST (Make.com scheduler — 9h y 21h):
        { "token": "Alchomes2025", "hora": "09" }
        { "token": "Alchomes2025", "hora": "21" }

    Respuesta:
        { "ok": true, "mensaje": "...", "enviado": true/false,
          "callmebot_resp": "...", "error_whatsapp": "..." }
    """
    if request.method == "POST":
        data   = request.get_json(force=True) or {}
        token  = data.get("token", "")
        enviar = data.get("enviar", True)   # por defecto SÍ envía en POST (Make)
        hora   = data.get("hora")
    else:
        token  = request.args.get("token", "")
        enviar = request.args.get("enviar", "1") == "1"
        hora   = request.args.get("hora")

    # Acepta API_TOKEN (Alchomes2025) o TEST_TOKEN (test1234)
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    try:
        mensaje = generar_mensaje_resumen(hora)
    except GmailAuthError as e:
        logger.error(f"Gmail auth caducado: {e}")
        aviso = (
            "⚠️ ALCHOMES — Error de sistema\n\n"
            "El acceso a Gmail ha caducado.\n"
            "El resumen diario NO se está enviando.\n\n"
            "Renueva el acceso en:\n"
            "hostal-pdf-extractor.onrender.com/oauth/inicio"
        )
        try:
            enviar_whatsapp_callmebot(aviso)
        except Exception as we:
            logger.error(f"Error enviando aviso WhatsApp: {we}")
        return jsonify({"ok": False, "error": str(e), "aviso_enviado": True}), 500
    except Exception as e:
        logger.error(f"Error generando resumen: {e}")
        return jsonify({"ok": False, "error": f"Error generando resumen: {e}"}), 500

    resultado = {"ok": True, "mensaje": mensaje, "enviado": False}

    if enviar:
        try:
            cb_resp = enviar_whatsapp_callmebot(mensaje)
            resultado["enviado"] = True
            resultado["callmebot_resp"] = cb_resp[:300]
        except Exception as e:
            logger.error(f"Error enviando WhatsApp: {e}")
            resultado["enviado"] = False
            resultado["error_whatsapp"] = str(e)

    return jsonify(resultado), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


# ── Debug visual ────────────────────────────────────────────────────────────────
DEBUG_PAGE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><title>Debug Gmail</title>
<style>body{font-family:system-ui;padding:2rem;max-width:600px;margin:auto}
input{width:100%;padding:.5rem;margin:.5rem 0 1rem;border:1px solid #ccc;border-radius:6px}
button{padding:.6rem 1.5rem;background:#5c2d91;color:#fff;border:none;border-radius:6px;cursor:pointer}
pre{background:#f4f4f4;padding:1rem;border-radius:6px;white-space:pre-wrap;word-break:break-all;font-size:.8rem}
</style></head><body>
<h2>🔍 Debug estructura email Gmail</h2>
<label>Test token</label><input id="tok" value="test1234"/>
<label>Message ID (del email original de registroparteviajeros.com)</label>
<input id="mid" placeholder="ej: 19eac43cf2de581f"/>
<button onclick="run()">Analizar</button>
<pre id="out">Resultado aparecerá aquí...</pre>
<script>
async function run(){
  const out=document.getElementById('out');
  out.textContent='Consultando...';
  try{
    const r=await fetch('/debug-mensaje',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message_id:document.getElementById('mid').value,
        token:document.getElementById('tok').value})});
    const d=await r.json();
    out.textContent=JSON.stringify(d,null,2);
  }catch(e){out.textContent='Error: '+e;}
}
</script></body></html>"""

@app.route("/probar-ultimo-parte", methods=["GET"])
def probar_ultimo_parte():
    """
    Endpoint de prueba manual: busca el email de 'Parte de viajeros' más reciente
    en Gmail y ejecuta el mismo flujo que /extraer (extracción + envío del código
    de puerta por Beds24), sin necesitar Make ni conocer el message_id.

    Uso (modo simulación, no envía nada al cliente):
        /probar-ultimo-parte?token=Alchomes2025

    Uso (envío real, solo si estás seguro):
        /probar-ultimo-parte?token=Alchomes2025&enviar=1

    Uso (envío real SIN enlace ni teléfono, para diagnosticar bloqueos de Booking.com):
        /probar-ultimo-parte?token=Alchomes2025&enviar=1&minimo=1

    Uso (envío real con sendEmail=true, última variante de diagnóstico):
        /probar-ultimo-parte?token=Alchomes2025&enviar=1&con_email=1
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    dry_run = request.args.get("enviar", "0") != "1"
    version_minima = request.args.get("minimo", "0") == "1"
    con_email = request.args.get("con_email", "0") == "1"

    try:
        access_token = get_access_token()
    except GmailAuthError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error de autenticación Gmail: {e}"}), 500

    try:
        message_ids = buscar_message_ids_gmail(access_token, max_results=1)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error buscando en Gmail: {e}"}), 500

    if not message_ids:
        return jsonify({"ok": False, "error": "No se encontró ningún email de 'Parte de viajeros' en los últimos 90 días"}), 404

    message_id = message_ids[0]

    try:
        pdf_bytes, pdf_filename, cuerpo_email = descargar_adjunto_gmail(message_id, access_token)
    except Exception as e:
        return jsonify({"ok": False, "message_id": message_id, "error": f"Error descargando adjunto: {e}"}), 500

    r = procesar_pdf_bytes(pdf_bytes, pdf_filename, incluir_texto=True)
    if r["error"]:
        return jsonify({"ok": False, "message_id": message_id, "error": r["error"]}), 500

    texto_para_detectar = (r.get("texto_extraido") or "") + "\n" + cuerpo_email
    room_id_detectado = detectar_room_id(r["habitacion"], texto_para_detectar)
    localizador = extraer_localizador(texto_para_detectar)

    beds24_resultado = enviar_codigo_puerta_beds24(
        habitacion_texto=r["habitacion"],
        texto_completo=texto_para_detectar,
        fecha_entrada=r["fecha_entrada"],
        nombre_cliente=r.get("nombre"),
        dry_run=dry_run,
        version_minima=version_minima,
        enviar_tambien_email=con_email,
    )

    return jsonify({
        "ok": True,
        "modo": "SIMULACIÓN (no se envió nada)" if dry_run else "ENVÍO REAL",
        "message_id": message_id,
        "pdf_filename": pdf_filename,
        "habitacion_texto_pdf": r["habitacion"],
        "localizador_detectado": localizador,
        "room_id_detectado": room_id_detectado,
        "nombre_habitacion_real": ROOM_CONFIG.get(room_id_detectado, {}).get("nombre") if room_id_detectado else None,
        "email_cliente": r["email"],
        "fecha_entrada": r["fecha_entrada"],
        "fecha_salida": r["fecha_salida"],
        "codigo_puerta": beds24_resultado,
    }), 200


@app.route("/ver-mensajes-beds24", methods=["GET"])
def ver_mensajes_beds24():
    """
    Diagnóstico: consulta directamente en Beds24 el historial de mensajes
    de una reserva concreta, para confirmar si un mensaje se registró/envió
    realmente (y ver el estado que reporta el canal).

    Uso:
        /ver-mensajes-beds24?token=Alchomes2025&book_id=89432182
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    book_id = request.args.get("book_id", "")
    if not book_id:
        return jsonify({"ok": False, "error": "Falta el parámetro book_id"}), 400

    try:
        access_token = get_beds24_access_token()
        resp = requests.get(
            f"{BEDS24_API_BASE}/bookings/messages",
            headers={"token": access_token, "accept": "application/json"},
            params={"bookingId": book_id},
            timeout=15,
        )
        resp.raise_for_status()
        return jsonify({"ok": True, "book_id": book_id, "mensajes": resp.json()}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/ver-reservas-dia-beds24", methods=["GET"])
def ver_reservas_dia_beds24():
    """
    Diagnóstico: muestra la respuesta cruda de Beds24 al consultar reservas
    de un día concreto, para entender por qué el resumen devuelve resultados
    duplicados/excesivos (posible problema con el filtro de fechas o paginación).

    Uso:
        /ver-reservas-dia-beds24?token=Alchomes2025&fecha=2026-07-08&tipo=checkin
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    fecha = request.args.get("fecha", date.today().isoformat())
    tipo = request.args.get("tipo", "checkin")

    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    params = {"propertyId": BEDS24_PROPERTY_ID}
    if tipo == "checkin":
        params["checkInFrom"] = fecha
        params["checkInTo"] = fecha
    else:
        params["checkOutFrom"] = fecha
        params["checkOutTo"] = fecha

    try:
        resp = requests.get(
            f"{BEDS24_API_BASE}/bookings",
            headers={"token": access_token, "accept": "application/json"},
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    data = raw.get("data", [])
    resumen_items = [
        {
            "id": b.get("id"),
            "roomId": b.get("roomId"),
            "checkIn": b.get("arrival") or b.get("checkIn") or b.get("firstNight"),
            "checkOut": b.get("departure") or b.get("checkOut") or b.get("lastNight"),
            "status": b.get("status"),
        }
        for b in data
    ]

    return jsonify({
        "ok": True,
        "params_enviados": params,
        "total_items_data": len(data),
        "pages_info": raw.get("pages"),
        "primer_item_SIN_PROCESAR": data[0] if data else None,
        "primeros_10_items": resumen_items[:10],
        "todos_los_items_resumidos": resumen_items,
    }), 200


@app.route("/debug", methods=["GET"])
def debug_page():
    return render_template_string(DEBUG_PAGE)

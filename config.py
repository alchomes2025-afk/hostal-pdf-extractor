"""
config.py — Configuración central: variables de entorno, constantes de
habitaciones/RPV, cliente de Firestore y reservas ficticias de prueba.

Todo lo que antes vivía como globals sueltos al principio de app.py se
centraliza aquí para que el resto de módulos (services/, routes/) importen
desde un único sitio.
"""
import os
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GmailAuthError(Exception):
    """Se lanza cuando el refresh token de Gmail ha caducado o es inválido."""
    pass


# ════════════════════════════════════════════════════════════════════════════
# FIRESTORE — Histórico de interacciones y conversaciones por reserva
# ════════════════════════════════════════════════════════════════════════════
FIRESTORE_CREDENTIALS_JSON = os.environ.get("FIRESTORE_CREDENTIALS_JSON", "")
db = None
firestore = None
if FIRESTORE_CREDENTIALS_JSON:
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account
        _cred_info = json.loads(FIRESTORE_CREDENTIALS_JSON)
        _credentials = service_account.Credentials.from_service_account_info(_cred_info)
        db = firestore.Client(credentials=_credentials, project=_cred_info.get("project_id"))
        logger.info("[Firestore] Cliente inicializado correctamente")
    except Exception as e:
        logger.error(f"[Firestore] Error inicializando cliente: {e}")
        db = None
else:
    logger.warning("[Firestore] FIRESTORE_CREDENTIALS_JSON no configurada — histórico desactivado")


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
BEDS24_PROPERTY_ID   = os.environ.get("BEDS24_PROPERTY_ID", "339751")  # ALC Homes San Blas
BEDS24_PROPERTY_ID_CASA_PRIMAVERA = os.environ.get("BEDS24_PROPERTY_ID_CASA_PRIMAVERA", "349341")
# Lista de TODAS las propiedades en las que buscar una reserva por su número —
# el huésped no indica de qué propiedad es, así que el sistema las recorre todas.
BEDS24_PROPERTY_IDS  = [BEDS24_PROPERTY_ID, BEDS24_PROPERTY_ID_CASA_PRIMAVERA]
BEDS24_API_BASE      = "https://beds24.com/api/v2"

# PINs de acceso por habitación (se editan solo aquí, en Render → Environment)
PIN_HABITACION_2 = os.environ.get("PIN_HABITACION_2", "")  # Playa del Albir
PIN_HABITACION_3 = os.environ.get("PIN_HABITACION_3", "")  # Cala del Moraig
PIN_DOBLE        = os.environ.get("PIN_DOBLE", "")         # Playa de la Fossá
PIN_DELUXE       = os.environ.get("PIN_DELUXE", "")        # Cala Coveta Fumá
PIN_CASA_PRIMAVERA = os.environ.get("PIN_CASA_PRIMAVERA", "2486")  # Código del cajetín de llaves

# Configuración de las 5 habitaciones: roomId de Beds24 → nombre + PIN + palabras clave
# para detectar a qué habitación corresponde un parte de viajero (buscando en el
# nombre del archivo y en el texto extraído del PDF, sin acentos gracias a normalizar()).
ROOM_CONFIG = {
    "702397": {"nombre": "Playa Lanuza",       "pin": None,             "keywords": ["lanuza"]},
    "702398": {"nombre": "Playa del Albir",    "pin": PIN_HABITACION_2, "keywords": ["albir"]},
    "702399": {"nombre": "Cala del Moraig",    "pin": PIN_HABITACION_3, "keywords": ["moraig"]},
    "702396": {"nombre": "Playa de la Fossá",  "pin": PIN_DOBLE,        "keywords": ["fossa"]},
    "702395": {"nombre": "Cala Coveta Fumá",   "pin": PIN_DELUXE,       "keywords": ["coveta", "fuma"]},
    # La Casa de la Primavera — Gran Alacant (propiedad Beds24 349341).
    # "pin" aquí es el código del cajetín de llaves (no una cerradura electrónica).
    "720841": {"nombre": "La Casa de la Primavera", "pin": PIN_CASA_PRIMAVERA, "keywords": ["primavera"]},
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

# ── Links de registroparteviajeros.com por room_id de Beds24 ─────────────
RPV_LINKS = {
    "702397": "https://app.registroparteviajeros.com/propiedad/hliPDoDTb9",  # Hab 1 · Playa Lanuza
    "702398": "https://app.registroparteviajeros.com/propiedad/CgbPrarDLi",  # Hab 2 · Playa del Albir
    "702399": "https://app.registroparteviajeros.com/propiedad/d1ydBdUSOr",  # Hab 3 · Cala del Moraig
    "702396": "https://app.registroparteviajeros.com/propiedad/MRgkbMeAt7",  # Hab 4 · Playa de la Fossá
    "702395": "https://app.registroparteviajeros.com/propiedad/YQGCsngJaN",  # Hab 5 · Cala Coveta Fumá
}

# ── Configuración: API de registroparteviajeros ───────────────────────
RPV_API_KEY = os.environ.get("RPV_API_KEY", "")  # Cuenta RPV del hostal (ALC Homes San Blas)
RPV_API_KEY_CASA_PRIMAVERA = os.environ.get("RPV_API_KEY_CASA_PRIMAVERA", "")  # Cuenta RPV distinta, propia de La Casa de la Primavera
RPV_API_URL = "https://app.registroparteviajeros.com/api/v1/usuarios"

# Mapeo: room_id de Beds24 → prop_id real de registroparteviajeros
# (se obtiene de la sección API → Código de Propiedad del panel de RPV)
RPV_PROPERTY_MAP = {
    "702397": "prop_W17mVBrVyrGinzWt4VvGmw",   # Playa Lanuza        (Hab 1)
    "702398": "prop_W17mVBrVyrGinzWs_jN1-Q",   # Playa del Albir     (Hab 2)
    "702399": "prop_W17mVBrVyrGinzWj80wzxw",    # Cala del Moraig     (Hab 3)
    "702396": "prop_W17mVBrVyrGinzWivGS55Q",    # Playa de la Fossá   (Hab 4)
    "702395": "prop_W17mVBrVyrGinzWhTOByeA",    # Cala Coveta Fumá    (Hab 5)
    "720841": "prop_W1w0u47vzIWL-biD4feb1Q",    # La Casa de la Primavera
}

# Mapeo: room_id → qué API key de RPV usar (algunas propiedades tienen cuenta
# de RPV propia, distinta de la del hostal). Si un room_id no aparece aquí,
# se usa RPV_API_KEY (la cuenta del hostal) por defecto.
RPV_API_KEY_MAP = {
    "720841": RPV_API_KEY_CASA_PRIMAVERA,  # La Casa de la Primavera — cuenta RPV propia
}

GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL    = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL_PRI  = "openai/gpt-oss-120b"
GROQ_MODEL_FALL = "openai/gpt-oss-20b"

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

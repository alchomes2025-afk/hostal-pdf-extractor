"""
services/rooms.py — Detección de la habitación real (roomId de Beds24) a
partir del texto de un parte de viajero / cuerpo de email.
"""
import re
import logging

from config import ROOM_CONFIG

logger = logging.getLogger(__name__)


def normalizar(texto):
    t = texto.lower()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")]:
        t = t.replace(a, b)
    t = t.replace("habitación","habitacion")
    t = re.sub(r"[^a-z0-9 ]","",t).strip()
    return t


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

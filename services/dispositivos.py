"""
services/dispositivos.py — A quién pertenece cada dispositivo (solo para el PIN compartido del propietario: 1379 = Ione o Adrián).

El 1379 lo usan Ione y Adrián, cada uno desde sus dispositivos. La primera vez que un dispositivo entra con ese PIN la app pregunta «¿Eres Ione o Adrián?»
(solo esas dos opciones) y el servidor lo recuerda: a partir de entonces ese dispositivo entra ya identificado y la sesión lleva el nombre. El dispositivo lo
identifica el `deviceId` (UUID que la app genera y guarda en el dispositivo). No es una barrera de seguridad —el PIN ya da el mismo acceso a los dos—, sino
saber quién hizo cada cosa.

Estado en Firestore: colección `dispositivos_acceso`, un documento por dispositivo: {nombre, perfil, creado, ultimo_uso}. Sin Firestore: nadie queda
identificado y registrar devuelve None. Nada de lo de aquí lanza excepción hacia arriba.
"""
import logging
import re
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)

COLECCION = "dispositivos_acceso"
_ID_VALIDO = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _ref(device_id):
    device_id = str(device_id or "").strip()
    if config.db is None or not _ID_VALIDO.match(device_id):
        return None
    return config.db.collection(COLECCION).document(device_id)


def buscar(device_id, perfil):
    """El nombre con el que se registró ese dispositivo para ese perfil, o None."""
    try:
        ref = _ref(device_id)
        if ref is None:
            return None
        doc = ref.get()
        datos = (doc.to_dict() or {}) if doc.exists else {}
        if datos.get("perfil") == perfil.id and datos.get("nombre") in perfil.identidad_por_dispositivo:
            return datos["nombre"]
        return None
    except Exception as e:
        logger.error(f"[dispositivos] No se pudo leer el dispositivo: {e}")
        return None


def registrar(device_id, nombre, perfil):
    """Asocia el dispositivo a una de las personas del perfil. Devuelve el nombre guardado o None (dispositivo, nombre o base de datos no válidos)."""
    try:
        ref = _ref(device_id)
        if ref is None or nombre not in perfil.identidad_por_dispositivo:
            return None
        ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        previo = ref.get()
        creado = (previo.to_dict() or {}).get("creado", ahora) if previo.exists else ahora
        ref.set({"nombre": nombre, "perfil": perfil.id, "creado": creado, "ultimo_uso": ahora})
        return nombre
    except Exception as e:
        logger.error(f"[dispositivos] No se pudo registrar el dispositivo: {e}")
        return None

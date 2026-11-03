"""
services/rpv_webhook.py — Aviso en tiempo real de RPV cuando el parte de una reserva queda completo
(evento `guest_registration.completed`, enviado por Svix a nuestra URL).

El aviso NO se toma como dato: solo dice «mira RPV ahora». Al recibirlo se descarta la copia en memoria de esa
cuenta (services/rpv.py) y se vuelve a consultar /partes, así la web de check-in y los avisos ven el parte
completado al momento en vez de esperar al sondeo. Un aviso falso (no puede haberlo: va firmado) solo provocaría
una consulta de más, limitada por el límite local de peticiones. El sondeo normal sigue como respaldo.

Firma (Svix): cabeceras svix-id, svix-timestamp y svix-signature («v1,<base64>» separadas por espacios);
HMAC-SHA256 de «id.timestamp.cuerpo» con el secreto «whsec_<base64>» de ESA propiedad. Cada habitación tiene su
propio secreto: variables de entorno RPV_WEBHOOK_SECRET_<room_id> (nunca en el repo).
"""
import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from collections import deque

from config import RPV_PROPERTY_MAP
from services import rpv

logger = logging.getLogger(__name__)

TOLERANCIA_SEGUNDOS = 5 * 60
MAX_RECORDADOS = 500

_vistos = {}                 # id del evento -> epoch; deduplica los reenvíos de Svix
_ultimos = deque(maxlen=50)  # constancia en memoria para /webhook-rpv/ultimos (sin datos personales)


class FirmaInvalida(Exception):
    pass


def _secretos():
    """{room_id: secreto whsec_…} de las habitaciones que lo tienen configurado."""
    secretos = {}
    for room_id in RPV_PROPERTY_MAP:
        valor = os.environ.get(f"RPV_WEBHOOK_SECRET_{room_id}", "").strip()
        if valor:
            secretos[room_id] = valor
    return secretos


def _clave(secreto):
    texto = secreto[len("whsec_"):] if secreto.startswith("whsec_") else secreto
    return base64.b64decode(texto)


def verificar_firma(cuerpo, cabeceras, ahora=None):
    """Devuelve el room_id cuyo secreto firmó el aviso, o lanza FirmaInvalida. `cuerpo` son los bytes tal cual llegaron."""
    msg_id, marca, firma = (cabeceras.get(h, "") for h in ("svix-id", "svix-timestamp", "svix-signature"))
    if not (msg_id and marca and firma):
        raise FirmaInvalida("faltan cabeceras de firma")
    try:
        if abs((ahora if ahora is not None else time.time()) - int(marca)) > TOLERANCIA_SEGUNDOS:
            raise FirmaInvalida("marca de tiempo fuera de tolerancia")
    except ValueError:
        raise FirmaInvalida("marca de tiempo no válida")
    firmas = [p.split(",", 1)[1] for p in firma.split() if p.startswith("v1,") and "," in p]
    contenido = f"{msg_id}.{marca}.".encode() + cuerpo
    for room_id, secreto in _secretos().items():
        try:
            esperada = base64.b64encode(hmac.new(_clave(secreto), contenido, hashlib.sha256).digest()).decode()
        except Exception:
            logger.error(f"[webhook-rpv] El secreto de la habitación {room_id} no es válido (¿se copió entero?)")
            continue
        if any(hmac.compare_digest(esperada, f) for f in firmas):
            return room_id
    raise FirmaInvalida("firma no válida")


def _refrescar(room_id):
    try:
        rpv.refrescar_cuenta(room_id)
    except Exception as e:
        logger.error(f"[webhook-rpv] No se pudo refrescar RPV tras el aviso: {e}")


def procesar(evento, refrescar_en_segundo_plano=True):
    """
    Trata un aviso ya autenticado. Devuelve {"resultado": "refrescado"|"repetido"|"prueba"|"ignorado", ...}.
    Siempre se contesta 2xx a Svix salvo firma inválida, para que no reintente algo que no vamos a hacer.
    """
    ahora = time.time()
    for k in [k for k, t in _vistos.items() if ahora - t > 24 * 3600]:
        del _vistos[k]
    if len(_vistos) > MAX_RECORDADOS:
        _vistos.clear()
    evento_id = str(evento.get("id") or "")
    datos = evento.get("data") if isinstance(evento.get("data"), dict) else {}
    reserva = datos.get("booking") if isinstance(datos.get("booking"), dict) else {}
    guests = datos.get("guests") if isinstance(datos.get("guests"), dict) else {}
    prop_a_room = {prop: room for room, prop in RPV_PROPERTY_MAP.items()}
    room_id = prop_a_room.get(datos.get("property_id"))
    resumen = {"t": ahora, "id": evento_id, "tipo": evento.get("type"), "habitacion": room_id or "desconocida",
               "entrada": reserva.get("check_in"), "completo": datos.get("complete"),
               "registrados": guests.get("registered"), "esperados": guests.get("expected")}

    if evento.get("type") != "guest_registration.completed":
        resultado = "ignorado"
    elif evento.get("test"):
        resultado = "prueba"
    elif evento_id and evento_id in _vistos:
        resultado = "repetido"
    elif not room_id:
        resultado = "ignorado"
        logger.warning(f"[webhook-rpv] Aviso de una propiedad que no conocemos: {datos.get('property_id')}")
    else:
        if evento_id:
            _vistos[evento_id] = ahora
        resultado = "refrescado"
        logger.info(f"[webhook-rpv] Parte completado: habitación {room_id}, entrada {reserva.get('check_in')} "
                    f"({guests.get('registered')}/{guests.get('expected')})")
        if refrescar_en_segundo_plano:
            threading.Thread(target=_refrescar, args=(room_id,), daemon=True).start()
        else:
            _refrescar(room_id)
    resumen["resultado"] = resultado
    _ultimos.appendleft(resumen)
    return {"resultado": resultado}


def ultimos(limite=50):
    return list(_ultimos)[:limite]

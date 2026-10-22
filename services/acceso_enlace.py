"""
services/acceso_enlace.py — Enlace personal de acceso a la web de check-in.

Problema que resuelve: pedir al huésped que escriba su nombre «exactamente así» le obliga a repetir una errata
si la reserva se hizo mal escrita (o la hizo otra persona), y el número de reserva de Beds24 es casi correlativo
y se puede adivinar. En su lugar, el huésped recibe (por correo, o al encontrarse su reserva en la web) un enlace
con un TOKEN FIRMADO: `https://alc-homes-checkin.web.app/?t=<token>`. Abre directamente su reserva, sin teclear
nada, y la web lo recuerda en su dispositivo para las siguientes veces.

El token es `base64url(id de la reserva en Beds24)` + `.` + firma HMAC-SHA256 (128 bits) con la clave secreta
CHECKIN_LINK_SECRET. No guarda estado: se verifica recalculando la firma. No lleva datos personales y sin la clave no
se puede fabricar ni adivinar. Si la clave no está configurada en Render, el sistema funciona como antes (sin
enlaces personales) y `habilitado()` es False. Cambiar la clave invalida todos los enlaces ya enviados.

Un token identifica la reserva igual que acertar el nombre: NO salta ninguna comprobación. El código de la habitación
solo se muestra, como siempre, con el parte completado y a partir de las 15:00 (ver routes/checkin.py).
"""
import base64
import hashlib
import hmac

from config import CHECKIN_LINK_SECRET

CHECKIN_URL = "https://alc-homes-checkin.web.app/"
TELEFONO = "+34 622 38 35 87"
IDIOMAS = ("es", "en", "fr", "de", "val")


def habilitado():
    return bool(CHECKIN_LINK_SECRET)


def _b64(datos):
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def _firma(book_id):
    return _b64(hmac.new(CHECKIN_LINK_SECRET.encode(), f"checkin-link:{book_id}".encode(), hashlib.sha256).digest()[:16])


def crear_token(book_id):
    """Token del enlace personal de esa reserva, o None si no hay clave configurada."""
    if not habilitado() or book_id in (None, ""):
        return None
    book_id = str(book_id)
    return f"{_b64(book_id.encode())}.{_firma(book_id)}"


def verificar_token(token):
    """El id de reserva (str) si el token es auténtico; None si está mal formado, manipulado o no hay clave."""
    if not habilitado() or not isinstance(token, str) or token.count(".") != 1 or len(token) > 120:
        return None
    parte_id, firma = token.split(".")
    try:
        book_id = base64.urlsafe_b64decode(parte_id + "=" * (-len(parte_id) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not book_id.isdigit() or not hmac.compare_digest(firma, _firma(book_id)):
        return None
    return book_id


def _firma_hab(book_id):
    return _b64(hmac.new(CHECKIN_LINK_SECRET.encode(), f"checkin-room:{book_id}".encode(), hashlib.sha256).digest()[:16])


def crear_token_habitacion(book_id):
    """Token del enlace de UNA habitación de una reserva de varias: abre solo esa habitación (ver services/grupos_acceso.py).
    Lleva la «h» delante del id y otro dominio de firma, así que un token de habitación nunca vale como token de la reserva ni al revés."""
    if not habilitado() or book_id in (None, ""):
        return None
    book_id = str(book_id)
    return f"{_b64(('h' + book_id).encode())}.{_firma_hab(book_id)}"


def interpretar_token(token):
    """(id de reserva, solo_habitacion) si el token es auténtico, de la reserva (False) o de una habitación (True); (None, False) si no."""
    book_id = verificar_token(token)
    if book_id:
        return book_id, False
    if not habilitado() or not isinstance(token, str) or token.count(".") != 1 or len(token) > 120:
        return None, False
    parte_id, firma = token.split(".")
    try:
        crudo = base64.urlsafe_b64decode(parte_id + "=" * (-len(parte_id) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None, False
    if not crudo.startswith("h") or not crudo[1:].isdigit() or not hmac.compare_digest(firma, _firma_hab(crudo[1:])):
        return None, False
    return crudo[1:], True


def url_checkin(book_id=None, idioma=None):
    """Enlace a la web de check-in: el personal de esa reserva si se puede (con el idioma del huésped), o el general."""
    token = crear_token(book_id)
    if not token:
        return CHECKIN_URL
    return f"{CHECKIN_URL}?t={token}" + (f"&l={idioma}" if idioma in IDIOMAS else "")

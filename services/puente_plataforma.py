"""
services/puente_plataforma.py — Llamadas al «puente de la plataforma»: un Apps Script de la cuenta de HostalFácil,
publicado como aplicación web, que hace lo que el servidor no puede hacer solo. Mismo patrón que services/email_send.py
(petición POST con un secreto compartido).

Acciones: ping (¿responde y llega a la carpeta?), ocr (texto de una foto o PDF escaneado, con la conversión de Google
Drive: gratis), guardar (archivo en la carpeta del cliente + fila en su hoja de registro) y correo (aviso interno enviado
desde la cuenta de la plataforma, no desde el buzón de huéspedes de ningún cliente).
"""
import base64
import logging

import requests

from config import PUENTE_SECRETO, PUENTE_URL

logger = logging.getLogger(__name__)

TIMEOUT = 90   # el OCR de un PDF de varias páginas puede tardar


class PuenteError(Exception):
    pass


def configurado():
    return bool(PUENTE_URL and PUENTE_SECRETO)


def _llamar(accion, **datos):
    if not configurado():
        raise PuenteError("puente de la plataforma sin configurar (PUENTE_URL / PUENTE_SECRETO)")
    try:
        resp = requests.post(PUENTE_URL, json={"secret": PUENTE_SECRETO, "accion": accion, **datos}, timeout=TIMEOUT)
        data = resp.json()
    except Exception as e:
        raise PuenteError(f"el puente de la plataforma no responde: {e}")
    if not data.get("ok"):
        raise PuenteError(f"el puente de la plataforma ha fallado: {data.get('error')} {data.get('detalle') or ''}".strip())
    return data


def ping(raiz_id):
    """Nombre de la carpeta raíz si el puente responde y la carpeta está compartida con la cuenta de la plataforma."""
    return _llamar("ping", raiz_id=raiz_id).get("raiz")


def ocr(contenido, mime):
    """Texto de una imagen o PDF escaneado."""
    return _llamar("ocr", mime=mime, base64=base64.b64encode(contenido).decode()).get("texto") or ""


def correo(to, asunto, cuerpo):
    """Envía un correo de texto desde la cuenta de la plataforma (hostalfacil@gmail.com)."""
    _llamar("correo", to=to, asunto=asunto, cuerpo=cuerpo)


def guardar(raiz_id, ruta, nombre, mime, contenido, registro=None, descripcion=None):
    """Guarda el archivo en <raiz>/<ruta…>/ y, si se indica, añade una fila a la hoja de registro (la celda
    '{{ENLACE}}' de la fila se sustituye por el enlace del archivo). Devuelve {file_id, url, nombre, hoja_url}."""
    data = _llamar("guardar", raiz_id=raiz_id, ruta=ruta, nombre=nombre, mime=mime,
                   base64=base64.b64encode(contenido).decode(), registro=registro, descripcion=descripcion)
    return {k: data.get(k) for k in ("file_id", "url", "nombre", "hoja_url")}

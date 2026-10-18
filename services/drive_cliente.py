"""
services/drive_cliente.py — Conexión del Drive de cada cliente con la plataforma (primer uso de las facturas).

El cliente da permiso UNA vez desde la app (botón «Conectar mi Drive»): se abre una página de un Apps Script («conector») que se ejecuta con
la identidad DEL CLIENTE; esa página crea en su Drive la carpeta «HostalFácil-Gestión», la comparte con la cuenta de la plataforma
(hostalfacil@gmail.com, editor) y avisa a este backend (POST /mobile/facturas/conectado) con el id de la carpeta. Aquí se comprueba, se
crean las subcarpetas (el puente de la plataforma, que ya tiene acceso) y se guarda el id por cliente en Firestore (`facturas_config`):
así no hace falta ninguna variable de entorno por cliente.

Seguridad: el enlace lleva un token de un solo uso (caduca a los 20 min) que solo puede pedir quien tenga el PIN de administrador, y el
aviso del conector va firmado con el secreto compartido (PUENTE_SECRETO). Un token usado, caducado o desconocido se rechaza.
"""
import hmac
import logging
import re
import secrets
import time
from datetime import datetime

import config
from services import puente_plataforma

logger = logging.getLogger(__name__)

COLECCION_CONFIG = "facturas_config"        # un documento por cliente: {drive_raiz, conectado_por, fecha}
COLECCION_CONEXIONES = "facturas_conexiones"   # un documento por token de conexión
CADUCIDAD_SEG = 20 * 60
TTL_CACHE_SEG = 60
REINTENTOS_ACCESO = 4                       # tras compartir la carpeta, Drive puede tardar unos segundos en dar acceso
_ID_CARPETA = re.compile(r"[A-Za-z0-9_-]{10,100}")
_cache = {}                                 # cliente → (instante, id de la carpeta raíz)


class ConexionError(Exception):
    def __init__(self, mensaje, status=400):
        super().__init__(mensaje)
        self.status = status


def _col(nombre):
    return config.db.collection(nombre) if config.db is not None else None


def raiz(cliente_id, por_defecto=""):
    """Id de la carpeta raíz del Drive del cliente: la guardada al conectar y, si no hay, `por_defecto` (la variable de entorno antigua)."""
    hit = _cache.get(cliente_id)
    if hit and time.time() - hit[0] < TTL_CACHE_SEG:
        return hit[1] or por_defecto
    col = _col(COLECCION_CONFIG)
    valor = ""
    if col is not None:
        try:
            doc = col.document(cliente_id).get()
            valor = ((doc.to_dict() or {}).get("drive_raiz") or "") if doc.exists else ""
        except Exception as e:
            logger.error(f"[drive_cliente] Error leyendo la configuración de {cliente_id}: {e}")
            return por_defecto
    _cache[cliente_id] = (time.time(), valor)
    return valor or por_defecto


def olvidar_cache(cliente_id=None):
    _cache.pop(cliente_id, None) if cliente_id else _cache.clear()


def nuevo_token(cliente_id, usuario=""):
    """Token de un solo uso para el enlace del conector. Lo pide solo el administrador desde la app."""
    col = _col(COLECCION_CONEXIONES)
    if col is None:
        raise ConexionError("no se puede iniciar la conexión ahora mismo (sin base de datos)", 503)
    token = secrets.token_urlsafe(24)
    col.document(token).set({"cliente": cliente_id, "creado": time.time(), "usado": False, "usuario": str(usuario or "")[:40]})
    return token


def completar(token, folder_id, email, secreto, rutas):
    """Llamada del conector tras crear y compartir la carpeta. Comprueba el aviso, crea las subcarpetas y guarda la conexión.
    Devuelve {cliente, carpeta}."""
    if not config.PUENTE_SECRETO or not hmac.compare_digest(str(secreto or "").encode(), config.PUENTE_SECRETO.encode()):
        raise ConexionError("no autorizado", 401)
    folder_id = str(folder_id or "")
    if not _ID_CARPETA.fullmatch(folder_id):
        raise ConexionError("identificador de carpeta no válido")
    col = _col(COLECCION_CONEXIONES)
    if col is None:
        raise ConexionError("sin base de datos", 503)
    doc = col.document(str(token or ""))
    previo = doc.get()
    datos = (previo.to_dict() or {}) if previo.exists else {}
    if not datos:
        raise ConexionError("enlace no válido", 404)
    if datos.get("usado"):
        raise ConexionError("este enlace ya se usó: vuelve a la app y pulsa «Conectar mi Drive» otra vez", 409)
    if time.time() - float(datos.get("creado") or 0) > CADUCIDAD_SEG:
        raise ConexionError("el enlace ha caducado: vuelve a la app y pulsa «Conectar mi Drive» otra vez", 410)

    nombre = None
    for intento in range(REINTENTOS_ACCESO):
        try:
            nombre = puente_plataforma.ping(folder_id)
            break
        except puente_plataforma.PuenteError as e:
            if intento == REINTENTOS_ACCESO - 1:
                logger.error(f"[drive_cliente] La carpeta {folder_id} no es accesible para la plataforma: {e}")
                raise ConexionError("la cuenta de HostalFácil aún no puede ver la carpeta; vuelve a intentarlo en un minuto", 502)
            time.sleep(2)
    try:
        puente_plataforma.estructura(folder_id, rutas)
    except puente_plataforma.PuenteError as e:
        logger.error(f"[drive_cliente] No se pudieron crear las subcarpetas: {e}")
        raise ConexionError("no se pudieron crear las subcarpetas; vuelve a intentarlo", 502)

    cliente_id = datos["cliente"]
    config_col = _col(COLECCION_CONFIG)
    config_col.document(cliente_id).set({"drive_raiz": folder_id, "nombre_carpeta": nombre or "", "conectado_por": str(email or "")[:120],
                                         "fecha": datetime.now().strftime("%Y-%m-%d %H:%M")})
    doc.set({**datos, "usado": True})
    olvidar_cache(cliente_id)
    logger.info(f"[drive_cliente] Drive de {cliente_id} conectado por {email or '?'} (carpeta {nombre})")
    return {"cliente": cliente_id, "carpeta": nombre}

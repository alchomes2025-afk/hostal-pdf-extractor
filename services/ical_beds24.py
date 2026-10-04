"""
services/ical_beds24.py — Vigilancia de los calendarios iCal de Beds24 que
importa RPV (registroparteviajeros.com).

RPV crea las reservas de su listado importando, una vez al día, el iCal de
exportación de cada habitación (api.beds24.com/ical/bookings.ics?roomid=…).
Si Beds24 deja de servirlo (el 2026-10-04 se descubrió que "Export" estaba en
"Disable" en todas las habitaciones y respondía "Error: room synchroniser not
enabled"), RPV no importa nada nuevo, sigue mostrando "Sincronizado" y no avisa:
los huéspedes de las reservas nuevas no pueden hacer el parte.

Este módulo abre los enlaces (los mismos que están en las fichas de RPV, en la
variable de entorno BEDS24_ICAL_URLS) y comprueba que devuelven un calendario
válido. Lo llama /watchdog; un fallo entra en `problemas` y sale por el WhatsApp
con el dedupe habitual.

- Un enlace OK se vuelve a comprobar como mucho cada TTL_OK (55 min): son 6
  llamadas, no hace falta repetirlas en cada pasada de 15 min.
- Un fallo se reintenta enseguida una vez y, si persiste, solo se da por bueno
  tras FALLOS_PARA_AVISAR pasadas seguidas del watchdog (~15 min): así un corte
  de red puntual no manda un WhatsApp de "fallo" y otro de "recuperado".
- Ningún mensaje ni log incluye el enlace: lleva el token. Por eso no se usa el
  texto de las excepciones de `requests`, que sí lo incluye.
"""
import logging
import re
import time

import requests

from config import BEDS24_ICAL_URLS, ROOM_CONFIG

logger = logging.getLogger(__name__)

TTL_OK = 55 * 60
FALLOS_PARA_AVISAR = 2

_estado = {}  # url -> {"t": epoch de la última comprobación, "fallos": seguidos, "error": str|None}


def _urls():
    # Además de los separadores habituales, se corta siempre justo antes de cada
    # "http": si se olvida un separador entre dos enlaces, no quedan pegados.
    return [u for u in re.split(r"[,;\s]+|(?=https?://)", BEDS24_ICAL_URLS or "") if u.startswith("http")]


def _nombre(url):
    m = re.search(r"roomid=(\d+)", url)
    room = m.group(1) if m else "?"
    return ROOM_CONFIG.get(room, {}).get("nombre", f"habitación {room}")


def _consultar(url):
    """None si el enlace devuelve un calendario válido; si no, un texto corto
    que describe el fallo (sin el enlace)."""
    try:
        r = requests.get(url, timeout=15)
    except requests.RequestException as e:
        return f"sin respuesta ({type(e).__name__})"
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    texto = (r.text or "").strip()
    if texto.startswith("BEGIN:VCALENDAR"):
        return None
    primera_linea = texto.splitlines()[0][:80] if texto else ""
    return primera_linea or "respuesta vacía"


def _comprobar(url):
    error = _consultar(url)
    if error is not None:
        time.sleep(2)
        error = _consultar(url)
    return error


def comprobar_feeds_ical():
    """
    Devuelve None si BEDS24_ICAL_URLS no está configurada; si no,
    {"ok": [nombres], "fail": ["nombre: motivo", ...]} — solo con los fallos
    ya confirmados (FALLOS_PARA_AVISAR pasadas seguidas).
    """
    urls = _urls()
    if not urls:
        return None

    ok, fail = [], []
    for url in urls:
        nombre = _nombre(url)
        st = _estado.get(url)
        if st and st["error"] is None and time.time() - st["t"] < TTL_OK:
            ok.append(nombre)
            continue

        error = _comprobar(url)
        fallos = 0 if error is None else (st["fallos"] if st else 0) + 1
        _estado[url] = {"t": time.time(), "fallos": fallos, "error": error}

        if error is None:
            ok.append(nombre)
        elif fallos >= FALLOS_PARA_AVISAR:
            fail.append(f"{nombre}: {error}")
            logger.error(f"[ical_beds24] {nombre}: {error} ({fallos} pasadas seguidas)")
        else:
            logger.warning(f"[ical_beds24] {nombre}: {error} (primer fallo, se confirma en la próxima pasada)")
            ok.append(nombre)
    return {"ok": ok, "fail": fail}

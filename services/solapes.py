"""
services/solapes.py — Aviso de reservas que se pisan: dos reservas activas de la MISMA habitación con noches en común.

Es lo que ocurre en una sobreventa: Booking.com (u otro canal) vende una noche que Beds24 ya tenía ocupada porque la
sincronización falló un rato. Beds24 guarda las dos reservas en la misma habitación y nadie se entera hasta que llega el
segundo huésped. Aquí se miran las llegadas de hace SOLAPES_DIAS_ATRAS días a SOLAPES_DIAS_ADELANTE por delante (en trozos de
30 días y con limit 500, como el resto de consultas amplias: Beds24 devuelve resultados incompletos con rangos grandes).

Limitación: solo ve reservas que Beds24 SÍ tiene. Una reserva que el canal aceptó pero Beds24 nunca recibió no aparece aquí.

Se comprueba como mucho cada INTERVALO_MIN minutos (el watchdog corre cada 15; son unas 10 llamadas a Beds24 por pasada, y
el incidente «Credit limit exceeded» de sep 2026 obliga a no abusar). Dedupe en Firestore (system_state/solapes_avisados):
un par de reservas se avisa una sola vez.
"""
import logging
import time
from datetime import timedelta

import requests

import config
from config import BEDS24_API_BASE, BEDS24_PROPERTY_IDS, ROOM_CONFIG
from services.beds24 import (_canal_legible, _extraer_nombre_huesped_beds24, es_bloqueo_beds24,
                             get_beds24_access_token)
from services.fechas import hoy_madrid
from services.whatsapp import alerta

logger = logging.getLogger(__name__)

SOLAPES_DIAS_ATRAS = 30
SOLAPES_DIAS_ADELANTE = 120
TROZO_DIAS = 30
INTERVALO_MIN = 60

_ultima_pasada = 0.0
_avisados_en_memoria = set()   # por si Firestore no está disponible: no repetir el aviso en cada pasada


def _leer_reservas():
    """Reservas (crudas) activas de todas las propiedades con llegada en la ventana. Lanza si no se puede consultar."""
    token = get_beds24_access_token()
    hoy = hoy_madrid()
    desde, hasta = hoy - timedelta(days=SOLAPES_DIAS_ATRAS), hoy + timedelta(days=SOLAPES_DIAS_ADELANTE)
    vistas, resultado = set(), []
    for property_id in BEDS24_PROPERTY_IDS:
        inicio = desde
        while inicio <= hasta:
            fin = min(inicio + timedelta(days=TROZO_DIAS), hasta)
            resp = requests.get(
                f"{BEDS24_API_BASE}/bookings",
                headers={"token": token, "accept": "application/json"},
                params={"propertyId": property_id, "arrivalFrom": inicio.isoformat(), "arrivalTo": fin.isoformat(),
                        "limit": 500, "includePersonalInfo": "true"},
                timeout=20,
            )
            resp.raise_for_status()
            for b in resp.json().get("data", []):
                if b.get("id") in vistas:
                    continue   # reservas en el borde entre dos trozos
                vistas.add(b.get("id"))
                if str(b.get("status", "")).lower() == "cancelled" or es_bloqueo_beds24(b):
                    continue
                if b.get("arrival") and b.get("departure") and b.get("roomId") is not None:
                    resultado.append(b)
            inicio = fin + timedelta(days=1)
    return resultado


def hallar_solapes(reservas, hoy_iso):
    """
    Pares (a, b) de reservas de la misma habitación (y unidad) que comparten alguna noche. Salir el mismo día que entra la
    otra NO es solape. Se ignoran los pares que ya terminaron antes de hoy.
    """
    por_unidad = {}
    for b in reservas:
        por_unidad.setdefault((str(b.get("roomId")), str(b.get("unitId") or "")), []).append(b)
    pares = []
    for lista in por_unidad.values():
        lista.sort(key=lambda b: (b.get("arrival") or "", str(b.get("id"))))
        for i, a in enumerate(lista):
            for b in lista[i + 1:]:
                if (b["arrival"]) >= a["departure"]:
                    break   # ordenadas por llegada: las siguientes empiezan aún más tarde
                if str(a.get("id")) == str(b.get("id")) or min(a["departure"], b["departure"]) < hoy_iso:
                    continue
                pares.append((a, b))
    return pares


def _clave_par(a, b):
    return "-".join(sorted([str(a.get("id")), str(b.get("id"))]))


def _describir(b):
    return (f"{_extraer_nombre_huesped_beds24(b)} ({_canal_legible(b)}, reserva {b.get('apiReference') or '—'}, "
            f"Beds24 {b.get('id')}): {b.get('arrival')} → {b.get('departure')}")


def calcular_solapes():
    """Lista de dicts {clave, habitacion, a, b} con los solapes ahora mismo. Lanza si Beds24 no responde."""
    pares = hallar_solapes(_leer_reservas(), hoy_madrid().isoformat())
    return [{"clave": _clave_par(a, b),
             "habitacion": ROOM_CONFIG.get(str(a.get("roomId")), {}).get("nombre", f"Room {a.get('roomId')}"),
             "a": _describir(a), "b": _describir(b)} for a, b in pares]


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("solapes_avisados")


def _leer_avisados():
    ref = _doc_ref()
    if ref is None:
        return set(_avisados_en_memoria)
    try:
        doc = ref.get()
        return set((doc.to_dict() or {}).get("pares", [])) if doc.exists else set()
    except Exception as e:
        logger.error(f"[solapes] Error leyendo estado en Firestore: {e}")
        return set(_avisados_en_memoria)


def _marcar_avisado(claves):
    _avisados_en_memoria.update(claves)
    ref = _doc_ref()
    if ref is None:
        return
    try:
        ref.set({"pares": sorted(_leer_avisados() | set(claves))})
    except Exception as e:
        logger.error(f"[solapes] Error guardando estado en Firestore: {e}")


def avisar_solapes(ahora=None):
    """Pensada para /watchdog. No lanza excepción: cualquier fallo se loguea y no debe bloquear el resto del watchdog."""
    global _ultima_pasada
    ahora = time.time() if ahora is None else ahora
    if ahora - _ultima_pasada < INTERVALO_MIN * 60:
        return
    _ultima_pasada = ahora
    try:
        solapes = calcular_solapes()
    except Exception as e:
        logger.error(f"[solapes] No se pudieron comprobar las reservas: {e}")
        return
    avisados = _leer_avisados()
    nuevos = [s for s in solapes if s["clave"] not in avisados]
    if not nuevos:
        return
    detalle = "\n\n".join(f"Habitación: {s['habitacion']}\n• {s['a']}\n• {s['b']}" for s in nuevos)
    alerta(f"🚨 Posible sobreventa: {len(nuevos)} par(es) de reservas se pisan", detalle +
           "\n\nHay que mover o cancelar una de las dos. Comprueba también el calendario del canal.", nivel="critico")
    _marcar_avisado([s["clave"] for s in nuevos])

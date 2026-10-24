"""
services/incidencias_limpieza.py — Días en los que la limpiadora de La Casa de la Primavera NO puede limpiar.

La limpiadora entra en la app de gestión con su propio PIN (config.PIN_LIMPIEZA) y ve solo el calendario de la casa (ocupado / libre). Pulsa un
día, confirma, y queda marcada una «incidencia de limpieza»: Ione recibe un aviso y en el calendario normal el día lleva un letrero ámbar
«INCIDENCIA» (haya o no reserva ese día) que, al pulsarlo, dice «LIMPIEZA NO DISPONIBLE» con el rango de fechas.

Estado en Firestore: colección `incidencias_limpieza`, un documento por día (id = fecha ISO) con {fecha, propiedad, creada}. Los días pasados se
borran al marcar uno nuevo. Nada de lo de aquí lanza excepción hacia arriba salvo ErrorIncidencias (para devolver un mensaje claro).
"""
import logging
import time
from datetime import date, datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)

COLECCION = "incidencias_limpieza"
PROPIEDAD = "349341"            # La Casa de la Primavera (Beds24)
HABITACION = "720841"           # su única «habitación» en Beds24
DIAS_VISTA = 365                # hasta cuándo se puede marcar y se enseña
_CACHE_SEG = 15
_cache = {"hasta": 0.0, "dias": []}


class ErrorIncidencias(Exception):
    def __init__(self, mensaje, status=400):
        super().__init__(mensaje)
        self.status = status


def _col():
    if config.db is None:
        raise ErrorIncidencias("Las incidencias no están disponibles ahora mismo", 503)
    return config.db.collection(COLECCION)


def _fecha(texto, hoy):
    try:
        d = date.fromisoformat(str(texto))
    except (TypeError, ValueError):
        raise ErrorIncidencias("Fecha no válida")
    if d < hoy:
        raise ErrorIncidencias("No se puede marcar un día que ya ha pasado")
    if d > hoy + timedelta(days=DIAS_VISTA):
        raise ErrorIncidencias("Esa fecha queda demasiado lejos")
    return d


def dias(hoy, forzar=False):
    """Días con incidencia desde hoy (ISO, ordenados). Con caché de unos segundos; [] si no hay Firestore."""
    if not forzar and time.time() < _cache["hasta"]:
        return [d for d in _cache["dias"] if d >= hoy.isoformat()]
    try:
        encontrados = sorted(doc.id for doc in _col().stream() if doc.id >= hoy.isoformat())
    except ErrorIncidencias:
        return []
    except Exception as e:
        logger.error(f"[incidencias] No se pudieron leer: {e}")
        return []
    _cache.update(hasta=time.time() + _CACHE_SEG, dias=encontrados)
    return encontrados


def rangos(lista):
    """Agrupa días consecutivos: [{'desde','hasta','dias'}]."""
    resultado = []
    for iso in sorted(set(lista)):
        d = date.fromisoformat(iso)
        if resultado and date.fromisoformat(resultado[-1]["hasta"]) + timedelta(days=1) == d:
            resultado[-1]["hasta"] = iso
            resultado[-1]["dias"] += 1
        else:
            resultado.append({"desde": iso, "hasta": iso, "dias": 1})
    return resultado


def rango_de(lista, iso):
    """El rango (consecutivo) de la lista al que pertenece ese día, o None."""
    return next((r for r in rangos(lista) if r["desde"] <= iso <= r["hasta"]), None)


def marcar(texto_fecha, hoy):
    """Marca el día. Devuelve (rango resultante, ya_estaba)."""
    d = _fecha(texto_fecha, hoy)
    iso = d.isoformat()
    col = _col()
    ya = iso in dias(hoy, forzar=True)
    if not ya:
        try:
            col.document(iso).set({"fecha": iso, "propiedad": PROPIEDAD, "creada": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            for viejo in [doc.id for doc in col.stream() if doc.id < hoy.isoformat()]:   # limpieza de días pasados
                col.document(viejo).delete()
        except Exception as e:
            logger.error(f"[incidencias] No se pudo marcar {iso}: {e}")
            raise ErrorIncidencias("No se ha podido guardar. Inténtalo de nuevo.", 502)
    _cache["hasta"] = 0
    return rango_de(dias(hoy, forzar=True) or [iso], iso) or {"desde": iso, "hasta": iso, "dias": 1}, ya


def quitar(texto_fecha, hoy):
    """Quita la marca del día. Devuelve True si estaba marcado."""
    d = _fecha(texto_fecha, hoy)
    iso = d.isoformat()
    ya = iso in dias(hoy, forzar=True)
    if ya:
        try:
            _col().document(iso).delete()
        except Exception as e:
            logger.error(f"[incidencias] No se pudo quitar {iso}: {e}")
            raise ErrorIncidencias("No se ha podido guardar. Inténtalo de nuevo.", 502)
    _cache["hasta"] = 0
    return ya


def noches_ocupadas(reservas, hoy):
    """Noches (ISO) de la casa ocupadas por reservas o bloqueos no cancelados, de hoy a DIAS_VISTA días. Solo ocupado/libre: sin nombres ni precios."""
    fin = hoy + timedelta(days=DIAS_VISTA)
    ocupadas = set()
    for b in reservas:
        if str(b.get("roomId", "")) != HABITACION or str(b.get("status", "")).lower() == "cancelled":
            continue
        try:
            d, salida = date.fromisoformat(str(b["arrival"])[:10]), date.fromisoformat(str(b["departure"])[:10])
        except (KeyError, ValueError):
            continue
        while d < salida and d <= fin:
            if d >= hoy:
                ocupadas.add(d.isoformat())
            d += timedelta(days=1)
    return sorted(ocupadas)

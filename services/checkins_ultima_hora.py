"""
services/checkins_ultima_hora.py — Aviso de WhatsApp para check-ins de
ÚLTIMA HORA, tanto en el Hostal como en La Casa de la Primavera: reservas con
llegada HOY que no aparecían en ningún resumen de ese día (ni en el de las
23h del día anterior ni en el de las 8h de hoy, ver
services/resumen_programado.py). Funciona a cualquier hora del día. Antes se
llamaba primavera_avisos.py, de cuando solo cubría La Casa de la Primavera.

Lleva en Firestore, por fecha, los book_id ya anunciados — por los resúmenes
(llaman a marcar_anunciados() tras enviarse) y por esta misma función — para
no avisar dos veces de la misma reserva:
    {"dias": {"YYYY-MM-DD": [book_id, ...], ...}}
Que una fecha esté en el mapa significa que algún resumen ya cubrió ese día;
mientras no lo esté, no se avisa de nada (todas las llegadas parecerían "de
última hora"). Los días pasados se podan al escribir.

El documento de Firestore sigue llamándose system_state/primavera_avisos a
propósito: cambiarle el nombre perdería el estado en curso al desplegar y
provocaría avisos duplicados. No renombrarlo.

"Hoy" es siempre el día de Madrid (services/fechas.py), no el del servidor
(UTC). Con date.today() el día cambiaba a las 02:00 de Madrid y el watchdog de
las 02:13 avisaba de todas las llegadas del día como si fueran de última hora.
"""
import logging
from datetime import date

import config
from services.beds24 import obtener_bookings_dia_beds24
from services.fechas import hoy_madrid
from services.whatsapp import alerta

logger = logging.getLogger(__name__)

ROOM_ID_PRIMAVERA = "720841"


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("primavera_avisos")


def _leer_dias():
    """{fecha_iso: set(book_id como str)} de los días ya cubiertos por algún
    resumen. None si Firestore no está disponible o falla la lectura (para no
    confundirlo con "no hay nada anunciado")."""
    ref = _doc_ref()
    if ref is None:
        return None
    try:
        doc = ref.get()
        if not doc.exists:
            return {}
        data = doc.to_dict() or {}
        if "dias" in data:
            return {f: set(str(b) for b in ids) for f, ids in (data.get("dias") or {}).items()}
        # Formato antiguo (hasta oct 2026): un único día {fecha, book_ids}.
        if data.get("fecha"):
            return {data["fecha"]: set(str(b) for b in data.get("book_ids", []))}
        return {}
    except Exception as e:
        logger.error(f"[primavera_avisos] Error leyendo estado en Firestore: {e}")
        return None


def marcar_anunciados(book_ids, dia=None):
    """Añade estos book_id al conjunto de anunciados de `dia` (por defecto
    hoy). Escribe aunque la lista venga vacía: que el día figure en el mapa es
    lo que indica que ya lo cubrió un resumen y habilita los avisos de última
    hora de ese día."""
    ref = _doc_ref()
    dias = _leer_dias()
    if ref is None or dias is None:
        return
    hoy_iso = hoy_madrid().isoformat()
    dia_iso = (dia or hoy_madrid()).isoformat()
    dias = {f: ids for f, ids in dias.items() if f >= hoy_iso}
    dias[dia_iso] = dias.get(dia_iso, set()) | {str(b) for b in book_ids if b is not None}
    try:
        ref.set({"dias": {f: sorted(ids) for f, ids in dias.items()}})
    except Exception as e:
        logger.error(f"[primavera_avisos] Error guardando estado en Firestore: {e}")


def _formatear_estancia(entrada):
    """(noches, fecha_salida_fmt) a partir de arrival/departure ISO. Si no se
    puede calcular (campos ausentes/formato raro), devuelve (None, valor tal
    cual llegó) para no romper el aviso por esto."""
    try:
        llegada = date.fromisoformat(entrada["arrival"])
        salida = date.fromisoformat(entrada["departure"])
        return (salida - llegada).days, salida.strftime("%d/%m/%Y")
    except Exception:
        return None, entrada.get("departure") or "?"


def comprobar_y_avisar_checkins_ultima_hora():
    """
    Consulta los check-ins de HOY en TODAS las propiedades (hostal + La Casa
    de la Primavera) directamente en Beds24 y avisa por WhatsApp de
    cualquiera que no se haya anunciado todavía (ni en un resumen ni en una
    llamada anterior a esta misma función) — pensada para llamarse desde
    /watchdog, que ya corre cada 15 min, así que una reserva de última hora
    se detecta y avisa en <15 min, a cualquier hora.

    No lanza excepción hacia arriba: cualquier fallo se loguea y no debe
    bloquear el resto del watchdog.
    """
    hoy_iso = hoy_madrid().isoformat()
    dias = _leer_dias()
    if not dias or hoy_iso not in dias:
        # Ningún resumen ha cubierto todavía el día de hoy (falló el de las
        # 23h de ayer y aún no ha salido el de las 8h). Sin esa referencia,
        # todas las llegadas parecerían de última hora. El resumen de las 8h
        # se reintenta en cada watchdog, así que esto se resuelve solo.
        return

    entradas = obtener_bookings_dia_beds24(hoy_iso, tipo="checkin")
    if not entradas:
        return

    anunciados = dias[hoy_iso]
    nuevas = [e for e in entradas if str(e.get("book_id")) not in anunciados]
    if not nuevas:
        return

    for e in nuevas:
        canal = e.get("canal", "Desconocido")
        if e.get("room_id") == ROOM_ID_PRIMAVERA:
            noches, salida_fmt = _formatear_estancia(e)
            linea_estancia = f"{noches} noche{'s' if noches != 1 else ''}, sale {salida_fmt}" if noches is not None else f"Sale {salida_fmt}"
            titulo = "⚡ Check-in de última hora — La Casa de la Primavera"
            cuerpo = (
                f"Huésped: {e.get('huesped', '?')}\nCanal: {canal}\n{linea_estancia}\n\n"
                f"(No estaba en el resumen diario — reserva de última hora.)"
            )
        else:
            titulo = "⚡ Check-in de última hora — Hostal ALC Homes"
            cuerpo = (
                f"Habitación: {e.get('nombre_habitacion', '?')}\n"
                f"Huésped: {e.get('huesped', '?')}\nCanal: {canal}\n\n"
                f"(No estaba en el resumen diario — reserva de última hora.)"
            )
        alerta(titulo, cuerpo, nivel="info")

    marcar_anunciados([e.get("book_id") for e in nuevas])

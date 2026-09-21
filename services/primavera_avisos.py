"""
services/primavera_avisos.py — Aviso de WhatsApp para check-ins de ÚLTIMA
HORA, tanto en el Hostal como en La Casa de la Primavera (reservas que
llegan después de que ya haya salido el resumen diario de la mañana,
típicamente reservas hechas el mismo día). El nombre del módulo viene de
cuando solo cubría Primavera; se mantiene para no tocar los imports de
routes/watchdog.py y routes/resumen_routes.py.

Lleva en Firestore (system_state/primavera_avisos) la lista de book_id de
Beds24 ya anunciados HOY — tanto por el resumen diario (services/resumen.py
marca los suyos tras enviarse) como por esta misma función — para no avisar
dos veces de la misma reserva. El doc se "reinicia" solo cada día nuevo (se
compara la fecha guardada, no hace falta borrarlo).
"""
import logging
from datetime import date

import config
from services.beds24 import obtener_bookings_dia_beds24
from services.whatsapp import alerta

logger = logging.getLogger(__name__)

ROOM_ID_PRIMAVERA = "720841"


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("primavera_avisos")


def _leer_anunciados_hoy():
    """book_id (como str) ya anunciados hoy. Vacío si Firestore no está
    disponible, si no hay doc todavía, o si el doc guardado es de un día
    anterior (se considera reiniciado, sin necesidad de borrarlo)."""
    ref = _doc_ref()
    if ref is None:
        return set()
    try:
        doc = ref.get()
        if not doc.exists:
            return set()
        data = doc.to_dict() or {}
        if data.get("fecha") != date.today().isoformat():
            return set()
        return set(str(b) for b in data.get("book_ids", []))
    except Exception as e:
        logger.error(f"[primavera_avisos] Error leyendo estado en Firestore: {e}")
        return set()


def marcar_anunciados(book_ids):
    """Añade estos book_id (de reservas de La Casa de la Primavera) al
    conjunto de 'ya anunciados hoy', para que ni el resumen de la tarde ni
    el chequeo de última hora vuelvan a avisar de ellos."""
    book_ids = [str(b) for b in book_ids if b is not None]
    if not book_ids:
        return
    ref = _doc_ref()
    if ref is None:
        return
    try:
        nuevos = _leer_anunciados_hoy() | set(book_ids)
        ref.set({"fecha": date.today().isoformat(), "book_ids": sorted(nuevos)})
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
    cualquiera que no se haya anunciado todavía (ni en el resumen diario ni
    en una llamada anterior a esta misma función) — pensada para llamarse
    desde /watchdog, que ya corre cada 15 min, así que una reserva de última
    hora se detecta y avisa en <15 min en vez de esperar al resumen de la
    noche.

    No lanza excepción hacia arriba: cualquier fallo se loguea y no debe
    bloquear el resto del watchdog.
    """
    hoy_iso = date.today().isoformat()
    entradas = obtener_bookings_dia_beds24(hoy_iso, tipo="checkin")
    if not entradas:
        return

    anunciados = _leer_anunciados_hoy()
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

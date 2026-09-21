"""
services/resumen.py — Generación del resumen diario para WhatsApp
(entradas/salidas de hoy según Beds24, cruzadas con los partes de RPV).
"""
import logging
from datetime import date, datetime

from services.beds24 import get_beds24_access_token, obtener_bookings_dia_beds24
from services.rpv import obtener_partes_recibidos_hoy
from services.whatsapp import avisar_error_critico

logger = logging.getLogger(__name__)


ROOM_ID_PRIMAVERA = "720841"


def _formatear_estancia(entrada):
    """(noches, fecha_salida_fmt) a partir de arrival/departure ISO de la
    reserva. Devuelve (None, valor_crudo) si no se puede calcular, para no
    romper el resumen por un campo ausente o con formato inesperado."""
    try:
        llegada = date.fromisoformat(entrada["arrival"])
        salida = date.fromisoformat(entrada["departure"])
        return (salida - llegada).days, salida.strftime("%d/%m/%Y")
    except Exception:
        return None, entrada.get("departure") or "?"


def generar_mensaje_resumen(hora_str=None):
    """
    Genera el resumen diario para WhatsApp.
    - ENTRADAS HOY y SALIDAS HOY se obtienen directamente de Beds24 (reservas
      reales), no de los emails de partes recibidos — así el resumen refleja
      quién debe entrar/salir hoy según la reserva, independientemente de si
      ya ha rellenado el parte o no.
    - Para cada ENTRADA se indica además si el parte de viajero ya se ha
      recibido (cruzando con los emails procesados) o si sigue pendiente.
    - Para las entradas de La Casa de la Primavera se añade además cuántas
      noches dura la reserva y la fecha de salida (en el hostal esto no
      aporta tanto porque el huésped suele ver la duración a simple vista;
      en Primavera, al ser una vivienda completa reservada con más antelación
      y menos rotación visual, conviene dejarlo explícito).
    - Cada entrada indica también el canal por el que llegó la reserva
      (Booking.com, Airbnb, Directo...).

    Devuelve (mensaje, book_ids_hoy): el segundo valor es la lista de book_id
    de Beds24 de TODAS las entradas de hoy (hostal + La Casa de la Primavera),
    para que el llamador las marque como "ya anunciadas" en
    services/primavera_avisos tras confirmar el envío — así el chequeo de
    última hora (ver services/primavera_avisos.py, que ahora cubre ambas
    propiedades) no vuelve a avisar de ninguna de ellas.
    """
    hoy = date.today()
    hoy_iso = hoy.isoformat()
    if hora_str is None:
        hora_str = datetime.now().strftime("%H")

    # Punto de control: verificar autenticación con Beds24 antes de consultar
    # las reservas del día. Si falla, avisamos por WhatsApp además de que el
    # resumen seguirá generándose (con listas vacías) para no bloquear el envío.
    try:
        get_beds24_access_token()
    except Exception as e:
        avisar_error_critico(
            "Fallo de autenticación con Beds24 (resumen diario)",
            f"No se pudo conectar con Beds24 para generar el resumen de hoy ({e}). "
            f"El resumen se enviará sin entradas/salidas hasta que se resuelva. "
            f"Revisa BEDS24_REFRESH_TOKEN en Render."
        )

    entradas_beds24 = obtener_bookings_dia_beds24(hoy_iso, tipo="checkin")
    salidas_beds24  = obtener_bookings_dia_beds24(hoy_iso, tipo="checkout")
    partes_recibidos = obtener_partes_recibidos_hoy()

    hoy_fmt = hoy.strftime("%d/%m/%Y")
    lineas = [f"🏨 ALCHOMES — {hoy_fmt} · {hora_str}:00h"]

    lineas.append("\n✅ ENTRADAS HOY:")
    if entradas_beds24:
        for e in entradas_beds24:
            parte_ok = (e["room_id"], hoy_iso) in partes_recibidos
            estado = "📄 parte recibido" if parte_ok else "⚠️ parte PENDIENTE"
            canal = e.get("canal", "Desconocido")
            if e["room_id"] == ROOM_ID_PRIMAVERA:
                noches, salida_fmt = _formatear_estancia(e)
                estancia = f"{noches} noche{'s' if noches != 1 else ''}, sale {salida_fmt}" if noches is not None else f"sale {salida_fmt}"
                lineas.append(f"• {e['nombre_habitacion']} ({estado}) — {canal} — {estancia}")
            else:
                lineas.append(f"• {e['nombre_habitacion']} ({estado}) — {canal}")
    else:
        lineas.append("• (ninguna)")

    lineas.append("\n🚪 SALIDAS HOY:")
    if salidas_beds24:
        for s in salidas_beds24:
            lineas.append(f"• {s['nombre_habitacion']}")
    else:
        lineas.append("• (ninguna)")

    book_ids_hoy = [e["book_id"] for e in entradas_beds24]
    return "\n".join(lineas), book_ids_hoy

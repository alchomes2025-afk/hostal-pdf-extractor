"""
services/resumen.py — Generación del resumen diario para WhatsApp
(entradas/salidas de hoy según Beds24, cruzadas con los partes de RPV).
"""
import logging
from datetime import date, datetime

from services.beds24 import get_beds24_access_token, obtener_bookings_dia_beds24
from services.rpv import obtener_partes_con_estado
from services.whatsapp import avisar_error_critico
from services.fechas import ahora_madrid, hoy_madrid

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


def generar_mensaje_resumen(hora_str=None, dia=None):
    """
    Genera el resumen para WhatsApp de las entradas y salidas de `dia` (por
    defecto hoy, Madrid). Se usa para el resumen de las 8h (dia = hoy) y el de
    las 23h (dia = mañana) — ver services/resumen_programado.py.
    - ENTRADAS y SALIDAS se obtienen directamente de Beds24 (reservas
      reales), de las dos propiedades, no de los partes recibidos — así el
      resumen refleja quién entra/sale según la reserva, haya rellenado o no
      el parte.
    - Para cada ENTRADA se indica además si el parte de viajero ya se ha
      recibido en RPV o si sigue pendiente.
    - Para las entradas de La Casa de la Primavera se añade además cuántas
      noches dura la reserva y la fecha de salida (en el hostal esto no
      aporta tanto porque el huésped suele ver la duración a simple vista;
      en Primavera, al ser una vivienda completa reservada con más antelación
      y menos rotación visual, conviene dejarlo explícito).
    - Cada entrada y cada salida indica el canal por el que llegó la reserva
      (Booking.com, Airbnb, Directo...).

    Devuelve (mensaje, book_ids_entradas): el segundo valor es la lista de
    book_id de Beds24 de TODAS las entradas de `dia`, para que el llamador las
    marque como "ya anunciadas" para ese día (services/checkins_ultima_hora)
    tras confirmar el envío — así el chequeo de última hora no vuelve a
    avisar de ninguna de ellas.
    """
    hoy = hoy_madrid()
    dia = dia or hoy
    dia_iso = dia.isoformat()
    es_manana = dia > hoy
    etiqueta = "MAÑANA" if es_manana else "HOY"
    if hora_str is None:
        hora_str = ahora_madrid().strftime("%H")

    # Punto de control: verificar autenticación con Beds24 antes de consultar
    # las reservas del día. Si falla, avisamos por WhatsApp además de que el
    # resumen seguirá generándose (con listas vacías) para no bloquear el envío.
    try:
        get_beds24_access_token()
    except Exception as e:
        avisar_error_critico(
            "Fallo de autenticación con Beds24 (resumen diario)",
            f"No se pudo conectar con Beds24 para generar el resumen del {dia.strftime('%d/%m/%Y')} ({e}). "
            f"El resumen se enviará sin entradas/salidas hasta que se resuelva. "
            f"Revisa BEDS24_REFRESH_TOKEN en Render."
        )

    entradas_beds24 = obtener_bookings_dia_beds24(dia_iso, tipo="checkin")
    salidas_beds24  = obtener_bookings_dia_beds24(dia_iso, tipo="checkout")
    # Dato de RPV de hasta 5 min (no los ~30 de segundo plano): el resumen
    # sale 2 veces al día y el estado del parte debe estar al día.
    partes_recibidos, rpv_sin_verificar = obtener_partes_con_estado(max_age=5 * 60)

    dia_fmt = dia.strftime("%d/%m/%Y")
    if es_manana:
        lineas = [f"🌙 ALCHOMES — Resumen de MAÑANA {dia_fmt} · {hora_str}:00h"]
    else:
        lineas = [f"🏨 ALCHOMES — {dia_fmt} · {hora_str}:00h"]

    lineas.append(f"\n✅ ENTRADAS {etiqueta}:")
    if entradas_beds24:
        for e in entradas_beds24:
            if (e["room_id"], dia_iso) in partes_recibidos:
                estado = "📄 parte recibido"
            elif e["room_id"] in rpv_sin_verificar:
                estado = "❓ parte SIN VERIFICAR (RPV no responde)"
            else:
                estado = "⚠️ parte PENDIENTE"
            canal = e.get("canal", "Desconocido")
            if e["room_id"] == ROOM_ID_PRIMAVERA:
                noches, salida_fmt = _formatear_estancia(e)
                estancia = f"{noches} noche{'s' if noches != 1 else ''}, sale {salida_fmt}" if noches is not None else f"sale {salida_fmt}"
                lineas.append(f"• {e['nombre_habitacion']} ({estado}) — {canal} — {estancia}")
            else:
                lineas.append(f"• {e['nombre_habitacion']} ({estado}) — {canal}")
    else:
        lineas.append("• (ninguna)")

    lineas.append(f"\n🚪 SALIDAS {etiqueta}:")
    if salidas_beds24:
        for s in salidas_beds24:
            lineas.append(f"• {s['nombre_habitacion']} — {s.get('canal', 'Desconocido')}")
    else:
        lineas.append("• (ninguna)")

    book_ids_entradas = [e["book_id"] for e in entradas_beds24]
    return "\n".join(lineas), book_ids_entradas

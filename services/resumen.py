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


def generar_mensaje_resumen(hora_str=None):
    """
    Genera el resumen diario para WhatsApp.
    - ENTRADAS HOY y SALIDAS HOY se obtienen directamente de Beds24 (reservas
      reales), no de los emails de partes recibidos — así el resumen refleja
      quién debe entrar/salir hoy según la reserva, independientemente de si
      ya ha rellenado el parte o no.
    - Para cada ENTRADA se indica además si el parte de viajero ya se ha
      recibido (cruzando con los emails procesados) o si sigue pendiente.
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
            lineas.append(f"• {e['nombre_habitacion']} ({estado})")
    else:
        lineas.append("• (ninguna)")

    lineas.append("\n🚪 SALIDAS HOY:")
    if salidas_beds24:
        for s in salidas_beds24:
            lineas.append(f"• {s['nombre_habitacion']}")
    else:
        lineas.append("• (ninguna)")

    return "\n".join(lineas)

"""
services/enlace_parte.py — Mensaje de EMERGENCIA con el enlace de RPV de la habitación, para que el huésped haga el parte cuando
nada más funciona (el enlace por reserva aún no existe: llegará con la API de RPV).

El enlace general de la habitación (propiedad/<código>) pide las fechas y, según RPV (2026-10-16), asocia el parte a la reserva
importada o crea una reserva manual («M»). Por eso el mensaje pide al huésped las fechas EXACTAS de su reserva y el número de
reserva de Beds24 como referencia: así se evita un duplicado o un parte con fechas que no cuadran. Solo es texto para copiar: no
envía nada ni lleva códigos de acceso (esos se mandan después, cuando el parte consta como completado).
"""
from datetime import date

from config import ROOM_CONFIG, RPV_LINKS


class ErrorEnlace(Exception):
    pass


def _fecha(iso):
    return date.fromisoformat(str(iso)[:10]).strftime("%d/%m/%Y")


def redactar_enlace_parte(booking, idioma):
    """(texto, habitación, enlace) para esa reserva de Beds24. Lanza ErrorEnlace si faltan datos."""
    idioma = "es" if idioma == "es" else "en"
    room_id = str(booking.get("roomId") or "")
    enlace = RPV_LINKS.get(room_id)
    if not enlace:
        raise ErrorEnlace("Esa habitación no tiene enlace de RPV configurado")
    try:
        llegada, salida = _fecha(booking["arrival"]), _fecha(booking["departure"])
    except (KeyError, ValueError):
        raise ErrorEnlace("La reserva no tiene fechas válidas")
    habitacion = ROOM_CONFIG.get(room_id, {}).get("nombre", room_id)
    nombre = str(booking.get("firstName") or "").strip().split(" ")[0]
    numero = booking.get("id")
    if idioma == "es":
        texto = (f"Hola{' ' + nombre if nombre else ''}, para completar el registro de viajeros de tu estancia en {habitacion}, "
                 f"entra en este enlace y rellena los datos de todos los huéspedes:\n{enlace}\n\n"
                 f"Cuando te pida las fechas, pon exactamente estas: llegada {llegada} y salida {salida}. "
                 f"Si te pide un número de reserva o referencia, pon {numero}.\n\n"
                 "Avísanos cuando termines y te enviaremos enseguida los códigos de acceso. Gracias.")
    else:
        texto = (f"Hello{' ' + nombre if nombre else ''}, to complete the guest registration for your stay in {habitacion}, "
                 f"please open this link and fill in the details of all guests:\n{enlace}\n\n"
                 f"When it asks for the dates, enter exactly these: arrival {llegada} and departure {salida}. "
                 f"If it asks for a booking number or reference, enter {numero}.\n\n"
                 "Let us know when you have finished and we will send you the access codes right away. Thank you.")
    return texto, habitacion, enlace

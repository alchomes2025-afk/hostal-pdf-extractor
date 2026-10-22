"""
services/enlace_parte.py — Mensaje de EMERGENCIA con el enlace de RPV de la habitación, para que el huésped haga el parte cuando
nada más funciona (el enlace por reserva aún no existe: llegará con la API de RPV).

El enlace general de la habitación (propiedad/<código>) pide las fechas y, según RPV (2026-10-16), asocia el parte a la reserva
importada o crea una reserva manual («M»). Por eso el mensaje pide al huésped las fechas EXACTAS de su reserva y el número de
reserva de Beds24 como referencia: así se evita un duplicado o un parte con fechas que no cuadran. Solo es texto para copiar: no
envía nada ni lleva códigos de acceso (esos se mandan después, cuando el parte consta como completado).

Reserva de VARIAS habitaciones: Beds24 la guarda como una reserva por habitación y RPV crea un parte por habitación, así que el mensaje
lleva el enlace, las fechas y la referencia de CADA habitación (un solo mensaje para todo el grupo).
"""
from datetime import date

from config import ROOM_CONFIG, RPV_LINKS


class ErrorEnlace(Exception):
    pass


def _fecha(iso):
    return date.fromisoformat(str(iso)[:10]).strftime("%d/%m/%Y")


def _datos(booking):
    """(habitación, enlace, llegada, salida, nº de reserva) de una reserva de una habitación; ErrorEnlace si falta algo."""
    room_id = str(booking.get("roomId") or "")
    enlace = RPV_LINKS.get(room_id)
    if not enlace:
        raise ErrorEnlace("Esa habitación no tiene enlace de RPV configurado")
    try:
        llegada, salida = _fecha(booking["arrival"]), _fecha(booking["departure"])
    except (KeyError, ValueError):
        raise ErrorEnlace("La reserva no tiene fechas válidas")
    return ROOM_CONFIG.get(room_id, {}).get("nombre", room_id), enlace, llegada, salida, booking.get("id")


def redactar_enlace_parte(booking, idioma, miembros=None):
    """(texto, habitación(es), enlace(s)) para esa reserva de Beds24. `miembros` son las reservas de su grupo de habitaciones (incluida
    ella): con más de una, un solo mensaje con los datos de cada habitación. Lanza ErrorEnlace si faltan datos."""
    idioma = "es" if idioma == "es" else "en"
    grupo = sorted(miembros, key=lambda b: int(b.get("id") or 0)) if miembros and len(miembros) > 1 else [booking]
    datos = [_datos(b) for b in grupo]
    nombre = str(booking.get("firstName") or "").strip().split(" ")[0]
    saludo = ("Hola" if idioma == "es" else "Hello") + (" " + nombre if nombre else "")
    if len(datos) == 1:
        habitacion, enlace, llegada, salida, numero = datos[0]
        if idioma == "es":
            texto = (f"{saludo}, para completar el registro de viajeros de tu estancia en {habitacion}, "
                     f"entra en este enlace y rellena los datos de todos los huéspedes:\n{enlace}\n\n"
                     f"Cuando te pida las fechas, pon exactamente estas: llegada {llegada} y salida {salida}. "
                     f"Si te pide un número de reserva o referencia, pon {numero}.\n\n"
                     "Avísanos cuando termines y te enviaremos enseguida los códigos de acceso. Gracias.")
        else:
            texto = (f"{saludo}, to complete the guest registration for your stay in {habitacion}, "
                     f"please open this link and fill in the details of all guests:\n{enlace}\n\n"
                     f"When it asks for the dates, enter exactly these: arrival {llegada} and departure {salida}. "
                     f"If it asks for a booking number or reference, enter {numero}.\n\n"
                     "Let us know when you have finished and we will send you the access codes right away. Thank you.")
        return texto, habitacion, enlace
    if idioma == "es":
        bloques = "\n\n".join(f"• {h}: {e}\n  Llegada {ll} y salida {sa}. Referencia: {n}" for h, e, ll, sa, n in datos)
        texto = (f"{saludo}, tu reserva tiene {len(datos)} habitaciones y hay que completar el registro de viajeros de CADA una. "
                 "Entra en cada enlace y rellena los datos de los huéspedes de esa habitación:\n\n"
                 f"{bloques}\n\n"
                 "Cuando te pida las fechas, pon exactamente las indicadas en cada habitación y, si te pide un número de reserva o referencia, la que aparece debajo. "
                 "Avísanos cuando hayas terminado todas y te enviaremos enseguida los códigos de acceso. Gracias.")
    else:
        bloques = "\n\n".join(f"• {h}: {e}\n  Arrival {ll} and departure {sa}. Reference: {n}" for h, e, ll, sa, n in datos)
        texto = (f"{saludo}, your booking has {len(datos)} rooms and the guest registration must be completed for EACH one. "
                 "Please open each link and fill in the details of the guests staying in that room:\n\n"
                 f"{bloques}\n\n"
                 "When it asks for the dates, enter exactly the ones shown for each room and, if it asks for a booking number or reference, the one listed below it. "
                 "Let us know when you have finished all of them and we will send you the access codes right away. Thank you.")
    return texto, " + ".join(d[0] for d in datos), [d[1] for d in datos]

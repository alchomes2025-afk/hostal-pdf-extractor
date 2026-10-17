"""
services/acceso_correos.py — Textos de los correos del formulario «¿No te encuentra? Recibe tu enlace por correo»
de la web de check-in (POST /solicitar-acceso), en los 5 idiomas de la web: es, en, fr, de, val.

Dos correos: el enlace personal (se encontró la reserva) y «no localizada» (no se encontró). No llevan nombres ni
datos de la reserva: el correo puede ir a una dirección que haya escrito otra persona.
"""
from services.acceso_enlace import TELEFONO

_ENLACE = {
    "es": ("Tu enlace personal de check-in — ALC Homes",
           "Hola,\n\nAquí tienes tu enlace personal para hacer el check-in online y consultar tus códigos de acceso cuando estén disponibles:\n\n{url}\n\n"
           "Es personal: no lo compartas. Guárdalo y podrás volver a entrar sin tener que escribir tu nombre.\n\n"
           "Si tienes cualquier problema, llama al teléfono de atención al cliente {tel} (24 h).\n\nEquipo ALC Homes"),
    "en": ("Your personal check-in link — ALC Homes",
           "Hello,\n\nHere is your personal link to complete the online check-in and see your access codes once they are available:\n\n{url}\n\n"
           "It is personal: please do not share it. Keep it and you will be able to come back without typing your name.\n\n"
           "If you have any problem, call customer service on {tel} (24 h).\n\nALC Homes Team"),
    "fr": ("Votre lien personnel de check-in — ALC Homes",
           "Bonjour,\n\nVoici votre lien personnel pour effectuer le check-in en ligne et consulter vos codes d'accès dès qu'ils seront disponibles :\n\n{url}\n\n"
           "Il est personnel : ne le partagez pas. Conservez-le et vous pourrez revenir sans avoir à saisir votre nom.\n\n"
           "En cas de problème, appelez le service client au {tel} (24 h/24).\n\nL'équipe ALC Homes"),
    "de": ("Dein persönlicher Check-in-Link — ALC Homes",
           "Hallo,\n\nHier ist dein persönlicher Link, um den Online-Check-in abzuschließen und deine Zugangscodes zu sehen, sobald sie verfügbar sind:\n\n{url}\n\n"
           "Er ist persönlich: bitte nicht weitergeben. Speichere ihn, dann kannst du wiederkommen, ohne deinen Namen einzugeben.\n\n"
           "Bei Problemen ruf den Kundenservice unter {tel} an (rund um die Uhr).\n\nDas ALC-Homes-Team"),
    "val": ("El teu enllaç personal de check-in — ALC Homes",
            "Hola,\n\nAci tens el teu enllaç personal per fer el check-in en línia i consultar els teus codis d'accés quan estiguen disponibles:\n\n{url}\n\n"
            "És personal: no el compartisques. Guarda'l i podràs tornar a entrar sense haver d'escriure el teu nom.\n\n"
            "Si tens qualsevol problema, telefona al servei d'atenció al client {tel} (24 h).\n\nEquip ALC Homes"),
}

_NO_LOCALIZADA = {
    "es": ("No hemos localizado tu reserva — ALC Homes",
           "Hola,\n\nNo hemos podido localizar tu reserva con los datos que has indicado. Recuerda que el check-in online se activa el día antes de tu llegada.\n\n"
           "Puedes responder a este correo con tu nombre completo, tal como figura en la reserva, y el número de confirmación (Booking, Airbnb…), "
           "o llamar al teléfono de atención al cliente {tel} (24 h).\n\nEquipo ALC Homes"),
    "en": ("We could not find your booking — ALC Homes",
           "Hello,\n\nWe could not find your booking with the details you gave us. Please note that online check-in opens the day before your arrival.\n\n"
           "You can reply to this email with your full name, as it appears on the booking, and your confirmation number (Booking, Airbnb…), "
           "or call customer service on {tel} (24 h).\n\nALC Homes Team"),
    "fr": ("Nous n'avons pas trouvé votre réservation — ALC Homes",
           "Bonjour,\n\nNous n'avons pas pu trouver votre réservation avec les informations indiquées. Le check-in en ligne s'ouvre la veille de votre arrivée.\n\n"
           "Vous pouvez répondre à ce message avec votre nom complet, tel qu'il figure sur la réservation, et votre numéro de confirmation (Booking, Airbnb…), "
           "ou appeler le service client au {tel} (24 h/24).\n\nL'équipe ALC Homes"),
    "de": ("Wir haben deine Buchung nicht gefunden — ALC Homes",
           "Hallo,\n\nWir konnten deine Buchung mit den angegebenen Daten nicht finden. Der Online-Check-in wird am Tag vor deiner Anreise freigeschaltet.\n\n"
           "Du kannst auf diese E-Mail mit deinem vollständigen Namen, wie in der Buchung angegeben, und deiner Bestätigungsnummer (Booking, Airbnb…) antworten "
           "oder den Kundenservice unter {tel} anrufen (rund um die Uhr).\n\nDas ALC-Homes-Team"),
    "val": ("No hem localitzat la teua reserva — ALC Homes",
            "Hola,\n\nNo hem pogut localitzar la teua reserva amb les dades que has indicat. Recorda que el check-in en línia s'activa el dia abans de la teua arribada.\n\n"
            "Pots respondre a aquest correu amb el teu nom complet, tal com figura en la reserva, i el número de confirmació (Booking, Airbnb…), "
            "o telefonar al servei d'atenció al client {tel} (24 h).\n\nEquip ALC Homes"),
}


def correo_enlace(idioma, url):
    """(asunto, cuerpo) del correo con el enlace personal."""
    asunto, cuerpo = _ENLACE.get(idioma) or _ENLACE["es"]
    return asunto, cuerpo.format(url=url, tel=TELEFONO)


def correo_no_localizada(idioma):
    """(asunto, cuerpo) del correo cuando no se ha encontrado la reserva."""
    asunto, cuerpo = _NO_LOCALIZADA.get(idioma) or _NO_LOCALIZADA["es"]
    return asunto, cuerpo.format(tel=TELEFONO)


def correo_codigos(idioma, url, nombre_habitacion, pin_habitacion, codigo_portal=None):
    """(asunto, cuerpo) con los códigos de acceso de un huésped ya alojado: portal del hostal (si lo hay) y habitación.
    Lo envía el equipo a mano en una emergencia (POST /enviar-codigos); solo en español e inglés."""
    es = idioma == "es"
    lineas = []
    if codigo_portal:
        lineas.append(f"• Puerta de la calle (portal): {codigo_portal}# — introduce el código y empuja la puerta." if es
                      else f"• Street door (building): {codigo_portal}# — enter the code and push the door.")
    lineas.append(f"• Tu habitación ({nombre_habitacion}): {pin_habitacion} — introdúcelo en el teclado de la puerta. "
                  "Pulsa el triángulo para cerrar desde fuera." if es
                  else f"• Your room ({nombre_habitacion}): {pin_habitacion} — enter it on the door keypad. "
                       "Press the triangle to lock from outside.")
    codigos = "\n".join(lineas)
    if es:
        return ("Tus códigos de acceso — ALC Homes",
                f"Hola,\n\nEstos son tus códigos de acceso:\n\n{codigos}\n\nTu enlace personal de check-in: {url}\n\n"
                f"Si tienes cualquier problema, llama al teléfono de atención al cliente {TELEFONO} (24 h).\n\nEquipo ALC Homes")
    return ("Your access codes — ALC Homes",
            f"Hello,\n\nHere are your access codes:\n\n{codigos}\n\nYour personal check-in link: {url}\n\n"
            f"If you have any problem, call customer service on {TELEFONO} (24 h).\n\nALC Homes Team")

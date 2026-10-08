"""
services/rebotes.py — Constancia y aviso de los correos devueltos (rebotes) que NO son de Booking.

Los emails a huéspedes (check-in de Hostelworld, «registro completado», respuestas automáticas del
buzón) salen de la cuenta de Gmail del buzón de huéspedes. Si la dirección del huésped no existe, su
buzón está lleno o su servidor lo rechaza, Gmail devuelve un rebote (mailer-daemon / postmaster) a
ese mismo buzón, y hasta ahora nadie se enteraba: el huésped se quedaba sin su enlace.

El script de Apps Script de ese buzón (procesarCorreosCheckin → revisarRebotes) manda aquí los rebotes
nuevos. Este módulo:
  - ignora los de Booking (las direcciones @guest.booking.com rebotan siempre, ya conocido y bloqueado
    en services/email_send.py: no se les envía nada),
  - analiza cada rebote (a quién iba, motivo, si es definitivo o temporal, qué correo era),
  - lo anota en Firestore (colección rebotes_email, un documento por mensaje de Gmail: dedupe y
    constancia) y busca la reserva a la que corresponde la dirección,
  - envía UN WhatsApp con lo ocurrido, en lenguaje claro, y qué hacer.

Del cuerpo del rebote solo se guardan los datos analizados (destinatario, motivo, asunto original),
no el texto completo.
"""
import logging
import re
from datetime import timedelta

import config
from services.beds24 import obtener_bookings_rango_beds24
from services.fechas import ahora_madrid, hoy_madrid
from services.whatsapp import enviar_whatsapp_callmebot

logger = logging.getLogger(__name__)

COLECCION = "rebotes_email"
MAX_MENSAJES = 20            # por llamada
MAX_CUERPO = 8000
MAX_EN_ALERTA = 5            # rebotes detallados en un mismo WhatsApp
CHECKIN_URL = "https://alc-homes-checkin.web.app/"
PROPIOS = ("alchomes2025.guest@gmail.com", "mailer-daemon", "postmaster")

_EMAIL = r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+"
# Formas habituales de decir «a quién iba»: DSN estándar, Gmail (dos formatos) y Outlook/Exchange
_DESTINATARIO = [
    re.compile(rf"Final-Recipient:\s*rfc822;\s*<?({_EMAIL})", re.I),
    re.compile(rf"wasn'?t delivered to\s+<?({_EMAIL})", re.I),
    re.compile(rf"Delivery to the following recipients?\s+(?:failed permanently|has been delayed|failed)[^\n]*:\s*<?({_EMAIL})", re.I),
    re.compile(rf"Your message to\s+<?({_EMAIL})>?\s+couldn'?t be delivered", re.I),
]
_MOTIVO = [
    re.compile(r"Diagnostic-Code:\s*(?:smtp;\s*)?(.+)", re.I),
    re.compile(r"(?:The response from the remote server was|The error that the other server returned was):\s*(.+?)(?:\n\s*\n|\Z)", re.I | re.S),
    re.compile(r"Remote Server returned '([^']+)'", re.I),
    re.compile(r"Technical details of (?:permanent|temporary) failure:\s*(.+?)(?:\n\s*\n|\Z)", re.I | re.S),
]
_CODIGO = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")
_ASUNTO_ORIGINAL = re.compile(r"^Subject:\s*(.+)$", re.I | re.M)

# (fragmento en minúsculas del motivo o del código) → explicación en lenguaje claro
EXPLICACIONES = [
    (("5.1.1", "5.1.0", "does not exist", "user unknown", "no such user", "address not found", "recipientnotfound", "couldn't be found"),
     "la dirección no existe (lo más probable es que esté mal escrita)"),
    (("5.1.2", "domain name not found", "domain not found", "host or domain", "nxdomain"),
     "el dominio de la dirección no existe (error al escribirla)"),
    (("5.2.2", "mailbox full", "over quota", "quota exceeded", "mailbox is full"),
     "el buzón del huésped está lleno"),
    (("5.7.", "spam", "blocked", "rejected", "policy", "denied"),
     "el servidor del huésped ha rechazado el correo (lo bloquea o lo considera spam)"),
]


def _limpiar(texto, maximo=220):
    return re.sub(r"\s+", " ", texto or "").strip(" .:-")[:maximo]


def analizar_rebote(asunto, remitente, cuerpo):
    """Datos de un rebote: {destinatario, motivo, codigo, tipo, explicacion, asunto_original, es_booking}.
    tipo: «permanente», «temporal» o «desconocido». Nunca falla: si no entiende el formato devuelve lo que pueda."""
    asunto, cuerpo = asunto or "", (cuerpo or "")[:MAX_CUERPO]
    destinatario = next((m.group(1) for r in _DESTINATARIO if (m := r.search(cuerpo))), None)
    if not destinatario:
        destinatario = next((e for e in re.findall(_EMAIL, cuerpo) if not any(p in e.lower() for p in PROPIOS)), None)
    destinatario = (destinatario or "").rstrip(".,;:>)").lower() or None

    motivo = next((_limpiar(m.group(1)) for r in _MOTIVO if (m := r.search(cuerpo))), "")
    if not motivo:   # sin línea técnica: la primera línea con texto que no sea una cabecera «** … **»
        motivo = next((_limpiar(l) for l in cuerpo.splitlines() if l.strip() and not l.strip().startswith("**")), "")
    codigo = (_CODIGO.search(motivo) or _CODIGO.search(cuerpo) or [None, None])[1]

    texto = f"{asunto} {motivo} {cuerpo[:600]}".lower()
    if "(delay)" in asunto.lower() or "has been delayed" in texto or (codigo or "").startswith("4"):
        tipo = "temporal"
    elif (codigo or "").startswith("5") or any(x in texto for x in ("failed permanently", "undeliverable", "wasn't delivered", "wasnt delivered", "couldn't be delivered", "(failure)")):
        tipo = "permanente"
    else:
        tipo = "desconocido"

    if tipo == "temporal":
        explicacion = "la entrega se ha retrasado; Gmail seguirá intentándolo unos días"
    else:
        explicacion = next((texto_ for claves, texto_ in EXPLICACIONES if any(c in f"{codigo or ''} {motivo}".lower() for c in claves)), "no se ha podido entregar")

    asuntos = [a.strip() for a in _ASUNTO_ORIGINAL.findall(cuerpo) if a.strip() and a.strip().lower() != asunto.strip().lower()]
    es_booking = "guest.booking.com" in f"{destinatario or ''} {cuerpo}".lower() or (destinatario or "").endswith("booking.com")
    return {"destinatario": destinatario, "motivo": motivo, "codigo": codigo, "tipo": tipo, "explicacion": explicacion,
            "asunto_original": _limpiar(asuntos[0], 120) if asuntos else None, "es_booking": es_booking}


def _tipo_correo(asunto_original):
    a = (asunto_original or "").lower()
    if "gracias por reservar" in a or "thank you for your reservation" in a:
        return "el enlace de check-in (Hostelworld)"
    if "registro completado" in a or "registration complete" in a:
        return "el aviso de «registro completado»"
    if a.startswith("re:"):
        return "una respuesta automática del buzón"
    return None


def _coleccion():
    return config.db.collection(COLECCION) if config.db is not None else None


def _ya_registrado(coleccion, mensaje_id):
    if coleccion is None:
        return False
    try:
        return coleccion.document(mensaje_id).get().exists
    except Exception as e:
        logger.error(f"[rebotes] Error leyendo Firestore: {e}")
        return False


def _buscar_reserva(email):
    """Reserva (llegada de hace 7 días a dentro de 60) cuyo email coincide con el del rebote, o None."""
    if not email:
        return None
    try:
        hoy = hoy_madrid()
        for e in obtener_bookings_rango_beds24((hoy - timedelta(days=7)).isoformat(), (hoy + timedelta(days=60)).isoformat(), tipo="checkin"):
            if (e.get("email") or "").strip().lower() == email:
                return {"habitacion": e.get("nombre_habitacion"), "huesped": e.get("huesped"), "canal": e.get("canal"),
                        "llegada": e.get("arrival"), "salida": e.get("departure")}
    except Exception as e:
        logger.error(f"[rebotes] No se pudo buscar la reserva de {email}: {e}")
    return None


def _fecha_corta(iso):
    try:
        return f"{iso[8:10]}/{iso[5:7]}"
    except Exception:
        return iso or "?"


def mensaje_aviso(rebotes):
    """WhatsApp en lenguaje claro para quien no conoce el sistema: qué ha pasado, a quién y qué hacer."""
    plural = len(rebotes) > 1
    lineas = [f"⚠️ ALCHOMES — {'Correos devueltos' if plural else 'Un correo devuelto'} (no es de Booking)", ""]
    for r in rebotes[:MAX_EN_ALERTA]:
        lineas.append(f"• {r['destinatario'] or 'dirección desconocida'}: {r['explicacion']}")
        if r.get("reserva"):
            rv = r["reserva"]
            lineas.append(f"  Reserva: {rv['habitacion']} · {rv['huesped']} · {rv['canal']} · llega el {_fecha_corta(rv['llegada'])}")
        detalle = _tipo_correo(r.get("asunto_original"))
        if detalle:
            lineas.append(f"  Era {detalle}.")
        if r.get("motivo"):
            lineas.append(f"  Detalle técnico: {r['motivo'][:140]}")
    if len(rebotes) > MAX_EN_ALERTA:
        lineas.append(f"… y {len(rebotes) - MAX_EN_ALERTA} más (ver /rebotes).")
    lineas.append("")
    if all(r["tipo"] == "temporal" for r in rebotes):
        lineas.append("Qué hacer: de momento nada. Gmail lo reintentará; si acaba fallando recibirás otro aviso.")
    else:
        lineas += ["Qué hacer:",
                   "1. Abre la reserva en Beds24 y comprueba el email del huésped.",
                   "2. Si está mal escrito, corrígelo en la reserva.",
                   f"3. Mientras tanto, contacta al huésped por otro medio (teléfono o mensajes de la plataforma) y pásale el enlace de check-in: {CHECKIN_URL}"]
    return "\n".join(lineas)


def procesar_rebotes(mensajes):
    """
    Recibe los mensajes de rebote que ha encontrado el script ([{id, fecha, de, asunto, cuerpo}]), registra
    los nuevos que no son de Booking y avisa por WhatsApp. Devuelve {"nuevos", "ignorados_booking", "repetidos"}.
    Si falla el envío del WhatsApp lanza la excepción: el script no avanza y lo reintenta en la siguiente pasada,
    y como nada se ha anotado todavía no se pierde ni se duplica.
    """
    coleccion = _coleccion()
    nuevos, booking, repetidos = [], 0, 0
    for m in (mensajes or [])[:MAX_MENSAJES]:
        mensaje_id = str(m.get("id") or "").strip()
        if not mensaje_id or _ya_registrado(coleccion, mensaje_id):
            repetidos += 1
            continue
        datos = analizar_rebote(m.get("asunto"), m.get("de"), m.get("cuerpo"))
        if datos["es_booking"]:
            booking += 1
            continue
        nuevos.append({"id": mensaje_id, "fecha_rebote": str(m.get("fecha") or ""), **datos})

    if nuevos:
        for r in nuevos:
            r["reserva"] = _buscar_reserva(r["destinatario"])
        enviar_whatsapp_callmebot(mensaje_aviso(nuevos))
        if coleccion is not None:
            for r in nuevos:
                try:
                    coleccion.document(r["id"]).set({k: v for k, v in r.items() if k != "id"} | {"registrado": ahora_madrid().isoformat()})
                except Exception as e:
                    logger.error(f"[rebotes] Error guardando el rebote {r['id']}: {e}")
    return {"nuevos": len(nuevos), "ignorados_booking": booking, "repetidos": repetidos}


def ultimos_rebotes(limite=30):
    """Los últimos rebotes anotados (más recientes primero); lista vacía si no hay Firestore."""
    coleccion = _coleccion()
    if coleccion is None:
        return []
    docs = coleccion.order_by("registrado", direction="DESCENDING").limit(limite).stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]

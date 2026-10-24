"""
services/reservas_correo.py — Cotejo de los avisos de reserva que llegan por correo con el calendario de Beds24.

Booking, Airbnb, Hostelworld y las demás plataformas avisan por correo (buzón alchomes2025@gmail.com) cada vez que
entra, se modifica o se cancela una reserva. Si Beds24 no la recibe (canal caído, sincronización fallida, sobreventa en
camino), nadie se enteraba hasta que el huésped llamaba. Aquí se comprueba cada aviso contra Beds24 y se avisa por
WhatsApp/Telegram SOLO cuando, pasada una espera razonable, no cuadra.

Piezas:
  - Apps Script del buzón (solo lee, nunca responde) → POST /reserva-correo con los avisos nuevos → recibir_avisos():
    analiza cada uno (plataforma, tipo, nº de reserva, fechas) y lo deja «pendiente» en Firestore (reservas_correo).
  - El watchdog (cada 15 min) llama a verificar_pendientes(): una sola consulta a Beds24 por pasada, y solo si hay pendientes.
        nueva / modificada → debe existir, no cancelada, con las mismas fechas (si el correo las trae)
        cancelada          → no debe seguir activa en Beds24
    Si cuadra: «verificada». Si no cuadra y el aviso lleva más de ESPERA_MINUTOS: UN aviso; si más tarde cuadra, UN «ya cuadra».
    Si no se puede consultar Beds24 NO se avisa (no se sabe si cuadra); se reintenta en la siguiente pasada.
  - Un aviso que no se entiende (formato nuevo) queda como «sin_entender»: se anota para ver y ajustar el analizador,
    pero no se da la alarma.

Del correo solo se guardan los datos analizados y el asunto, no el cuerpo.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

import config
from config import BEDS24_API_BASE, BEDS24_PROPERTY_IDS
from services.beds24 import (CAMPOS_ID_INTERNOS, ROOM_ID_DISPLAY_NAME, _canal_legible, _extraer_nombre_huesped_beds24,
                             _ref_en_booking, es_bloqueo_beds24, get_beds24_access_token)
from services.fechas import hoy_madrid
from services.whatsapp import enviar_whatsapp_callmebot

logger = logging.getLogger(__name__)

COLECCION = "reservas_correo"
ESPERA_MINUTOS = 20          # Beds24 tarda unos minutos en recibir la reserva del canal (caso real: ~9 min)
VIGILAR_HORAS = 36           # tras este tiempo sin cuadrar se deja de comprobar (ya se avisó una vez)
MAX_MENSAJES = 40            # por llamada
MAX_CUERPO = 12000
MAX_EN_ALERTA = 6

# ── Análisis del correo ──────────────────────────────────────────────────

_PLATAFORMAS = (("booking.com", "Booking.com"), ("airbnb", "Airbnb"), ("hostelworld", "Hostelworld"),
                ("holidu", "Holidu"), ("expedia", "Expedia"), ("vrbo", "Vrbo"), ("agoda", "Agoda"))
# Orden importa: «cancelada» primero (un asunto de cancelación suele nombrar también la reserva); luego modificada; luego nueva.
_TIPOS = (
    ("cancelada", ("cancel", "anulad", "stornier", "annul")),
    ("modificada", ("modif", "changed", "change to", "updated", "alteration", "amended", "cambio en", "ha cambiado", "reservation change")),
    ("nueva", ("new booking", "new reservation", "confirmed booking", "nueva reserva", "reserva nueva", "booking confirmed", "reservation confirmed",
               "reserva confirmada", "confirmed:", "booking confirmation", "confirmación de reserva", "has booked", "ha reservado",
               "you have a new", "tienes una nueva", "instant booking", "reservation request")),
)
_ETIQUETA_REF = r"(?:reserva|reservation|booking|confirmation|confirmaci[oó]n|c[oó]digo|code|n[ºo°]\.?|number|n[uú]mero|ref(?:erence|erencia)?)"
# Aviso del propio Beds24 («Booking: <casa>: <habitación> - Wed 28 Oct 2026: 95179058 - Huésped - Canal - New Booking Notification…»):
# lo manda Beds24 cuando YA tiene la reserva, así que compararlo con Beds24 no detecta nada. Se ignora.
_AVISO_BEDS24 = re.compile(r"^\s*Booking(?: Cancelled)?:\s.+?:\s.+?\s-\s[A-Za-z]{3} \d{1,2} [A-Za-z]{3} \d{4}:\s*\d+", re.I)
# Error de canal que avisa el propio Beds24: «Booking.com returned an error message for <casa> - roomId 702395 - …».
_ERROR_CANAL = re.compile(r"^\s*(?P<canal>[A-Za-z][\w.]*?)(?:\.com)? returned an error message for (?P<casa>.+?) - roomId (?P<room>\d+)", re.I)
_REF_HOSTELWORLD = re.compile(r"(?<![\d-])(\d{5,7}-\d{8,10})(?![\d-])")
_REF_TRIP = re.compile(r"#(\d{13,18})#")
_REF_HOLIDU = re.compile(r"(?:ID DE LA RESERVA|booking id|reservation id)\W{0,5}(\d{5,9})|\((\d{5,9})\)", re.I)
_RANGO_FECHAS = re.compile(r"(?:fechas|dates)\W{0,5}" + r"(\d{1,2}\s+" + r"[a-záéíóú]{3,10}\.?\s+\d{4})\s*[-–]\s*(\d{1,2}\s+[a-záéíóú]{3,10}\.?\s+\d{4})", re.I)
_REF_NUM = re.compile(rf"{_ETIQUETA_REF}[^\d\n]{{0,25}}(\d{{8,12}})\b", re.I)
_REF_AIRBNB = re.compile(r"\b(HM[A-Z0-9]{8})\b")
_REF_SUELTA = re.compile(r"\b(\d{9,10})\b")
_MESES = {"ene": 1, "jan": 1, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "aug": 8,
          "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12, "dec": 12}
_MES_TXT = r"(?:ene|jan|feb|mar|abr|apr|may|jun|jul|ago|aug|sep|set|oct|nov|dic|dec)[a-z]*\.?"
_FECHA = (r"(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{1,2}(?:\s+de)?\s+" + _MES_TXT + r"(?:\s+de)?\s+\d{4}|" + _MES_TXT + r"\s+\d{1,2},?\s+\d{4})")
_LLEGADA = re.compile(r"(?:check-?in|entrada|llegada|arrival|arriving)\W{0,12}(?:[a-záéíóú]{3,9},?\s+)?" + _FECHA, re.I)
_SALIDA = re.compile(r"(?:check-?out|salida|departure|departing)\W{0,12}(?:[a-záéíóú]{3,9},?\s+)?" + _FECHA, re.I)


def _a_iso(texto):
    """Fecha en cualquiera de los formatos habituales de los correos → 'AAAA-MM-DD', o None."""
    t = re.sub(r"\s+", " ", (texto or "").strip().lower())
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        a, mes, d = map(int, m.groups())
    elif (m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", t)):
        d, mes, a = map(int, m.groups())        # día/mes/año, como en los correos de las plataformas en español
    elif (m := re.fullmatch(r"(\d{1,2})(?: de)? ([a-z]+)\.?(?: de)? (\d{4})", t)):
        d, a = int(m.group(1)), int(m.group(3))
        mes = _MESES.get(m.group(2)[:3], 0)
    elif (m := re.fullmatch(r"([a-z]+)\.? (\d{1,2}),? (\d{4})", t)):
        d, a = int(m.group(2)), int(m.group(3))
        mes = _MESES.get(m.group(1)[:3], 0)
    else:
        return None
    try:
        return datetime(a, mes, d).date().isoformat()
    except ValueError:
        return None


def analizar_aviso(asunto, remitente, cuerpo):
    """Datos de un aviso de reserva: {plataforma, tipo, ref, llegada, salida, entendido}.
    tipo: nueva | modificada | cancelada | None. Nunca falla: lo que no entiende queda en None.
    `entendido` es True solo si hay tipo Y número de reserva (sin eso no se puede cotejar)."""
    asunto, cuerpo = asunto or "", (cuerpo or "")[:MAX_CUERPO]
    if (m_err := _ERROR_CANAL.match(asunto)):
        return {"plataforma": m_err.group("canal") + ".com", "tipo": "error_canal", "ref": m_err.group("room"), "llegada": None,
                "salida": None, "entendido": False, "error_canal": True}
    if _AVISO_BEDS24.match(asunto):
        return {"plataforma": "Beds24", "tipo": None, "ref": None, "llegada": None, "salida": None, "entendido": False, "ignorar": True}
    origen = f"{remitente or ''} {asunto}".lower()
    plataforma = next((nombre for clave, nombre in _PLATAFORMAS if clave in origen), None)
    plataforma = plataforma or next((nombre for clave, nombre in _PLATAFORMAS if clave in cuerpo[:800].lower()), None)

    asunto_l = asunto.lower()
    tipo = next((t for t, claves in _TIPOS if any(c in asunto_l for c in claves)), None)
    if tipo is None:
        inicio = cuerpo[:400].lower()
        tipo = next((t for t, claves in _TIPOS if any(c in inicio for c in claves)), None)

    if re.search(r"hostelworld", origen + cuerpo[:300].lower()):
        plataforma = "Hostelworld"
    elif "noreply_htl" in origen or re.search(r"booking no\. #\d", asunto, re.I):
        plataforma = "Trip.com"                      # «Booking no. #…# accepted» / «Cancellation request accepted (booking no. #…#)»
        if tipo is None and "accepted" in asunto_l:
            tipo = "nueva"

    ref = None
    for fuente in (asunto, cuerpo):
        m = ((_REF_TRIP.search(fuente) if plataforma == "Trip.com" else None)
             or (_REF_HOSTELWORLD.search(fuente) if plataforma == "Hostelworld" else None)
             or (_REF_HOLIDU.search(fuente) if plataforma == "Holidu" else None)
             or _REF_AIRBNB.search(fuente) or _REF_NUM.search(fuente))
        if m:
            ref = next(g for g in m.groups() if g)
            break
    if ref is None and plataforma == "Booking.com":
        m = _REF_SUELTA.search(asunto) or _REF_SUELTA.search(cuerpo)
        ref = m.group(1) if m else None

    texto = f"{asunto}\n{cuerpo}"
    m_llegada, m_salida = _LLEGADA.search(texto), _SALIDA.search(texto)
    llegada, salida = _a_iso(m_llegada.group(1)) if m_llegada else None, _a_iso(m_salida.group(1)) if m_salida else None
    if not (llegada and salida) and (m_rango := _RANGO_FECHAS.search(texto)):   # Holidu: «FECHAS: 17 sept 2026 - 20 sept 2026»
        llegada, salida = _a_iso(m_rango.group(1)), _a_iso(m_rango.group(2))
    if llegada and salida and salida <= llegada:
        llegada = salida = None            # fechas incoherentes: mejor no comparar que comparar mal
    return {"plataforma": plataforma, "tipo": tipo, "ref": ref, "llegada": llegada, "salida": salida,
            "entendido": bool(tipo and ref)}


# ── Cotejo con Beds24 ────────────────────────────────────────────────────

def _leer_beds24(desde, hasta):
    """Reservas (crudas, CON las canceladas) de todas las propiedades con llegada entre `desde` y `hasta` (ISO).
    Lanza si no se puede consultar."""
    token = get_beds24_access_token()
    resultado = []
    for property_id in BEDS24_PROPERTY_IDS:
        resp = requests.get(f"{BEDS24_API_BASE}/bookings", headers={"token": token, "accept": "application/json"},
                            params={"propertyId": property_id, "arrivalFrom": desde, "arrivalTo": hasta, "limit": 500,
                                    "includePersonalInfo": "true"}, timeout=20)
        resp.raise_for_status()
        resultado += [b for b in resp.json().get("data", []) if not es_bloqueo_beds24(b)]
    return resultado


def _coincidencias(reservas, ref):
    """Reservas de Beds24 cuyo campo de referencia (no un id interno) es exactamente `ref`."""
    ref = str(ref or "").strip().lower()
    if len(ref) < 5:
        return []
    return [b for b in reservas if (campo := _ref_en_booking(b, ref)) is not None and campo not in CAMPOS_ID_INTERNOS]


def _activa(b):
    return str(b.get("status", "")).lower() not in ("cancelled", "canceled")


def cotejar(aviso, reservas):
    """Resultado de comparar un aviso con las reservas de Beds24: (veredicto, detalle).
    veredicto: «ok» | «no_existe» | «fechas» | «cancelada_en_beds24» | «sigue_activa»."""
    encontradas = _coincidencias(reservas, aviso.get("ref"))
    activas = [b for b in encontradas if _activa(b)]
    if aviso["tipo"] == "cancelada":
        return ("sigue_activa", activas[0]) if activas else ("ok", None)
    if not encontradas:
        return "no_existe", None
    if not activas:
        return "cancelada_en_beds24", encontradas[0]
    b = activas[0]
    if (aviso.get("llegada") and b.get("arrival") != aviso["llegada"]) or (aviso.get("salida") and b.get("departure") != aviso["salida"]):
        return "fechas", b
    return "ok", b


# ── Estado en Firestore ──────────────────────────────────────────────────

def _coleccion():
    return config.db.collection(COLECCION) if config.db is not None else None


def _ahora():
    return datetime.now(timezone.utc)


def _minutos(iso, ahora):
    try:
        return (ahora - datetime.fromisoformat(iso)).total_seconds() / 60
    except Exception:
        return None


def recibir_avisos(mensajes):
    """Registra los avisos de reserva nuevos que manda el script del buzón ([{id, fecha, de, asunto, cuerpo}]) como
    «pendiente» (o «sin_entender»). No consulta Beds24: eso lo hace el watchdog. Devuelve {"nuevos", "sin_entender", "repetidos"}.
    Lanza si no hay Firestore: sin él no habría dónde recordar qué falta por comprobar y el script debe reintentar."""
    coleccion = _coleccion()
    if coleccion is None:
        raise RuntimeError("Firestore no disponible")
    nuevos = sin_entender = repetidos = ignorados = errores = 0
    for m in (mensajes or [])[:MAX_MENSAJES]:
        mensaje_id = re.sub(r"[^\w-]", "", str(m.get("id") or ""))[:80]
        if not mensaje_id or coleccion.document(mensaje_id).get().exists:
            repetidos += 1
            continue
        datos = analizar_aviso(m.get("asunto"), m.get("de"), m.get("cuerpo"))
        if datos.pop("error_canal", False):
            coleccion.document(mensaje_id).set({**datos, "estado": "error_canal", "asunto": str(m.get("asunto") or "")[:200],
                                                "recibido": _ahora().isoformat(), "avisado": False})
            errores += 1
            continue
        if datos.pop("ignorar", False):
            ignorados += 1
            continue
        estado = "pendiente" if datos["entendido"] else "sin_entender"
        coleccion.document(mensaje_id).set({
            **datos, "estado": estado, "asunto": str(m.get("asunto") or "")[:200], "de": str(m.get("de") or "")[:120],
            "fecha_correo": str(m.get("fecha") or ""), "recibido": _ahora().isoformat(), "avisado": False})
        nuevos += estado == "pendiente"
        sin_entender += estado == "sin_entender"
    return {"nuevos": nuevos, "sin_entender": sin_entender, "repetidos": repetidos, "ignorados": ignorados, "errores_canal": errores}


# ── Avisos ───────────────────────────────────────────────────────────────

_MOTIVOS = {
    "no_existe": "NO está en Beds24",
    "fechas": "está en Beds24 pero con OTRAS fechas",
    "cancelada_en_beds24": "en Beds24 figura CANCELADA",
    "sigue_activa": "el correo dice que se canceló, pero en Beds24 sigue ACTIVA",
}
_TIPO_TXT = {"nueva": "nueva reserva", "modificada": "modificación", "cancelada": "cancelación"}


def _fecha_corta(iso):
    try:
        return f"{iso[8:10]}/{iso[5:7]}"
    except Exception:
        return "?"


def _linea(p):
    fechas = f" · {_fecha_corta(p['llegada'])}→{_fecha_corta(p['salida'])}" if p.get("llegada") and p.get("salida") else ""
    return f"• {p.get('plataforma') or 'Plataforma desconocida'} · {_TIPO_TXT.get(p['tipo'], '?')} · nº {p['ref']}{fechas}"


def _detalle_beds24(b):
    if not b:
        return ""
    room = str(b.get("roomId", ""))
    return (f"\n  En Beds24: {ROOM_ID_DISPLAY_NAME.get(room, f'Room {room}')} · {_extraer_nombre_huesped_beds24(b)} · "
            f"{_canal_legible(b)} · {_fecha_corta(b.get('arrival'))}→{_fecha_corta(b.get('departure'))} · nº Beds24 {b.get('id')}")


def mensaje_aviso(problemas):
    """WhatsApp en lenguaje claro: qué aviso llegó por correo, qué pasa en Beds24 y qué hacer."""
    plural = len(problemas) > 1
    lineas = [f"⚠️ ALCHOMES — {'Reservas del correo que no cuadran' if plural else 'Una reserva del correo no cuadra'} con Beds24", "",
              f"{'Estos avisos llegaron' if plural else 'Este aviso llegó'} por correo hace más de {ESPERA_MINUTOS} min y Beds24 "
              f"(nuestro calendario) {'no los refleja' if plural else 'no lo refleja'} bien:", ""]
    for p, veredicto, b in problemas[:MAX_EN_ALERTA]:
        lineas.append(_linea(p) + f": {_MOTIVOS[veredicto]}{_detalle_beds24(b)}")
    if len(problemas) > MAX_EN_ALERTA:
        lineas.append(f"… y {len(problemas) - MAX_EN_ALERTA} más (ver /reservas-correo).")
    lineas += ["", "Por qué importa: si la plataforma vendió la noche y Beds24 no lo sabe, esa noche puede venderse otra vez (sobreventa).", "",
               "Qué hacer:",
               "1. Abre la reserva en la plataforma (o en su correo) y en Beds24 y compara fechas y habitación.",
               "2. Si falta en Beds24, bloquea esas fechas en las demás plataformas y créala a mano en Beds24.",
               "3. Si Beds24 sigue sin recibir reservas de ese canal, avisa a Adrián: puede haberse caído la conexión.",
               "", "Este aviso no se repetirá; cuando cuadre te llegará otro para confirmarlo."]
    return "\n".join(lineas)


def mensaje_resuelto(resueltos):
    plural = len(resueltos) > 1
    lineas = [f"✅ ALCHOMES — {'Ya cuadran' if plural else 'Ya cuadra'} {'las reservas' if plural else 'la reserva'} del correo con Beds24", "",
              f"{'Las reservas' if plural else 'La reserva'} de {'las' if plural else 'la'} que te avisamos antes ya {'están' if plural else 'está'} bien en Beds24. No hace falta hacer nada.", ""]
    lineas += [_linea(p) for p in resueltos[:MAX_EN_ALERTA]]
    return "\n".join(lineas)


# ── Pasada del watchdog ──────────────────────────────────────────────────

def _rango(pendientes):
    """Ventana de llegadas a pedir a Beds24: de hace 5 días a dentro de un año, ampliada si algún aviso trae fechas fuera de ella."""
    hoy = hoy_madrid()
    llegadas = [p["llegada"] for p in pendientes if p.get("llegada")]
    desde = min(llegadas + [(hoy - timedelta(days=5)).isoformat()])
    hasta = max(llegadas + [(hoy + timedelta(days=365)).isoformat()])
    return (datetime.fromisoformat(desde).date() - timedelta(days=1)).isoformat(), (datetime.fromisoformat(hasta).date() + timedelta(days=1)).isoformat()


def verificar_pendientes(ahora=None, leer_beds24=None):
    """Comprueba los avisos pendientes (y los ya avisados, por si cuadran después) contra Beds24, con UNA consulta por
    propiedad. Para llamar desde /watchdog; no lanza hacia arriba. Devuelve un resumen {"verificadas", "avisadas", "resueltas", "esperando"}.
    Si falla un envío no se anota, y se reintenta en la siguiente pasada."""
    coleccion = _coleccion()
    vacio = {"verificadas": 0, "avisadas": 0, "resueltas": 0, "esperando": 0}
    if coleccion is None:
        return vacio
    ahora = ahora or _ahora()
    docs = [(d.id, d.to_dict() or {}) for d in coleccion.where("estado", "in", ["pendiente", "avisada"]).stream()]
    if not docs:
        return vacio
    try:
        reservas = (leer_beds24 or _leer_beds24)(*_rango([d for _, d in docs]))
    except Exception as e:
        logger.error(f"[reservas_correo] No se pudo consultar Beds24 (no se avisa de nada esta pasada): {e}")
        return vacio

    resumen, problemas, resueltos, cambios = dict(vacio), [], [], []
    for doc_id, p in docs:
        minutos = _minutos(p.get("recibido"), ahora)
        veredicto, b = cotejar(p, reservas)
        if veredicto == "ok":
            if p["estado"] == "avisada":
                resueltos.append((doc_id, p))
            else:
                cambios.append((doc_id, {"estado": "verificada", "verificada": ahora.isoformat()}))
                resumen["verificadas"] += 1
        elif p["estado"] == "pendiente":
            if minutos is not None and minutos >= ESPERA_MINUTOS:
                problemas.append((doc_id, p, veredicto, b))
            else:
                resumen["esperando"] += 1
        elif minutos is not None and minutos >= VIGILAR_HORAS * 60:
            cambios.append((doc_id, {"estado": "caducada"}))

    if problemas:
        enviar_whatsapp_callmebot(mensaje_aviso([(p, v, b) for _, p, v, b in problemas]))
        for doc_id, p, veredicto, _ in problemas:
            cambios.append((doc_id, {"estado": "avisada", "avisado": True, "motivo": veredicto, "avisada_en": ahora.isoformat()}))
        resumen["avisadas"] = len(problemas)
    if resueltos:
        enviar_whatsapp_callmebot(mensaje_resuelto([p for _, p in resueltos]))
        for doc_id, _ in resueltos:
            cambios.append((doc_id, {"estado": "verificada", "verificada": ahora.isoformat()}))
        resumen["resueltas"] = len(resueltos)
    for doc_id, campos in cambios:
        try:
            coleccion.document(doc_id).update(campos)
        except Exception as e:
            logger.error(f"[reservas_correo] No se pudo anotar el estado de {doc_id}: {e}")
    return resumen


# ── Errores de canal (Beds24 → Booking/Expedia…) ─────────────────────────

ERRORES_VENTANA_HORAS = 3        # errores de la misma plataforma dentro de esta ventana forman un mismo episodio
ERRORES_MIN_HABITACIONES = 2     # un error suelto puede ser un fallo puntual; en dos o más habitaciones, es del canal
ERRORES_SILENCIO_HORAS = 6       # tras avisar de un canal, no se vuelve a avisar de él durante este tiempo
URL_ESTADO_BOOKING = "https://connectivity-status.booking.com/incidents.html"


def mensaje_errores_canal(canal, habitaciones, desde_iso):
    nombres = ", ".join(ROOM_ID_DISPLAY_NAME.get(r, f"Room {r}") for r in sorted(habitaciones))
    lineas = [f"⚠️ ALCHOMES — {canal} está rechazando actualizaciones de Beds24", "",
              f"Beds24 (nuestro calendario) ha avisado por correo de errores al actualizar {canal} en {len(habitaciones)} habitaciones: {nombres}.", "",
              "Suele ser un problema del propio canal y no nuestro: el 22-oct Booking.com tuvo un incidente crítico en sus APIs y se recuperó solo. "
              "No hace falta reautorizar nada ni tocar el sistema.", "",
              "Qué hacer:"]
    if canal.lower().startswith("booking"):
        lineas.append(f"1. Mira si Booking.com tiene un incidente abierto: {URL_ESTADO_BOOKING} (la página carga con JavaScript).")
    else:
        lineas.append(f"1. Mira la página de estado de {canal}, por si tiene un incidente abierto.")
    lineas += ["2. Mientras dure, una reserva nueva puede tardar en reflejarse en las demás plataformas: si entra alguna, comprueba que esas noches están bloqueadas en todas.",
               "3. Si sigue así pasadas 2-3 horas, avisa a Adrián.", "",
               f"Este aviso no se repetirá para {canal} en {ERRORES_SILENCIO_HORAS} h."]
    return "\n".join(lineas)


def avisar_errores_canal(ahora=None):
    """Avisa (una vez por episodio y canal) cuando Beds24 notifica errores de un canal en varias habitaciones a la vez.
    Para llamar desde /watchdog; devuelve los canales avisados. Si falla el envío no se anota y se reintenta en la siguiente pasada."""
    coleccion = _coleccion()
    if coleccion is None:
        return []
    ahora = ahora or _ahora()
    por_canal = {}
    for d in coleccion.where("estado", "in", ["error_canal"]).stream():
        e = d.to_dict() or {}
        minutos = _minutos(e.get("recibido"), ahora)
        if minutos is not None and minutos <= ERRORES_VENTANA_HORAS * 60:
            por_canal.setdefault(e.get("plataforma") or "?", {"rooms": set(), "desde": e["recibido"]})["rooms"].add(str(e.get("ref")))
    if not por_canal:
        return []
    estado_ref = coleccion.document("_estado")
    estado = estado_ref.to_dict() if estado_ref.get().exists else {}
    avisados = []
    for canal, datos in por_canal.items():
        if len(datos["rooms"]) < ERRORES_MIN_HABITACIONES:
            continue
        ultimo = _minutos(estado.get(canal), ahora)
        if ultimo is not None and ultimo < ERRORES_SILENCIO_HORAS * 60:
            continue
        enviar_whatsapp_callmebot(mensaje_errores_canal(canal, datos["rooms"], datos["desde"]))
        estado[canal] = ahora.isoformat()
        avisados.append(canal)
    if avisados:
        try:
            estado_ref.set(estado)
        except Exception as e:
            logger.error(f"[reservas_correo] No se pudo anotar el aviso de errores de canal: {e}")
    return avisados


def ultimos_avisos(limite=40):
    """Los últimos avisos recibidos con su estado (más recientes primero); vacío si no hay Firestore."""
    coleccion = _coleccion()
    if coleccion is None:
        return []
    docs = coleccion.order_by("recibido", direction="DESCENDING").limit(limite).stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]

"""
services/rpv_sin_importar.py — Aviso de WhatsApp cuando una reserva de Beds24 lleva
demasiado tiempo sin aparecer en RPV.

RPV importa las reservas de Beds24 con cierto retraso (integración nativa "b24",
frecuencia por confirmar; antes, el iCal una vez al día), así que que una reserva
recién creada no conste todavía es lo normal y NO se avisa. Se avisa cuando pasan
las horas que marca services.resumen.horas_normales_sin_rpv (menos si el huésped llega hoy
o mañana) desde que se creó o se modificó la reserva y RPV sigue sin conocerla: entonces
algo falla (iCal caído, integración parada, reserva que RPV no ha entendido) y el huésped
no podría hacer su parte.

Solo se comprueban las entradas de hoy a hoy+VENTANA_DIAS-1 (la ventana que
devuelve RPV) de las habitaciones que tienen cuenta de RPV. Si RPV no responde
para una habitación NO se avisa (no se sabe si consta o no; eso ya lo cubre el
watchdog). Se avisa una sola vez por reserva: dedupe por book_id en Firestore
(system_state/rpv_sin_importar), podado por fecha de llegada.

Cuando una reserva de la que ya se avisó acaba apareciendo en RPV (la importó RPV o alguien la creó a mano) se manda
UN segundo mensaje de confirmación («ya consta, no hace falta hacer nada», con lo que tardó); se anota en
`resueltas` para no repetirlo. Si la reserva se cancela o sale de la ventana de RPV antes de aparecer, no se confirma.

Con los mismos datos de cada pasada (una sola consulta a Beds24 y a RPV) se alimenta además la medición
del retraso real de RPV (services/rpv_latencia.py).
"""
import logging
from datetime import datetime, timedelta, timezone

import config
from services import rpv_latencia
from services.beds24 import obtener_bookings_rango_beds24
from services.fechas import hoy_madrid
from services.resumen import _hace, horas_normales_sin_rpv
from services.rpv import TTL_SEGUNDO_PLANO, VENTANA_DIAS, habitaciones_cubiertas, obtener_estado_partes
from services.whatsapp import enviar_whatsapp_callmebot

logger = logging.getLogger(__name__)


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("rpv_sin_importar")


def _leer_estado():
    """({book_id (str): fecha de llegada ISO} de las reservas ya avisadas, ídem de las avisadas que después han
    llegado a RPV y ya se han confirmado). None si Firestore no está disponible o falla la lectura (sin dedupe
    se repetiría el aviso cada 15 min)."""
    ref = _doc_ref()
    if ref is None:
        return None
    try:
        doc = ref.get()
        datos = (doc.to_dict() or {}) if doc.exists else {}
        return ({str(k): v for k, v in (datos.get("avisados") or {}).items()},
                {str(k): v for k, v in (datos.get("resueltas") or {}).items()})
    except Exception as e:
        logger.error(f"[rpv_sin_importar] Error leyendo estado en Firestore: {e}")
        return None


def _guardar_estado(avisados, resueltas, hoy_iso):
    ref = _doc_ref()
    try:
        ref.set({"avisados": {k: v for k, v in avisados.items() if v >= hoy_iso},
                 "resueltas": {k: v for k, v in resueltas.items() if v >= hoy_iso}})
        return True
    except Exception as e:
        logger.error(f"[rpv_sin_importar] Error guardando estado en Firestore: {e}")
        return False


def _horas_sin_cambios(entrada, ahora_utc):
    """Horas desde la última vez que la reserva se creó o modificó (la más reciente de
    las dos); None si Beds24 no da ninguna fecha legible."""
    horas = [h for h in (_hace(entrada.get("creada"), ahora_utc), _hace(entrada.get("modificada"), ahora_utc)) if h is not None]
    return min(horas) if horas else None


def leer_datos():
    """Lo que ve esta pasada: {"estados", "cubiertas", "entradas"} (RPV y Beds24, una consulta de cada).
    `cubiertas` son las habitaciones cuya cuenta de RPV ha respondido; sin ninguna, no se consulta Beds24."""
    estados, sin_verificar = obtener_estado_partes(max_age=TTL_SEGUNDO_PLANO)
    cubiertas = habitaciones_cubiertas() - sin_verificar
    entradas = []
    if cubiertas:
        hoy = hoy_madrid()
        entradas = obtener_bookings_rango_beds24(hoy.isoformat(), (hoy + timedelta(days=VENTANA_DIAS - 1)).isoformat(), tipo="checkin")
    return {"estados": estados, "cubiertas": cubiertas, "entradas": entradas}


def candidatas_sin_importar(ahora_utc=None, datos=None):
    """Reservas que RPV debería conocer ya y no conoce, sin enviar nada ni tocar Firestore:
    lista de dicts {book_id, nombre_habitacion, huesped, canal, arrival, departure, horas, umbral}. Separada del
    envío para poder verla desde el endpoint de diagnóstico."""
    ahora_utc = ahora_utc or datetime.now(timezone.utc)
    datos = datos or leer_datos()
    hoy = hoy_madrid()
    candidatas = []
    for e in datos["entradas"]:
        if e["room_id"] not in datos["cubiertas"] or (e["room_id"], e["arrival"]) in datos["estados"]:
            continue
        if str(e.get("status") or "").lower() == "black":   # bloqueo de fechas, no es una reserva
            continue
        horas = _horas_sin_cambios(e, ahora_utc)
        umbral = horas_normales_sin_rpv(e["arrival"], hoy)
        if horas is None or horas < umbral:
            continue
        candidatas.append({"book_id": str(e.get("book_id")), "nombre_habitacion": e["nombre_habitacion"],
                           "huesped": e.get("huesped", "?"), "canal": e.get("canal", "Desconocido"),
                           "arrival": e["arrival"], "departure": e.get("departure"), "horas": horas, "umbral": umbral})
    return sorted(candidatas, key=lambda c: (c["arrival"], c["nombre_habitacion"]))


def _antiguedad(horas):
    return f"{horas} h" if horas < 48 else f"{horas // 24} días"


def _fecha_corta(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m")
    except Exception:
        return iso or "?"


def mensaje_aviso(nuevas):
    """Texto pensado para quien no conoce el sistema (también lo recibe la persona
    encargada de las reservas): qué pasa, por qué importa y qué hacer, sin jerga."""
    plural = len(nuevas) > 1
    umbral = min(c["umbral"] for c in nuevas)
    lineas = [f"⚠️ ALCHOMES — {'Reservas que no han llegado' if plural else 'Una reserva no ha llegado'} a RPV", ""]
    lineas.append(
        f"{'Estas reservas llevan' if plural else 'Esta reserva lleva'} más de {umbral} h en Beds24 (nuestro gestor de reservas) "
        f"y todavía no {'aparecen' if plural else 'aparece'} en RPV, la web donde se hace el registro oficial de viajeros. "
        f"Lo normal es que ya {'estén' if plural else 'esté'}.")
    lineas.append("")
    for c in nuevas:
        salida = f" · sale el {_fecha_corta(c['departure'])}" if c.get("departure") else ""
        lineas.append(f"• {c['nombre_habitacion']} · {c['huesped']} · {c['canal']} · llega el {_fecha_corta(c['arrival'])}{salida} · lleva {_antiguedad(c['horas'])} sin llegar a RPV · nº Beds24 {c['book_id']}")
    lineas += [
        "",
        f"Por qué importa: si no {'están' if plural else 'está'} en RPV, {'esos huéspedes pueden' if plural else 'el huésped puede'} tener problemas para registrarse y recibir {'sus' if plural else 'su'} código{'s' if plural else ''} de entrada.",
        "",
        "Qué hacer:",
        f"1. Entra en RPV y busca {'cada reserva' if plural else 'la reserva'} por el nombre o la fecha de llegada.",
        f"2. Si ya {'aparecen' if plural else 'aparece'}, no hagas nada: llegó después de este aviso.",
        f"3. Si no {'aparecen' if plural else 'aparece'}, pulsa «Actualizar reservas» en el panel de RPV: importa todas las reservas de Beds24 al momento. Espera un minuto y vuelve a mirar.",
        f"4. Si sigue sin aparecer, {'créalas' if plural else 'créala'} a mano en RPV con los datos de arriba (si RPV te pide una referencia, usa el nº de Beds24{' de cada una' if plural else ''}).",
        "5. Si no sabes cómo hacerlo, o si te llegan varios avisos seguidos, avisa a Adrián: puede que se haya roto la conexión entre Beds24 y RPV.",
        "",
        "Este aviso no se repetirá para " + ("estas reservas" if plural else "esta reserva") +
        "; en cuanto " + ("aparezcan" if plural else "aparezca") + " en RPV te llegará otro mensaje para confirmarlo.",
    ]
    return "\n".join(lineas)


def llegadas_resueltas(avisados, resueltas, datos, ahora_utc=None):
    """Reservas de las que ya se avisó (están en `avisados`) y que ahora SÍ constan en RPV, sin confirmar todavía:
    lista de dicts {book_id, nombre_habitacion, huesped, canal, arrival, departure, horas}. `horas` son las
    transcurridas desde que se creó o modificó la reserva en Beds24 (None si no se sabe). Solo mira las que Beds24 sigue
    devolviendo en la ventana de RPV y cuya cuenta de RPV responde; una reserva cancelada o ya fuera de la ventana
    no se confirma."""
    ahora_utc = ahora_utc or datetime.now(timezone.utc)
    llegadas = []
    for e in datos["entradas"]:
        book_id = str(e.get("book_id"))
        if book_id not in avisados or book_id in resueltas:
            continue
        if e["room_id"] not in datos["cubiertas"] or (e["room_id"], e["arrival"]) not in datos["estados"]:
            continue
        llegadas.append({"book_id": book_id, "nombre_habitacion": e["nombre_habitacion"], "huesped": e.get("huesped", "?"),
                         "canal": e.get("canal", "Desconocido"), "arrival": e["arrival"], "departure": e.get("departure"),
                         "horas": _horas_sin_cambios(e, ahora_utc)})
    return sorted(llegadas, key=lambda c: (c["arrival"], c["nombre_habitacion"]))


def mensaje_llegada(llegadas):
    """Aviso corto de que una reserva de la que se avisó ya consta en RPV: no hay nada que hacer."""
    plural = len(llegadas) > 1
    lineas = [f"✅ ALCHOMES — {'Reservas que ya constan' if plural else 'Una reserva ya consta'} en RPV", "",
              f"{'Las reservas' if plural else 'La reserva'} de {'las' if plural else 'la'} que te avisamos antes ya "
              f"{'aparecen' if plural else 'aparece'} en RPV. No hace falta que hagas nada: todo en orden.", ""]
    for c in llegadas:
        salida = f" · sale el {_fecha_corta(c['departure'])}" if c.get("departure") else ""
        tardanza = f" · tardó como mucho {_antiguedad(c['horas'])} en llegar a RPV" if c.get("horas") is not None else ""
        lineas.append(f"• {c['nombre_habitacion']} · {c['huesped']} · {c['canal']} · llega el {_fecha_corta(c['arrival'])}{salida}{tardanza} · nº Beds24 {c['book_id']}")
    return "\n".join(lineas)


def avisar_reservas_sin_importar():
    """Avisa por WhatsApp de las reservas que RPV sigue sin conocer pasado el tiempo normal (una vez por reserva) y,
    más tarde, de que ya constan en RPV (también una vez), y anota la medición del retraso de RPV. Para llamarse desde
    /watchdog; no lanza excepción hacia arriba. Si falla un envío no se anota, y se reintenta en el próximo watchdog."""
    datos = leer_datos()
    if datos["entradas"]:
        rpv_latencia.registrar(datos["entradas"], datos["estados"], datos["cubiertas"])
    estado = _leer_estado()
    if estado is None:
        return
    avisados, resueltas = estado
    cambios = False

    nuevas = [c for c in candidatas_sin_importar(datos=datos) if c["book_id"] not in avisados]
    if nuevas:
        try:
            enviar_whatsapp_callmebot(mensaje_aviso(nuevas))
            avisados.update({c["book_id"]: c["arrival"] for c in nuevas})
            cambios = True
        except Exception as e:
            logger.error(f"[rpv_sin_importar] No se pudo enviar el aviso, se reintenta en el próximo watchdog: {e}")

    llegadas = llegadas_resueltas(avisados, resueltas, datos)
    if llegadas:
        try:
            enviar_whatsapp_callmebot(mensaje_llegada(llegadas))
            resueltas.update({c["book_id"]: c["arrival"] for c in llegadas})
            cambios = True
        except Exception as e:
            logger.error(f"[rpv_sin_importar] No se pudo enviar la confirmación, se reintenta en el próximo watchdog: {e}")

    if cambios:
        _guardar_estado(avisados, resueltas, hoy_madrid().isoformat())

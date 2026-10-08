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


def _leer_avisados():
    """{book_id (str): fecha de llegada ISO} de las reservas ya avisadas. None si
    Firestore no está disponible o falla la lectura (sin dedupe se repetiría el aviso cada 15 min)."""
    ref = _doc_ref()
    if ref is None:
        return None
    try:
        doc = ref.get()
        return {str(k): v for k, v in ((doc.to_dict() or {}).get("avisados") or {}).items()} if doc.exists else {}
    except Exception as e:
        logger.error(f"[rpv_sin_importar] Error leyendo estado en Firestore: {e}")
        return None


def _guardar_avisados(avisados, hoy_iso):
    ref = _doc_ref()
    try:
        ref.set({"avisados": {k: v for k, v in avisados.items() if v >= hoy_iso}})
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
        f"3. Si no {'aparecen' if plural else 'aparece'}, {'créalas' if plural else 'créala'} a mano en RPV con los datos de arriba (si RPV te pide una referencia, usa el nº de Beds24{' de cada una' if plural else ''}).",
        "4. Si no sabes cómo hacerlo, o si te llegan varios avisos seguidos, avisa a Adrián: puede que se haya roto la conexión entre Beds24 y RPV.",
        "",
        "Este aviso no se repetirá para " + ("estas reservas." if plural else "esta reserva."),
    ]
    return "\n".join(lineas)


def avisar_reservas_sin_importar():
    """Avisa por WhatsApp (un solo mensaje) de las reservas que RPV sigue sin conocer pasado el tiempo normal,
    una vez por reserva, y anota la medición del retraso de RPV. Para llamarse desde /watchdog; no lanza
    excepción hacia arriba."""
    datos = leer_datos()
    if datos["entradas"]:
        rpv_latencia.registrar(datos["entradas"], datos["estados"], datos["cubiertas"])
    avisados = _leer_avisados()
    if avisados is None:
        return
    nuevas = [c for c in candidatas_sin_importar(datos=datos) if c["book_id"] not in avisados]
    if not nuevas:
        return
    try:
        enviar_whatsapp_callmebot(mensaje_aviso(nuevas))
    except Exception as e:
        logger.error(f"[rpv_sin_importar] No se pudo enviar el aviso, se reintenta en el próximo watchdog: {e}")
        return
    avisados.update({c["book_id"]: c["arrival"] for c in nuevas})
    _guardar_avisados(avisados, hoy_madrid().isoformat())

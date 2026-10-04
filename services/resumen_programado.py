"""
services/resumen_programado.py — Envío automático de los dos resúmenes
diarios por WhatsApp, decidido desde /watchdog (cada 15 min):

  - a partir de las 08:00 (Madrid) → entradas y salidas de HOY
  - a partir de las 23:00 (Madrid) → entradas y salidas de MAÑANA

Antes los disparaba el Apps Script "Orquestador" con triggers horarios, que
se ejecutan en un minuto cualquiera dentro de la hora y no reintentan si
fallan. Aquí cada resumen sale en el primer watchdog a partir de su hora (como
mucho ~15 min después) y, si el envío falla, se reintenta en el siguiente.

Firestore system_state/resumenes_programados guarda la fecha (Madrid) del
último envío de cada uno: {"manana": "YYYY-MM-DD", "noche": "YYYY-MM-DD"}.
Sin Firestore no se envía nada: sin dedupe saldría un resumen cada 15 min (el
propio watchdog ya avisa si Firestore falla).

Tras enviarse, cada resumen marca sus entradas como anunciadas para el día
que cubre (services/checkins_ultima_hora.marcar_anunciados). Así el de las
23h deja preparado el día siguiente y los avisos de última hora funcionan
desde las 00:00.
"""
import logging
from datetime import time, timedelta

import config
from services.beds24 import get_beds24_access_token
from services.checkins_ultima_hora import marcar_anunciados
from services.fechas import ahora_madrid
from services.resumen import generar_mensaje_resumen
from services.whatsapp import enviar_whatsapp_callmebot

logger = logging.getLogger(__name__)

HORA_RESUMEN_MANANA = time(8, 0)
HORA_RESUMEN_NOCHE = time(23, 0)


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("resumenes_programados")


def _enviar_resumen(dia, hora_str):
    """Genera y envía el resumen de `dia`. Devuelve True solo si se envió."""
    try:
        get_beds24_access_token()
    except Exception as e:
        # Sin Beds24 el resumen saldría con "(ninguna)" en todo y se daría por
        # enviado. Se espera al siguiente watchdog, que ya avisa por su cuenta
        # del fallo de Beds24.
        logger.error(f"[resumen_programado] Beds24 no disponible, se reintenta en el próximo watchdog: {e}")
        return False
    try:
        mensaje, book_ids = generar_mensaje_resumen(hora_str, dia=dia)
        enviar_whatsapp_callmebot(mensaje)
    except Exception as e:
        logger.error(f"[resumen_programado] Error enviando el resumen de {dia}: {e}")
        return False
    marcar_anunciados(book_ids, dia=dia)
    return True


def enviar_resumenes_programados():
    """Envía el resumen que toque según la hora de Madrid, si no ha salido ya
    hoy. Pensada para llamarse desde /watchdog; no lanza excepción hacia
    arriba."""
    ref = _doc_ref()
    if ref is None:
        logger.error("[resumen_programado] Firestore no disponible — no se envían resúmenes automáticos")
        return
    try:
        doc = ref.get()
        estado = (doc.to_dict() or {}) if doc.exists else None
    except Exception as e:
        logger.error(f"[resumen_programado] Error leyendo estado en Firestore: {e}")
        return

    ahora = ahora_madrid()
    hoy = ahora.date()
    hoy_iso = hoy.isoformat()
    t = ahora.time()

    if estado is None:
        # Primera ejecución tras desplegar: no mandar de golpe un resumen cuya
        # hora ya pasó hoy (ese día ya lo envió el Apps Script).
        ref.set({
            "manana": hoy_iso if t >= HORA_RESUMEN_MANANA else "",
            "noche": hoy_iso if t >= HORA_RESUMEN_NOCHE else "",
        })
        return

    if t >= HORA_RESUMEN_NOCHE:
        clave, dia, hora_str = "noche", hoy + timedelta(days=1), "23"
    elif t >= HORA_RESUMEN_MANANA:
        clave, dia, hora_str = "manana", hoy, "08"
    else:
        return

    if estado.get(clave) == hoy_iso:
        return
    if _enviar_resumen(dia, hora_str):
        estado[clave] = hoy_iso
        try:
            ref.set(estado)
        except Exception as e:
            logger.error(f"[resumen_programado] Resumen enviado pero no se pudo guardar en Firestore (podría repetirse): {e}")

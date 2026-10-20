"""
services/avisos_registro.py — Registro de cómo se entregó cada aviso interno (WhatsApp por CallMeBot y respaldos).

Hasta ahora un aviso solo dejaba rastro en los registros de Render: no se podía saber a qué hora salió ni por qué
canal llegó (el 2026-10-18 CallMeBot falló y solo se supo por el Telegram de respaldo). Aquí se guarda, sin datos
personales, la primera línea de cada aviso, la hora y el resultado de cada canal, y se cuentan los fallos seguidos de
WhatsApp para avisar por los canales de respaldo cuando CallMeBot lleva varios avisos sin entregar.

Estado en Firestore: system_state/avisos_registro = {"envios": [...últimos MAX_ENVIOS...], "whatsapp_fallos_seguidos": n}.
Nada de lo de aquí puede romper el envío de un aviso: todo se traga y se registra en el log.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

MAX_ENVIOS = 60
FALLOS_PARA_AVISAR = 3   # avisos seguidos que CallMeBot no entrega antes de avisar por los respaldos


def _ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("avisos_registro")


def _recorte(texto, n=120):
    return " ".join(str(texto or "").split())[:n]


def registrar(mensaje, error_whatsapp, respaldos):
    """Anota un envío. `error_whatsapp` es None si CallMeBot lo entregó; `respaldos` es [(canal, error|None)].
    Devuelve los fallos seguidos de WhatsApp tras este envío (o None si no se pudo registrar)."""
    try:
        ref = _ref()
        if ref is None:
            return None
        doc = ref.get()
        datos = (doc.to_dict() or {}) if doc.exists else {}
        envios = list(datos.get("envios") or [])
        seguidos = 0 if error_whatsapp is None else int(datos.get("whatsapp_fallos_seguidos") or 0) + 1
        envios.append({
            "hora": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="seconds"),
            "asunto": _recorte((str(mensaje).strip().splitlines() or [""])[0], 100),
            "whatsapp": "ok" if error_whatsapp is None else _recorte(error_whatsapp),
            "respaldos": {canal: ("ok" if error is None else _recorte(error)) for canal, error in respaldos},
        })
        ref.set({"envios": envios[-MAX_ENVIOS:], "whatsapp_fallos_seguidos": seguidos})
        return seguidos
    except Exception as e:
        logger.error(f"[avisos_registro] No se pudo registrar el envío: {e}")
        return None


def leer():
    """{"envios": [...], "whatsapp_fallos_seguidos": n} o None si no hay Firestore."""
    try:
        ref = _ref()
        if ref is None:
            return None
        doc = ref.get()
        datos = (doc.to_dict() or {}) if doc.exists else {}
        return {"envios": list(datos.get("envios") or []), "whatsapp_fallos_seguidos": int(datos.get("whatsapp_fallos_seguidos") or 0)}
    except Exception as e:
        logger.error(f"[avisos_registro] No se pudo leer el registro: {e}")
        return None

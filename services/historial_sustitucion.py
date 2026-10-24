"""
services/historial_sustitucion.py — Registro de TODO lo que hace quien usa un perfil auditado (el sustituto de Ione, PIN 2589), con fecha y hora.

Lo escribe el SERVIDOR (no el navegador) cada vez que el sustituto hace algo: entrar, abrir el calendario, preguntar al asistente (pregunta y respuesta), consultar una tarea de la operativa,
crear, modificar, cancelar o bloquear reservas (con el ANTES y el DESPUÉS), preparar o enviar códigos de emergencia y cuando el asistente le manda escalar un asunto. El nombre sale de la sesión
firmada (lo que escribió al entrar y no puede cambiar), y no hay ninguna opción para desactivarlo desde su perfil. Los botones que pulsa y las reservas que mira en pantalla (que no generan petición)
los avisa la app, lo mejor que puede: son «de mejor esfuerzo».

Estado en Firestore: colección `historial_sustitucion`, un documento por evento: {ts, sustituto, perfil, tipo, resumen, detalle, caduca}. Se conservan RETENCION_MESES meses.
Lo lee solo el propietario (permiso `operativa.historial`). Nada de lo de aquí lanza excepción hacia arriba: un fallo al registrar nunca debe impedir lo que se estaba haciendo.
"""
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)

COLECCION = "historial_sustitucion"
RETENCION_MESES = 12
MAX_TEXTO = 1500
TIPOS = {
    "acceso": "🔑 Entró en la app", "calendario": "📅 Abrió el calendario", "consulta_asistente": "💬 Preguntó al asistente", "tarea_consultada": "📋 Consultó una tarea",
    "reserva_consultada": "👁 Miró una reserva", "reserva_creada": "➕ Creó una reserva", "reserva_modificada": "✏️ Modificó una reserva", "reserva_cancelada": "❌ Canceló una reserva",
    "bloqueo": "⛔ Bloqueó fechas", "emergencia": "🚨 Envío de emergencia", "escalado": "📞 Escalado", "boton": "🔘 Pulsó un botón",
}
TIPOS_DEL_CLIENTE = ("boton", "reserva_consultada")
_ESCALADO = re.compile(r"(dir[ií]gete a|cons[uú]ltalo con|remite a|rem[ií]telo a|avisa a|llama a|pregunta a)\s+(ione|adri[aá]n)", re.IGNORECASE)
_ultima_limpieza = {"t": 0.0}


def _col():
    if config.db is None:
        return None
    return config.db.collection(COLECCION)


def _recorte(valor, n=MAX_TEXTO):
    if isinstance(valor, str):
        return valor[:n]
    if isinstance(valor, dict):
        return {str(k)[:40]: _recorte(v, 300) for k, v in list(valor.items())[:20]}
    if isinstance(valor, (list, tuple)):
        return [_recorte(v, 300) for v in list(valor)[:20]]
    return valor


def registrar(perfil, nombre, tipo, resumen, detalle=None):
    """Anota un evento si el perfil es auditado. Devuelve True si se guardó. Nunca lanza."""
    try:
        if not perfil or not perfil.auditado or tipo not in TIPOS:
            return False
        col = _col()
        if col is None:
            logger.error("[historial] Sin Firestore: no se pudo registrar la actividad del sustituto")
            return False
        ahora = datetime.now(timezone.utc)
        doc_id = ahora.strftime("%Y%m%d%H%M%S%f") + "-" + uuid.uuid4().hex[:6]
        col.document(doc_id).set({"ts": ahora.isoformat(timespec="seconds"), "sustituto": str(nombre or "(sin nombre)")[:40], "perfil": perfil.id, "tipo": tipo,
                                  "resumen": _recorte(str(resumen or ""), 400), "detalle": _recorte(detalle or {}),
                                  "caduca": (ahora + timedelta(days=30 * RETENCION_MESES)).isoformat(timespec="seconds")})
        _limpiar_caducados(col, ahora)
        return True
    except Exception as e:
        logger.error(f"[historial] No se pudo registrar el evento {tipo}: {e}")
        return False


def _limpiar_caducados(col, ahora):
    if time.time() - _ultima_limpieza["t"] < 3600:
        return
    _ultima_limpieza["t"] = time.time()
    try:
        corte = ahora.isoformat(timespec="seconds")
        for doc in list(col.stream()):
            if (doc.to_dict() or {}).get("caduca", "9") < corte:
                col.document(doc.id).delete()
    except Exception as e:
        logger.error(f"[historial] No se pudieron borrar los eventos caducados: {e}")


def detectar_escalado(texto):
    """Si un texto del asistente manda al usuario a Ione o a Adrián, devuelve «Ione» o «Adrián»; si no, None."""
    m = _ESCALADO.search(str(texto or ""))
    return m.group(2).capitalize().replace("Adrian", "Adrián") if m else None


def listar(sustituto=None, dia=None, desde=None, hasta=None, tipo=None, limite=300):
    """Eventos, del más reciente al más antiguo. `dia` (AAAA-MM-DD, hora de Madrid) o `desde`/`hasta`. [] si no hay Firestore."""
    col = _col()
    if col is None:
        return []
    try:
        eventos = [dict(d.to_dict() or {}, id=d.id) for d in col.stream()]
    except Exception as e:
        logger.error(f"[historial] No se pudo leer el historial: {e}")
        return []
    if dia:
        desde = hasta = dia
    resultado = []
    for e in eventos:
        fecha = _fecha_madrid(e.get("ts"))
        if sustituto and e.get("sustituto", "").lower() != str(sustituto).lower():
            continue
        if tipo and e.get("tipo") != tipo:
            continue
        if desde and fecha < desde:
            continue
        if hasta and fecha > hasta:
            continue
        e["fecha"], e["hora"] = fecha, _hora_madrid(e.get("ts"))
        resultado.append(e)
    resultado.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return resultado[:limite]


def sustitutos():
    """Nombres con actividad registrada, con su último evento."""
    ultimos = {}
    for e in listar(limite=5000):
        ultimos.setdefault(e.get("sustituto"), e.get("ts"))
    return [{"nombre": n, "ultimo": t} for n, t in sorted(ultimos.items(), key=lambda x: x[1] or "", reverse=True)]


def _madrid(ts):
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(str(ts)).astimezone(ZoneInfo("Europe/Madrid"))
    except (ValueError, TypeError):
        return None


def _fecha_madrid(ts):
    d = _madrid(ts)
    return d.date().isoformat() if d else ""


def _hora_madrid(ts):
    d = _madrid(ts)
    return d.strftime("%H:%M") if d else ""

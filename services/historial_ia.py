"""
services/historial_ia.py — Asistente para preguntar sobre el Historial de sustitución («¿qué hizo ayer?», «¿qué reservas modificó esta semana?»).

Dos pasos, para gastar pocos tokens y que no pueda inventar: (1) el modelo traduce la pregunta a FILTROS (quién, desde cuándo, hasta cuándo, qué tipos de actividad); (2) el servidor
trae esos eventos del historial y el modelo responde SOLO con ellos. Lo usa únicamente el propietario (permiso `operativa.historial`).
"""
import json
import logging
import re
from datetime import timedelta

from services import entrevista
from services import historial_sustitucion as historial
from services.fechas import hoy_madrid

logger = logging.getLogger(__name__)

MAX_EVENTOS = 120
MAX_CHARS_CONTEXTO = 9000
_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _filtros(pregunta, nombres):
    hoy = hoy_madrid()
    sistema = (f"Traduces una pregunta sobre el historial de actividad de los sustitutos de la app de gestión a filtros. Hoy es {hoy.isoformat()} (hora de Madrid). "
               f"Sustitutos conocidos: {', '.join(nombres) or '(ninguno todavía)'}. Tipos de actividad: {', '.join(historial.TIPOS)}. "
               "Responde SOLO con un JSON: {\"sustituto\": nombre exacto o null, \"desde\": \"AAAA-MM-DD\" o null, \"hasta\": \"AAAA-MM-DD\" o null, \"tipos\": [] o lista de tipos}. "
               "«ayer» = el día anterior a hoy; «esta semana» = desde el lunes de esta semana hasta hoy; «hoy» = hoy. Si no se nombra a nadie, sustituto = null.")
    d = entrevista._llamar_modelo([{"role": "system", "content": sistema}, {"role": "user", "content": pregunta[:500]}])
    desde = d.get("desde") if _FECHA.match(str(d.get("desde") or "")) else None
    hasta = d.get("hasta") if _FECHA.match(str(d.get("hasta") or "")) else None
    quien = next((n for n in nombres if n.lower() == str(d.get("sustituto") or "").lower()), None)
    tipos = [t for t in (d.get("tipos") or []) if t in historial.TIPOS] if isinstance(d.get("tipos"), list) else []
    return quien, desde, hasta, tipos


def _linea(e):
    extra = ""
    det = e.get("detalle") or {}
    if e.get("tipo") == "consulta_asistente":
        extra = f" → respuesta: {str(det.get('respuesta') or '')[:300]}"
    elif e.get("tipo") in ("reserva_modificada", "reserva_cancelada", "reserva_creada"):
        extra = f" | antes={json.dumps(det.get('antes'), ensure_ascii=False)[:250]} | despues={json.dumps(det.get('despues'), ensure_ascii=False)[:250]}"
    return f"{e.get('fecha')} {e.get('hora')} · {e.get('sustituto')} · {e.get('tipo')} · {e.get('resumen')}{extra}"


def preguntar(pregunta):
    """Responde a una pregunta sobre el historial. Devuelve {respuesta, eventos}."""
    pregunta = " ".join(str(pregunta or "").split())[:500]
    if len(pregunta) < 4:
        raise entrevista.ErrorEntrevista("Escribe la pregunta")
    nombres = [s["nombre"] for s in historial.sustitutos()]
    quien, desde, hasta, tipos = _filtros(pregunta, nombres)
    if not desde and not hasta:
        desde = (hoy_madrid() - timedelta(days=7)).isoformat()   # sin periodo claro: la última semana
    eventos = []
    for t in (tipos or [None]):
        eventos += historial.listar(sustituto=quien, desde=desde, hasta=hasta, tipo=t, limite=MAX_EVENTOS)
    eventos = sorted({e["id"]: e for e in eventos}.values(), key=lambda e: e.get("ts", ""), reverse=True)[:MAX_EVENTOS]
    if not eventos:
        quien_txt = f" de {quien}" if quien else ""
        return {"respuesta": f"No hay actividad registrada{quien_txt} entre {desde or 'el principio'} y {hasta or 'hoy'}.", "eventos": 0}
    contexto = "\n".join(_linea(e) for e in reversed(eventos))[:MAX_CHARS_CONTEXTO]
    sistema = ("Eres el asistente del historial de actividad de los sustitutos. Responde en español, claro y breve, SOLO con los registros que te doy: no inventes nada. "
               "Cuando cites algo, di el día y la hora. Si los registros no bastan para contestar, dilo. "
               "Responde con un JSON: {\"respuesta\": \"...\"}.\n\nREGISTROS (de más antiguo a más reciente):\n" + contexto)
    d = entrevista._llamar_modelo([{"role": "system", "content": sistema}, {"role": "user", "content": pregunta}])
    texto = " ".join(str(d.get("respuesta") or "").split())
    if not texto:
        raise entrevista.ErrorEntrevista("No he podido responder. Inténtalo de nuevo.", 502)
    return {"respuesta": texto[:3000], "eventos": len(eventos)}

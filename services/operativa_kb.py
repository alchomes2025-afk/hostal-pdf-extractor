"""
services/operativa_kb.py — Base de conocimiento de la operativa de gestión (lo que sabe Ione sobre el puesto de gestora del hostal).

La consulta el asistente (herramienta `buscar_operativa`), la pestaña «Operativa de Gestión» (tareas diarias) y el Entrenamiento que la va llenando. En la fase 4 solo
se LEE (buscar y listar); el alta, la edición y la revisión llegan en las fases 5 y 6.

Cada entrada es un documento de Firestore, colección `kb_operativa`:
  tipo        «tarea» (tarea diaria, con pasos), «faq» (pregunta frecuente de huéspedes), «telefono» (contactos), «escalado» (qué hacer y a quién avisar en un tipo de
              problema), «proceso» (otros procedimientos: llamadas, vending, plataformas…)
  titulo      nombre corto
  contenido   el texto completo (pasos, qué comprobar, a quién avisar, datos a dar…)
  etiquetas   palabras clave para buscar
  orden       para ordenar las tareas del día
  estado      «borrador» (recién aprendido, pendiente de revisar) o «aprobado» (el asistente SOLO usa las aprobadas)
  actualizado, editado_por

Sin Firestore, o sin entradas aprobadas, la búsqueda devuelve vacío y el asistente dice que eso todavía no está recogido (no inventa procedimientos).
Nada de lo de aquí lanza excepción hacia arriba.
"""
import logging
import time
import unicodedata

import config

logger = logging.getLogger(__name__)

COLECCION = "kb_operativa"
TIPOS = ("tarea", "faq", "telefono", "escalado", "proceso")
MAX_RESULTADOS = 3
MAX_CHARS_ENTRADA = 1500      # lo que se le da al modelo de cada entrada (cuida los tokens por minuto de Groq)
_CACHE_SEG = 60
_cache = {"hasta": 0.0, "entradas": []}


def _norm(texto):
    sin_tildes = unicodedata.normalize("NFD", str(texto or "")).encode("ascii", "ignore").decode()
    return sin_tildes.lower()


def _palabras(texto):
    """Palabras de 3 o más letras, sin tildes, y sin las más comunes."""
    ruido = {"que", "los", "las", "una", "uno", "unos", "unas", "del", "con", "por", "para", "como", "cual", "cuando", "donde", "hay", "hacer", "tengo", "debo", "puedo"}
    limpio = "".join(c if c.isalnum() else " " for c in _norm(texto))
    return [p for p in limpio.split() if len(p) >= 3 and p not in ruido]


def entradas(estado="aprobado", forzar=False):
    """Las entradas con ese estado (None = todas), ordenadas por `orden` y título. [] si no hay Firestore."""
    if forzar or time.time() >= _cache["hasta"]:
        if config.db is None:
            return []
        try:
            todas = []
            for doc in config.db.collection(COLECCION).stream():
                d = doc.to_dict() or {}
                d["id"] = doc.id
                todas.append(d)
            _cache.update(hasta=time.time() + _CACHE_SEG, entradas=todas)
        except Exception as e:
            logger.error(f"[operativa_kb] No se pudo leer la base de conocimiento: {e}")
            return []
    resultado = [d for d in _cache["entradas"] if estado is None or d.get("estado") == estado]
    return sorted(resultado, key=lambda d: (d.get("orden") if isinstance(d.get("orden"), (int, float)) else 9999, _norm(d.get("titulo"))))


def olvidar_cache():
    _cache["hasta"] = 0.0


def _puntos(entrada, palabras):
    titulo, etiquetas, contenido = _norm(entrada.get("titulo")), _norm(" ".join(entrada.get("etiquetas") or [])), _norm(entrada.get("contenido"))
    puntos = 0
    for p in palabras:
        puntos += 3 * (p in titulo) + 2 * (p in etiquetas) + (p in contenido)
    return puntos


def buscar(consulta, tipo=None, limite=MAX_RESULTADOS):
    """Las entradas APROBADAS que mejor responden a la consulta: [{tipo, titulo, contenido}], de más a menos relevante. Vacío si nada coincide."""
    palabras = _palabras(consulta)
    if not palabras:
        return []
    candidatas = [e for e in entradas("aprobado") if tipo is None or e.get("tipo") == tipo]
    puntuadas = sorted(((_puntos(e, palabras), e) for e in candidatas), key=lambda x: -x[0])
    return [{"tipo": e.get("tipo"), "titulo": e.get("titulo"), "contenido": str(e.get("contenido") or "")[:MAX_CHARS_ENTRADA]}
            for puntos, e in puntuadas[:limite] if puntos > 0]


def temas():
    """Títulos de lo aprobado, para que el asistente sepa qué hay si la búsqueda no encuentra nada."""
    return [{"tipo": e.get("tipo"), "titulo": e.get("titulo")} for e in entradas("aprobado")][:60]

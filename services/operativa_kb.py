"""
services/operativa_kb.py — Base de conocimiento de la operativa de gestión (lo que sabe Ione sobre el puesto de gestora del hostal).

La consulta el asistente (herramienta `buscar_operativa`), la pestaña «Operativa de Gestión» (tareas diarias) y el Entrenamiento que la va llenando. En la fase 4 solo
se LEE (buscar y listar); el alta, la edición y la revisión llegan en las fases 5 y 6.

Cada entrada es un documento de Firestore, colección `kb_operativa`:
  tipo        «tarea» (tarea diaria, con pasos), «faq» (pregunta frecuente de huéspedes), «telefono» (contactos), «escalado» (qué hacer y a quién avisar en un tipo de
              problema), «proceso» (otros procedimientos: llamadas, vending, plataformas…)
  titulo      nombre corto
  contenido   el texto completo (pasos, qué comprobar, a quién avisar, datos a dar…)
  propiedad   «hostal», «primavera» o vacío (vale para todo). Lo de la Casa de la Primavera solo lo ven (y lo usa el asistente) quien tenga acceso a esa propiedad.
  etiquetas   palabras clave para buscar
  orden       para ordenar las tareas del día
  estado      «borrador» (recién aprendido, pendiente de revisar) o «aprobado» (el asistente SOLO usa las aprobadas)
  actualizado, editado_por

Sin Firestore, o sin entradas aprobadas, la búsqueda devuelve vacío y el asistente dice que eso todavía no está recogido (no inventa procedimientos).
Nada de lo de aquí lanza excepción hacia arriba.
"""
import logging
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone

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


PROPIEDADES = ("", "hostal", "primavera")


def entradas(estado="aprobado", forzar=False, primavera=True):
    """Las entradas con ese estado (None = todas), ordenadas por `orden` y título. [] si no hay Firestore. Sin `primavera`, sin las de La Casa de la Primavera."""
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
    resultado = [d for d in _cache["entradas"] if (estado is None or d.get("estado") == estado) and (primavera or d.get("propiedad") != "primavera")]
    return sorted(resultado, key=lambda d: (d.get("orden") if isinstance(d.get("orden"), (int, float)) else 9999, _norm(d.get("titulo"))))


def olvidar_cache():
    _cache["hasta"] = 0.0


def _puntos(entrada, palabras):
    titulo, etiquetas, contenido = _norm(entrada.get("titulo")), _norm(" ".join(entrada.get("etiquetas") or [])), _norm(entrada.get("contenido"))
    puntos = 0
    for p in palabras:
        puntos += 3 * (p in titulo) + 2 * (p in etiquetas) + (p in contenido)
    return puntos


def buscar(consulta, tipo=None, limite=MAX_RESULTADOS, primavera=True):
    """Las entradas APROBADAS que mejor responden a la consulta: [{tipo, titulo, contenido}], de más a menos relevante. Vacío si nada coincide."""
    palabras = _palabras(consulta)
    if not palabras:
        return []
    candidatas = [e for e in entradas("aprobado", primavera=primavera) if tipo is None or e.get("tipo") == tipo]
    puntuadas = sorted(((_puntos(e, palabras), e) for e in candidatas), key=lambda x: -x[0])
    return [{"tipo": e.get("tipo"), "titulo": e.get("titulo"), "contenido": str(e.get("contenido") or "")[:MAX_CHARS_ENTRADA]}
            for puntos, e in puntuadas[:limite] if puntos > 0]


def temas(primavera=True):
    """Títulos de lo aprobado, para que el asistente sepa qué hay si la búsqueda no encuentra nada."""
    return [{"tipo": e.get("tipo"), "titulo": e.get("titulo")} for e in entradas("aprobado", primavera=primavera)][:60]


# ── Edición (solo el propietario, fase 5) ─────────────────────────────────────

class ErrorKB(Exception):
    def __init__(self, mensaje, status=400):
        super().__init__(mensaje)
        self.status = status


ESTADOS = ("borrador", "aprobado")


def validar(datos):
    """Limpia y valida una entrada que llega del editor. Lanza ErrorKB con un mensaje legible."""
    tipo = str(datos.get("tipo") or "")
    if tipo not in TIPOS:
        raise ErrorKB("Elige el tipo de entrada")
    titulo = " ".join(str(datos.get("titulo") or "").split())
    if not 2 <= len(titulo) <= 120:
        raise ErrorKB("El título debe tener entre 2 y 120 caracteres")
    contenido = str(datos.get("contenido") or "").strip()
    if not contenido:
        raise ErrorKB("Escribe el contenido")
    if len(contenido) > 8000:
        raise ErrorKB("El contenido es demasiado largo (máximo 8000 caracteres)")
    etiquetas = datos.get("etiquetas") or []
    if isinstance(etiquetas, str):
        etiquetas = [x.strip() for x in etiquetas.replace(";", ",").split(",")]
    etiquetas = [" ".join(str(x).split())[:40] for x in etiquetas if str(x).strip()][:15]
    estado = datos.get("estado") if datos.get("estado") in ESTADOS else "borrador"
    try:
        orden = int(datos.get("orden")) if datos.get("orden") not in (None, "") else 1000
    except (TypeError, ValueError):
        raise ErrorKB("El orden debe ser un número")
    propiedad = datos.get("propiedad") if datos.get("propiedad") in PROPIEDADES else ""
    return {"tipo": tipo, "titulo": titulo, "contenido": contenido, "etiquetas": etiquetas, "estado": estado, "orden": orden, "propiedad": propiedad}


def _doc_id(valor):
    valor = str(valor or "")
    return valor if re.fullmatch(r"[A-Za-z0-9_-]{4,40}", valor) else ""


def guardar(datos, usuario=""):
    """Crea o actualiza una entrada. Devuelve su id. Un borrador pasa a aprobado solo si quien lo guarda lo marca (el propietario)."""
    if config.db is None:
        raise ErrorKB("La base de conocimiento no está disponible ahora mismo", 503)
    limpio = validar(datos)
    doc_id = _doc_id(datos.get("id")) or uuid.uuid4().hex[:12]
    limpio.update(actualizado=datetime.now(timezone.utc).isoformat(timespec="seconds"), editado_por=str(usuario or "")[:40])
    try:
        config.db.collection(COLECCION).document(doc_id).set(limpio)
    except Exception as e:
        logger.error(f"[operativa_kb] No se pudo guardar la entrada {doc_id}: {e}")
        raise ErrorKB("No se ha podido guardar. Inténtalo de nuevo.", 502)
    olvidar_cache()
    return doc_id


def borrar(doc_id):
    if config.db is None:
        raise ErrorKB("La base de conocimiento no está disponible ahora mismo", 503)
    doc_id = _doc_id(doc_id)
    if not doc_id:
        raise ErrorKB("Entrada desconocida", 404)
    try:
        config.db.collection(COLECCION).document(doc_id).delete()
    except Exception as e:
        logger.error(f"[operativa_kb] No se pudo borrar la entrada {doc_id}: {e}")
        raise ErrorKB("No se ha podido borrar. Inténtalo de nuevo.", 502)
    olvidar_cache()


def una(doc_id, solo_aprobada=True, primavera=True):
    """La entrada completa (con su id) o None."""
    doc_id = _doc_id(doc_id)
    for e in entradas(None, forzar=True, primavera=primavera):
        if e["id"] == doc_id and (not solo_aprobada or e.get("estado") == "aprobado"):
            return e
    return None


def indice(primavera=True):
    """Lo aprobado, agrupado por tipo, solo con id y título: es lo que enseña la pestaña Operativa de Gestión."""
    grupos = {t: [] for t in TIPOS}
    for e in entradas("aprobado", primavera=primavera):
        if e.get("tipo") in grupos:
            grupos[e["tipo"]].append({"id": e["id"], "titulo": e.get("titulo")})
    return grupos


def importar_faq(usuario=""):
    """Carga como BORRADORES las preguntas frecuentes que ya tiene la web de check-in (datos/faq_checkin.json, una entrada por sección de sus documentos de referencia)
    para que se revisen y se aprueben. No pisa nada: una entrada que ya existe (aunque se haya editado) se deja como está. Devuelve {creadas, existentes}."""
    import json
    import os
    if config.db is None:
        raise ErrorKB("La base de conocimiento no está disponible ahora mismo", 503)
    ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datos", "faq_checkin.json")
    with open(ruta, encoding="utf-8") as f:
        origen = json.load(f)
    existentes = {e["id"] for e in entradas(None, forzar=True)}
    creadas = 0
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in origen:
        if item["id"] in existentes:
            continue
        try:
            config.db.collection(COLECCION).document(item["id"]).set({
                "tipo": "faq", "titulo": item["titulo"], "contenido": item["contenido"], "etiquetas": item.get("etiquetas", []), "estado": "borrador",
                "orden": 1000 + item.get("orden", 0), "propiedad": item["propiedad"], "actualizado": ahora, "editado_por": str(usuario or "")[:40], "origen": "web de check-in"})
            creadas += 1
        except Exception as e:
            logger.error(f"[operativa_kb] No se pudo importar {item['id']}: {e}")
            raise ErrorKB("No se han podido importar todas. Inténtalo de nuevo.", 502)
    olvidar_cache()
    return {"creadas": creadas, "existentes": len(origen) - creadas}

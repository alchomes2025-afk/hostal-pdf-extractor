"""
services/guest_match.py — Encuentra qué reserva candidata corresponde al
nombre que escribió el huésped en la web de check-in.

Dos niveles, en orden (para no llamar a Groq en cada búsqueda):
  1. Coincidencia determinista: normaliza mayúsculas/acentos/orden de
     palabras. Gratis e instantáneo — cubre la inmensa mayoría de los casos.
  2. Si eso da cero o varias coincidencias, se le pasa a Groq la lista corta
     de candidatos (ya acotada por fecha en services/beds24.py, nunca toda
     la base de datos) para que tolere erratas de escritura.

No tiene conocimiento de Beds24 — recibe una lista de (item, nombre) ya
resuelta y devuelve cuál de esos items es, sea lo que sea el item.
"""
import json
import logging
import re
import unicodedata
import requests

from config import GROQ_API_KEY, GROQ_API_URL, GROQ_MODEL_PRI, GROQ_MODEL_FALL

logger = logging.getLogger(__name__)


def normalizar_nombre(texto):
    """Minúsculas, sin acentos, solo letras — como conjunto de palabras (no
    cadena), para que "Juan Perez" y "Perez Juan" comparen igual."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^a-zA-Z ]", " ", t).lower()
    return set(w for w in t.split() if w)


def _match_exacto(query_tokens, candidatos_tokens):
    """Índices cuyo conjunto de palabras contiene o está contenido en el de
    la consulta — tolera que falte o sobre un nombre/apellido, no erratas."""
    return [
        i for i, cand in enumerate(candidatos_tokens)
        if cand and (query_tokens <= cand or cand <= query_tokens)
    ]


def _desambiguar_con_groq(nombre_query, nombres_candidatos):
    """
    Le pide a Groq que decida, entre una lista corta de nombres candidatos
    (ya acotada por fecha), cuál corresponde al nombre escrito por el
    huésped — tolerando erratas de escritura, que la comparación
    determinista no cubre.

    Devuelve el índice (int) del candidato elegido, o None si Groq no está
    configurado, falla, o no encuentra ninguno con confianza suficiente.
    """
    if not GROQ_API_KEY:
        logger.warning(f"[guest_match] GROQ_API_KEY no configurada — no se puede desambiguar '{nombre_query}'")
        return None
    if not nombres_candidatos:
        return None

    lista = "\n".join(f"{i}: {n}" for i, n in enumerate(nombres_candidatos))
    prompt = (
        f'Un huésped ha escrito su nombre como: "{nombre_query}"\n\n'
        f"Estos son los nombres de huéspedes con reserva activa en la fecha relevante:\n{lista}\n\n"
        "¿Cuál de estos nombres es la MISMA PERSONA que escribió el huésped, "
        "considerando posibles erratas de escritura, acentos, orden de nombre/"
        "apellido, o nombre incompleto? Responde SOLO con un JSON de una línea: "
        '{"indice": N} con el número de la lista, o {"indice": null} si no hay '
        "ninguna coincidencia razonablemente segura. No expliques nada más, no uses markdown."
    )

    def _llamar(model):
        return requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 600, "temperature": 0},
            timeout=15,
        )

    try:
        resp = _llamar(GROQ_MODEL_PRI)
        if resp.status_code in (429, 503):
            logger.warning(f"[guest_match] {GROQ_MODEL_PRI} → {resp.status_code}, probando fallback")
            resp = _llamar(GROQ_MODEL_FALL)
        resp.raise_for_status()
        contenido_crudo = resp.json()["choices"][0]["message"]["content"].strip()
        contenido = re.sub(r"^```(json)?|```$", "", contenido_crudo, flags=re.M).strip()
        data = json.loads(contenido)
        idx = data.get("indice")
        logger.info(f"[guest_match] Groq respuesta cruda para '{nombre_query}': {contenido_crudo!r}")
        if isinstance(idx, int) and 0 <= idx < len(nombres_candidatos):
            logger.info(f"[guest_match] Groq desambiguó '{nombre_query}' → candidato {idx} ('{nombres_candidatos[idx]}')")
            return idx
        logger.info(f"[guest_match] Groq no encontró coincidencia segura para '{nombre_query}' (índice={idx!r})")
        return None
    except Exception as e:
        logger.warning(f"[guest_match] Groq FALLÓ desambiguando '{nombre_query}': {type(e).__name__}: {e}")
        return None


def emparejar_nombre(nombre_query, candidatos_con_nombre):
    """
    candidatos_con_nombre: lista de (item, nombre_extraido) — item puede ser
    cualquier cosa (en services/beds24.py, un booking de Beds24), ya filtrada
    a un conjunto pequeño y relevante (p. ej. por fecha) antes de llamar aquí.

    Devuelve (item_o_None, ambiguo_bool):
      - Una única coincidencia determinista → se devuelve directo, sin Groq.
      - Cero o varias → se intenta desambiguar con Groq entre esos mismos
        candidatos (nunca busca fuera de la lista recibida).
      - Si Groq tampoco resuelve con confianza → (None, True) si había
        candidatos con los que comparar, o (None, False) si la lista venía
        vacía (no hay nada que desambiguar, es simplemente "no encontrado").
    """
    query_tokens = normalizar_nombre(nombre_query)
    if not query_tokens or not candidatos_con_nombre:
        return None, False

    nombres = [n for _, n in candidatos_con_nombre]
    logger.info(f"[guest_match] query='{nombre_query}' — candidatos disponibles: {nombres}")
    candidatos_tokens = [normalizar_nombre(n) for n in nombres]

    exactos = _match_exacto(query_tokens, candidatos_tokens)
    if len(exactos) == 1:
        return candidatos_con_nombre[exactos[0]][0], False
    if len(exactos) > 1:
        logger.info(f"[guest_match] query='{nombre_query}' — match exacto ambiguo entre: {[nombres[i] for i in exactos]}")

    idx = _desambiguar_con_groq(nombre_query, nombres)
    if idx is not None:
        return candidatos_con_nombre[idx][0], False

    return None, True

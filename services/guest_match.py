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
from difflib import SequenceMatcher

import requests

from config import GROQ_API_KEY, GROQ_API_URL, GROQ_MODEL_PRI, GROQ_MODEL_FALL

logger = logging.getLogger(__name__)


def normalizar_nombre(texto):
    """Minúsculas, sin acentos, solo letras — como conjunto de palabras (no
    cadena), para que "Juan Perez" y "Perez Juan" comparen igual."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^a-zA-Z ]", " ", t).lower()
    return set(w for w in t.split() if w)


PALABRAS_MINIMAS = 2   # palabras que deben coincidir (o el nombre entero, si la reserva solo tiene una)
PALABRAS_DE_MAS = 1    # palabras de la consulta que pueden no estar en la reserva (p. ej. un segundo apellido)


def _coincide(query_tokens, cand):
    """Revisión de seguridad 2026-10-18: antes bastaba que la consulta estuviera CONTENIDA en el nombre (o al revés), así
    que escribir solo «Yuka» abría la reserva de la única Yuka alojada y mostraba su código de puerta. Ahora hacen falta
    al menos DOS palabras en común (o el nombre entero si la reserva solo tiene una) y como mucho UNA palabra de más en
    la consulta. Tolera orden, acentos y un apellido de más o de menos; no erratas."""
    if not query_tokens or not cand:
        return False
    return len(query_tokens & cand) >= min(PALABRAS_MINIMAS, len(cand)) and len(query_tokens - cand) <= PALABRAS_DE_MAS


def _match_exacto(query_tokens, candidatos_tokens):
    """Índices de los candidatos que `_coincide` con la consulta."""
    return [i for i, cand in enumerate(candidatos_tokens) if _coincide(query_tokens, cand)]


def _parecidas(a, b):
    return a == b or (min(len(a), len(b)) >= 3 and SequenceMatcher(None, a, b).ratio() >= 0.75)


def _compatible(consulta, candidato):
    """Guarda sobre la elección de Groq: lo escrito debe PARECERSE al nombre elegido aunque tenga erratas. Si no, se
    descarta (el modelo puede equivocarse o ser engañado con el texto que escribe el huésped). Mismas reglas que
    `_coincide`, comparando palabras parecidas en vez de iguales."""
    q, c = normalizar_nombre(consulta), normalizar_nombre(candidato)
    if not q or not c:
        return False
    usadas, sin_pareja = set(), 0
    for palabra in q:
        pareja = next((w for w in c if w not in usadas and _parecidas(palabra, w)), None)
        if pareja is None:
            sin_pareja += 1
        else:
            usadas.add(pareja)
    return len(usadas) >= min(PALABRAS_MINIMAS, len(c)) and sin_pareja <= PALABRAS_DE_MAS


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
        # Si solo UNO tiene exactamente las mismas palabras (los otros solo contienen o están contenidos), es ese.
        identicos = [i for i in exactos if candidatos_tokens[i] == query_tokens]
        if len(identicos) == 1:
            return candidatos_con_nombre[identicos[0]][0], False
        # Varias coincidencias igual de buenas: Groq no puede distinguirlas (el nombre es el mismo) y elegir al azar podría
        # dar la habitación y el código de OTRA persona con el mismo nombre. Se declara ambiguo y el huésped lo aclara.
        return None, True

    idx = _desambiguar_con_groq(nombre_query, nombres)
    if idx is not None and _compatible(nombre_query, nombres[idx]):
        return candidatos_con_nombre[idx][0], False
    if idx is not None:
        logger.warning(f"[guest_match] Groq eligió '{nombres[idx]}' para '{nombre_query}', que no se parece: se descarta")

    return None, True

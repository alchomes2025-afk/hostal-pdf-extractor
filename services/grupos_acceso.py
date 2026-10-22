"""
services/grupos_acceso.py — Cómo reparte los códigos una reserva de VARIAS habitaciones (web de check-in).

Una reserva de varias habitaciones llega a Beds24 como una reserva por habitación (la «maestra» y sus hijas con `masterId`). Hasta ahora quien
abría la web con el nombre de la reserva veía los códigos de TODAS las habitaciones. Eso vale para una familia, pero no para una empresa con
tres adultos que quieren su intimidad. Ahora quien reserva elige UNA vez:

  · «juntos»    → ve todos los códigos (como siempre).
  · «separados» → no ve ningún código: ve un enlace por habitación para repartir. Cada enlace abre SOLO su habitación (token firmado de
                  habitación, services/acceso_enlace.py) y ya no enseña el resto del grupo.

Mientras no haya elegido, la web pregunta y el servidor NO entrega ningún código del grupo. No se pregunta si alguna habitación ya está
alojada (`staying`): esa gente ya ha visto sus códigos, se deja como «juntos» sin molestarla.

La elección se guarda en Firestore (`checkin_grupos/<id de la reserva maestra>`: {modo, elegido}). Sin Firestore o sin la clave de los enlaces
personales (CHECKIN_LINK_SECRET) la función se desactiva y todo va como antes. Nunca lanza excepción hacia arriba.
"""
import logging
from datetime import datetime, timezone

import config
from services.acceso_enlace import crear_token_habitacion, habilitado

logger = logging.getLogger(__name__)

MODOS = ("juntos", "separados")
COLECCION = "checkin_grupos"


def _ref(master_id):
    if config.db is None or not str(master_id or "").isdigit():
        return None
    return config.db.collection(COLECCION).document(str(master_id))


def disponible():
    return habilitado() and config.db is not None


def obtener(master_id):
    """«juntos», «separados» o None (no ha elegido, o no se puede saber)."""
    try:
        ref = _ref(master_id)
        if ref is None:
            return None
        doc = ref.get()
        modo = (doc.to_dict() or {}).get("modo") if doc.exists else None
        return modo if modo in MODOS else None
    except Exception as e:
        logger.error(f"[grupos_acceso] No se pudo leer la elección del grupo {master_id}: {e}")
        return None


def guardar(master_id, modo):
    """True si se guardó."""
    if modo not in MODOS:
        return False
    try:
        ref = _ref(master_id)
        if ref is None:
            return False
        ref.set({"modo": modo, "elegido": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return True
    except Exception as e:
        logger.error(f"[grupos_acceso] No se pudo guardar la elección del grupo {master_id}: {e}")
        return False


def _sin_codigo(d):
    d["pin"] = None
    d["pin_available"] = False


def aplicar_eleccion(respuesta, master_id):
    """Ajusta la respuesta de /check-in de un GRUPO (respuesta["habitaciones"] con más de una) según lo elegido. Devuelve el modo que se ha
    aplicado: «pendiente» (hay que preguntar), «juntos» o «separados»; o None si la función está desactivada (la respuesta no cambia)."""
    habitaciones = respuesta.get("habitaciones") or []
    if len(habitaciones) < 2 or not disponible():
        return None
    modo = obtener(master_id)
    alojados = any(h.get("estado") == "staying" for h in habitaciones)
    if modo is None and alojados:
        modo = "juntos"     # ya tienen sus códigos a la vista: no se les pregunta
    if modo is None:
        modo = "pendiente"
    respuesta["eleccion_grupo"] = modo
    respuesta["puede_repartir"] = modo in ("juntos", "separados")
    if modo in ("pendiente", "separados"):
        _sin_codigo(respuesta)
        for h in habitaciones:
            _sin_codigo(h)
    if modo == "separados":
        for h in habitaciones:
            h["acceso_hab"] = crear_token_habitacion(h.get("book_id"))
    return modo

"""
services/rpv_latencia.py — Cuánto tarda RPV en recibir una reserva de Beds24, medido de verdad.

RPV importa las reservas con retraso (integración nativa "b24", frecuencia por confirmar). Para saber qué
decirle a un huésped que llama porque no puede hacer el parte («pruebe de nuevo dentro de X») y cuándo dar
una reserva por anómala, se mide: en cada pasada del watchdog (cada 15 min) se anota la hora a la que cada
reserva nueva aparece por primera vez en RPV frente a la hora a la que se creó en Beds24.

Cada medida es un intervalo [minimo, maximo] minutos (el retraso real está entre la última pasada en la que
aún no estaba y la primera en la que ya estaba; precisión ≈ 15-25 min). Solo cuentan como «importadas» las
que aparecen en RPV en estado «pendiente» (sin datos de huéspedes): si ya aparecen con el parte completado
no se sabe si las importó RPV o las creó el propio huésped. Las que pasan 72 h sin aparecer se anotan como
«nunca vistas».

Estado en Firestore (system_state/rpv_latencia): {"pendientes": {book_id: {...}}, "medidas": [...]},
sin datos personales (ni nombres ni emails), con un máximo de MAX_MEDIDAS medidas.
"""
import logging
import math
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)

SEGUIMIENTO_MIN = 72 * 60          # una reserva que no aparece en RPV en 72 h se da por «nunca vista»
VISTA_DIRECTA_MAX_MIN = 6 * 60     # si ya está en RPV la primera vez que la vemos, solo informa si es reciente
PASADAS_AUSENTE_PARA_OLVIDAR = 3   # una reserva que desaparece de Beds24 (cancelada, movida) 3 pasadas seguidas deja de seguirse
MAX_MEDIDAS = 300
MUESTRAS_FIABLES = 10              # con menos medidas la estadística es solo orientativa
ESPERA_TIPICA_PROVISIONAL_MIN = 120  # mientras no haya medidas fiables; el único dato conocido es «≤ 2 h 15 min» (2026-10-08)


def _doc_ref():
    if config.db is None:
        return None
    return config.db.collection("system_state").document("rpv_latencia")


def _leer():
    """{"pendientes": {...}, "medidas": [...]} o None si Firestore no está o falla."""
    ref = _doc_ref()
    if ref is None:
        return None
    try:
        doc = ref.get()
        datos = (doc.to_dict() or {}) if doc.exists else {}
        return {"pendientes": dict(datos.get("pendientes") or {}), "medidas": list(datos.get("medidas") or [])}
    except Exception as e:
        logger.error(f"[rpv_latencia] Error leyendo Firestore: {e}")
        return None


def edad_min(iso, ahora_utc):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        minutos = int((ahora_utc - dt).total_seconds() // 60)
        return minutos if minutos >= 0 else None
    except Exception:
        return None


def registrar(entradas, estados, cubiertas, ahora_utc=None):
    """
    Actualiza las medidas con las llegadas que ve esta pasada. `entradas` son las reservas de Beds24
    (services.beds24.obtener_bookings_rango_beds24), `estados` lo que conoce RPV y `cubiertas` las
    habitaciones cuya cuenta de RPV ha respondido. Devuelve cuántas medidas nuevas ha anotado.
    Nunca lanza excepción hacia arriba.
    """
    try:
        return _registrar(entradas, estados, cubiertas, ahora_utc or datetime.now(timezone.utc))
    except Exception as e:
        logger.error(f"[rpv_latencia] Error midiendo el retraso: {e}")
        return 0


def _registrar(entradas, estados, cubiertas, ahora_utc):
    if not entradas:   # Beds24 sin datos (o caído): no se toca nada, para no dar por desaparecidas las pendientes
        return 0
    datos = _leer()
    if datos is None:
        return 0
    pendientes, medidas = datos["pendientes"], datos["medidas"]
    medidos = {m.get("id") for m in medidas}
    nuevas, cambios = 0, False

    def anotar(book_id, info, minimo, maximo, estado, importada, nunca_vista=False):
        nonlocal nuevas
        medidas.append({"id": book_id, "habitacion": info["habitacion"], "canal": info["canal"], "creada": info["creada"],
                        "visto": ahora_utc.isoformat(), "minimo_min": minimo, "maximo_min": maximo, "estado_al_verla": estado,
                        "importada": importada, "nunca_vista": nunca_vista})
        medidos.add(book_id)
        nuevas += 1

    vistas = set()
    for e in entradas:
        room, book_id = e["room_id"], str(e.get("book_id"))
        vistas.add(book_id)   # Beds24 la devuelve: no está «desaparecida», aunque la cuenta de RPV no responda ahora
        if room not in cubiertas or str(e.get("status") or "").lower() == "black" or book_id in medidos:
            continue
        edad = edad_min(e.get("creada"), ahora_utc)
        if edad is None:
            continue
        en_rpv = estados.get((room, e["arrival"]))
        if book_id in pendientes:
            p = pendientes[book_id]
            if en_rpv:
                anotar(book_id, p, p["ultima_ausencia_min"], edad, en_rpv["estado"], en_rpv["estado"] == "pendiente")
                del pendientes[book_id]
            else:
                p["ultima_ausencia_min"], p["pasadas_ausente"] = edad, 0
            cambios = True
        elif edad <= SEGUIMIENTO_MIN:
            info = {"habitacion": e["nombre_habitacion"], "canal": e.get("canal", "Desconocido"), "creada": e["creada"]}
            if en_rpv:
                if edad <= VISTA_DIRECTA_MAX_MIN:   # ya estaba en RPV al verla: el retraso es como mucho su edad
                    anotar(book_id, info, 0, edad, en_rpv["estado"], en_rpv["estado"] == "pendiente")
                    cambios = True
            else:
                pendientes[book_id] = {**info, "ultima_ausencia_min": edad, "pasadas_ausente": 0}
                cambios = True

    for book_id, p in list(pendientes.items()):
        if book_id not in vistas:   # cancelada, movida o fuera de la ventana: tras varias pasadas se deja de seguir
            p["pasadas_ausente"] = p.get("pasadas_ausente", 0) + 1
            cambios = True
            if p["pasadas_ausente"] >= PASADAS_AUSENTE_PARA_OLVIDAR:
                del pendientes[book_id]
                continue
        edad = edad_min(p["creada"], ahora_utc)
        if edad is None or edad > SEGUIMIENTO_MIN:
            anotar(book_id, p, p["ultima_ausencia_min"], None, None, False, nunca_vista=True)
            del pendientes[book_id]
            cambios = True

    if cambios:
        ref = _doc_ref()
        try:
            ref.set({"pendientes": pendientes, "medidas": medidas[-MAX_MEDIDAS:]})
        except Exception as e:
            logger.error(f"[rpv_latencia] Error guardando en Firestore: {e}")
            return 0
    return nuevas


def estadisticas():
    """Resumen de las medidas: {muestras, mediana_min, p90_min, maximo_min, nunca_vistas, otras, fiable}. Los minutos
    son cotas superiores del retraso (hasta cuándo se vio por primera vez). Sin Firestore o sin medidas: todo a 0/None."""
    datos = _leer()
    medidas = datos["medidas"] if datos else []
    validas = sorted(m["maximo_min"] for m in medidas if m.get("importada") and m.get("maximo_min") is not None)
    n = len(validas)

    def percentil(p):
        return validas[max(0, math.ceil(p * n) - 1)] if n else None

    return {"muestras": n, "mediana_min": percentil(0.5), "p90_min": percentil(0.9), "maximo_min": validas[-1] if n else None,
            "nunca_vistas": sum(1 for m in medidas if m.get("nunca_vista")),
            "otras": sum(1 for m in medidas if not m.get("importada") and not m.get("nunca_vista")),
            "fiable": n >= MUESTRAS_FIABLES}


def seguimiento(ultimas=20):
    """Para el diagnóstico: {"pendientes": n, "ultimas": [las últimas medidas, sin ids ni datos personales]}."""
    datos = _leer() or {"pendientes": {}, "medidas": []}
    return {"pendientes": len(datos["pendientes"]),
            "ultimas": [{k: v for k, v in m.items() if k != "id"} for m in datos["medidas"][-ultimas:]]}


def espera_tipica_min(stats=None):
    """(minutos, fiable): lo que suele tardar RPV como mucho (percentil 90 medido, redondeado a 5 min, mínimo 15)
    o, sin medidas fiables, la estimación provisional."""
    stats = stats or estadisticas()
    if stats["fiable"]:
        return max(15, 5 * math.ceil(stats["p90_min"] / 5)), True
    return ESPERA_TIPICA_PROVISIONAL_MIN, False


def formato_min(minutos):
    """«25 min», «2 h», «1 h 20 min»."""
    minutos = max(0, int(minutos))
    if minutos < 60:
        return f"{minutos} min"
    h, resto = divmod(minutos, 60)
    return f"{h} h" if resto < 5 else f"{h} h {resto} min"


def consejo_reserva_no_en_rpv(edad, stats=None):
    """Qué decirle al huésped si su reserva aún no consta en RPV: (texto, anomala). `edad` son los minutos desde
    que se creó la reserva en Beds24 (None si no se sabe)."""
    if edad is None:
        return ("No sé cuándo se hizo la reserva. Pídale que lo intente de nuevo dentro de 30 minutos y, si sigue sin poder, "
                "revisa la reserva en RPV."), False
    tipica, fiable = espera_tipica_min(stats)
    if edad < tipica:
        restante = max(10, 5 * math.ceil((tipica - edad) / 5))
        provisional = "" if fiable else " (estimación provisional, aún sin suficientes medidas)"
        return (f"Es normal: la reserva se hizo hace {formato_min(edad)} y RPV suele tardar hasta unos {formato_min(tipica)}{provisional}. "
                f"Pídale que lo intente de nuevo dentro de {formato_min(restante)}; si todavía no puede, que pruebe otra vez una hora más tarde."), False
    return (f"Ya ha tardado más de lo normal: la reserva se hizo hace {formato_min(edad)} y lo habitual es hasta unos {formato_min(tipica)}. "
            "No hace falta que espere más: revisa la reserva en RPV y, si no está, créala a mano allí; después dígale que lo intente de nuevo."), True

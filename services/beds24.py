"""
services/beds24.py — Autenticación con Beds24, búsqueda de reservas y envío
del código de puerta vía Booking.com Messages.
"""
import logging
import threading
import time
import requests
from datetime import date, timedelta

from config import BEDS24_REFRESH_TOKEN, BEDS24_API_BASE, BEDS24_PROPERTY_ID, BEDS24_PROPERTY_IDS, ROOM_CONFIG
from services.guest_match import emparejar_nombre, normalizar_nombre, _match_exacto
from services.fechas import ahora_madrid, hoy_madrid

logger = logging.getLogger(__name__)


_token_cache = {"token": None, "expira": 0.0}   # access token (dura 24 h) y el instante hasta el que lo damos por bueno
_token_lock = threading.Lock()
MARGEN_TOKEN = 300          # se renueva 5 min antes de que caduque
REINTENTOS_TOKEN = 3
ESPERA_MAX_429 = 5          # segundos máximos que se espera entre reintentos tras un 429


def get_beds24_access_token():
    """
    Access token de Beds24 (24 h), pedido con el refresh token y guardado en memoria. Antes se pedía uno NUEVO en cada
    llamada (y un solo check-in hace varias), lo que acababa en «429 Too Many Requests» en /authentication/token y dejaba
    sin respuesta a la web de check-in. Ahora: caché; un candado para que, con la caché vacía (p. ej. tras un despliegue),
    varias peticiones simultáneas no pidan el token a la vez; y reintentos cortos si Beds24 aun así responde 429 o falla.
    El backend corre con 1 worker, así que la caché la comparten todas las peticiones.
    """
    if not BEDS24_REFRESH_TOKEN:
        raise Exception("BEDS24_REFRESH_TOKEN no configurado en Render.")
    if _token_cache["token"] and time.time() < _token_cache["expira"]:
        return _token_cache["token"]
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expira"]:   # otra petición ya lo renovó mientras esperábamos
            return _token_cache["token"]
        ultimo_error = None
        for intento in range(REINTENTOS_TOKEN):
            espera = 1 + intento
            try:
                resp = requests.get(
                    f"{BEDS24_API_BASE}/authentication/token",
                    headers={"refreshToken": BEDS24_REFRESH_TOKEN, "accept": "application/json"},
                    timeout=15,
                )
                if resp.status_code == 429:
                    try:
                        espera = min(float(resp.headers.get("Retry-After", espera)), ESPERA_MAX_429)
                    except (TypeError, ValueError):
                        pass
                resp.raise_for_status()
                datos = resp.json()
                _token_cache["token"] = datos["token"]
                _token_cache["expira"] = time.time() + max(float(datos.get("expiresIn") or 3600) - MARGEN_TOKEN, 60)
                return _token_cache["token"]
            except Exception as e:
                ultimo_error = e
                if intento < REINTENTOS_TOKEN - 1:
                    time.sleep(espera)
        raise ultimo_error


# ── Helper: búsqueda recursiva del número de Booking.com ─────────────────

# Campos de la reserva donde Beds24 guarda el número de confirmación de la plataforma. Revisión de seguridad 2026-10-18:
# antes se comparaba lo escrito con TODOS los campos, así que «Yuka» (firstName), «new» (status), «booking» (channel), «jp»
# (país) o un id de habitación o de propiedad abrían la primera reserva que coincidiera y mostraban su código de puerta.
CAMPOS_REFERENCIA = ("apiReference", "reference", "voucher", "externalId", "guestCode", "channelBookingId")
LONGITUD_MINIMA_REF = 5


def _ref_en_booking(obj, ref_norm, _path="", _clave=""):
    """
    Busca ref_norm en los campos de referencia (CAMPOS_REFERENCIA) del objeto de reserva devuelto por Beds24, a cualquier
    profundidad: el nombre del campo puede variar según versión de API o canal. Solo la igualdad exacta con el VALOR
    de uno de esos campos cuenta; nombres, correos, teléfonos, estado, canal o ids internos nunca.

    Devuelve el path del campo donde se halló (útil para el log) o None.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            r = _ref_en_booking(v, ref_norm, f"{_path}.{k}" if _path else k, k)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            r = _ref_en_booking(item, ref_norm, f"{_path}[{i}]", _clave)
            if r is not None:
                return r
    elif obj is not None and _clave in CAMPOS_REFERENCIA:
        if str(obj).strip().lower() == ref_norm:
            return _path or "raíz"
    return None


# ── Función principal: buscar reserva por número de Booking.com ──────────

# Campos con ids INTERNOS de Beds24 (casi correlativos, se pueden adivinar): no valen como «número de reserva»
# en la búsqueda pública. Los ids solo se aceptan si salen de un token firmado (buscar_booking_por_id).
CAMPOS_ID_INTERNOS = ("id", "masterId")


def buscar_booking_por_ref(booking_ref):
    """
    Busca en Beds24 la reserva cuyo número de confirmación de Booking.com
    coincida con booking_ref (el número que el huésped ve en su app/email).

    Estrategia:
      1. Obtiene TODAS las reservas con arrival en [-5 días, +180 días].
         Para 5 habitaciones, son ~30-40 objetos como máximo.
      2. Por cada reserva activa, busca booking_ref en todos sus campos
         de forma recursiva (_ref_en_booking).
      3. Loguea en qué campo exacto encontró la coincidencia.
         (Útil para saber qué campo usa tu versión de Beds24 si
         algún día necesitas optimizar la búsqueda.)

    Devuelve el dict completo de la reserva, o None si no se encuentra.
    """
    if not booking_ref or not booking_ref.strip():
        return None

    try:
        token = get_beds24_access_token()
    except Exception as e:
        logger.error(f"[check-in] Beds24 auth error en buscar_booking_por_ref: {e}")
        return None

    hoy   = hoy_madrid()
    desde = (hoy - timedelta(days=5)).isoformat()
    hasta = (hoy + timedelta(days=180)).isoformat()

    ref_norm = booking_ref.strip().lower()
    if len(ref_norm) < LONGITUD_MINIMA_REF:
        return None   # un número de confirmación real es más largo: «new» o «jp» no pueden abrir ninguna reserva

    # Recorre TODAS las propiedades configuradas (hostal + La Casa de la Primavera).
    # El huésped no indica de qué propiedad es su reserva, así que hay que
    # comprobarlas todas hasta encontrar una coincidencia.
    for property_id in BEDS24_PROPERTY_IDS:
        try:
            resp = requests.get(
                f"{BEDS24_API_BASE}/bookings",
                headers={"token": token, "accept": "application/json"},
                params={"propertyId": property_id,
                        "arrivalFrom": desde, "arrivalTo": hasta},
                timeout=20,
            )
            resp.raise_for_status()
            bookings = resp.json().get("data", [])
            logger.info(f"[check-in] Beds24 property {property_id}: {len(bookings)} reservas en rango {desde}→{hasta}")
        except Exception as e:
            logger.error(f"[check-in] Error consultando Beds24 (property {property_id}): {e}")
            continue

        for b in bookings:
            if str(b.get("status", "")).lower() == "cancelled":
                continue
            campo = _ref_en_booking(b, ref_norm)
            if campo in CAMPOS_ID_INTERNOS:
                continue
            if campo is not None:
                logger.info(
                    f"[check-in] Reserva encontrada: ref='{booking_ref}' "
                    f"en campo '{campo}' → book_id={b.get('id')} room={b.get('roomId')} property={property_id}"
                )
                return b

    logger.warning(f"[check-in] ref='{booking_ref}': no encontrado en ninguna propiedad ({BEDS24_PROPERTY_IDS}).")
    return None


def buscar_booking_por_id(book_id):
    """
    Reserva (no cancelada) con ese id de Beds24, o None. Solo se usa con ids que salen de un token firmado
    (services/acceso_enlace.py): el id de Beds24 es casi correlativo y por eso NO se acepta como referencia pública en
    buscar_booking_por_ref. Pregunta a Beds24 con el filtro `id` en cada propiedad.
    """
    book_id = str(book_id or "").strip()
    if not book_id.isdigit():
        return None
    try:
        token = get_beds24_access_token()
    except Exception as e:
        logger.error(f"[check-in] Beds24 auth error en buscar_booking_por_id: {e}")
        return None
    for property_id in BEDS24_PROPERTY_IDS:
        try:
            resp = requests.get(
                f"{BEDS24_API_BASE}/bookings",
                headers={"token": token, "accept": "application/json"},
                params={"propertyId": property_id, "id": book_id},
                timeout=20,
            )
            resp.raise_for_status()
            for b in resp.json().get("data", []):
                if str(b.get("id")) == book_id and str(b.get("status", "")).lower() != "cancelled":
                    return b
        except Exception as e:
            logger.error(f"[check-in] Error buscando la reserva {book_id} (property {property_id}): {e}")
    return None


def clave_grupo(booking):
    """Identificador del grupo de una reserva de varias habitaciones. Beds24 crea una reserva por habitación: la primera
    (la «maestra») tiene masterId vacío y las demás llevan en masterId el id de la maestra. Sin grupo, su propio id."""
    return str(booking.get("masterId") or booking.get("id") or "")


def clave_grupo_entrada(entrada):
    """Lo mismo que clave_grupo pero para las entradas de obtener_bookings_*_beds24 (dicts ya resumidos):
    el id de la reserva maestra del grupo de habitaciones o, sin grupo, el propio book_id."""
    return str(entrada.get("master_id") or entrada.get("book_id") or "")


def _colapsar_grupos(candidatos):
    """De una lista de (reserva, nombre), deja una por grupo de habitaciones: la maestra si está, si no la primera."""
    elegidos = {}
    for b, n in candidatos:
        k = clave_grupo(b)
        if k not in elegidos or str(b.get("id")) == k:
            elegidos[k] = (b, n)
    return list(elegidos.values())


_CACHE_GRUPOS = {}      # {id de la maestra: (instante, [reservas del grupo])}; evita repetir consultas en cada refresco de la web
TTL_GRUPOS = 300


def _pedir_reservas(token, params):
    try:
        resp = requests.get(f"{BEDS24_API_BASE}/bookings", headers={"token": token, "accept": "application/json"},
                            params={**params, "includePersonalInfo": "true"}, timeout=20)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        logger.error(f"[check-in] Error buscando las habitaciones del grupo ({params}): {e}")
        return []


def reservas_del_grupo(booking):
    """
    Todas las reservas (una por habitación, sin canceladas ni bloqueos) de la misma reserva de VARIAS HABITACIONES que
    `booking`, incluida ella misma y ordenadas por id. Si no es de grupo, o falla la consulta: [booking].

    Una reserva de varias habitaciones llega a Beds24 como una reserva por habitación con el mismo nombre y fechas; las
    hijas llevan en `masterId` el id de la primera. Se busca por masterId y, por si ese filtro no responde bien, también por
    la fecha de llegada, y se vuelve a filtrar en Python. Resultado en caché 5 min por grupo.
    """
    master = clave_grupo(booking)
    if not master.isdigit():
        return [booking]
    ahora = time.time()
    cache = _CACHE_GRUPOS.get(master)
    if cache and ahora - cache[0] < TTL_GRUPOS:
        miembros = cache[1]
    else:
        try:
            token = get_beds24_access_token()
        except Exception as e:
            logger.error(f"[check-in] Beds24 auth error en reservas_del_grupo: {e}")
            return [booking]
        propiedades = [booking["propertyId"]] if booking.get("propertyId") else BEDS24_PROPERTY_IDS
        encontrados = {}
        for pid in propiedades:
            consultas = [{"propertyId": pid, "masterId": master}]
            if booking.get("arrival"):
                consultas.append({"propertyId": pid, "arrivalFrom": booking["arrival"], "arrivalTo": booking["arrival"]})
            if str(booking.get("id")) != master:
                consultas.append({"propertyId": pid, "id": master})
            for params in consultas:
                for b in _pedir_reservas(token, params):
                    if str(b.get("id")) == master or str(b.get("masterId") or "") == master:
                        encontrados[str(b.get("id"))] = b
        miembros = list(encontrados.values())
        _CACHE_GRUPOS[master] = (ahora, miembros)
    resultado = {str(m.get("id")): m for m in miembros}
    resultado[str(booking.get("id"))] = booking   # la propia reserva, con los datos recién leídos
    validas = [b for b in resultado.values() if str(b.get("status", "")).lower() != "cancelled" and not es_bloqueo_beds24(b)]
    return sorted(validas, key=lambda b: int(b.get("id") or 0)) or [booking]


def _se_solapan(a, b):
    """True si las dos estancias comparten alguna noche (la salida de una el mismo día que entra la otra NO es solape)."""
    return (a.get("arrival") or "") < (b.get("departure") or "") and (b.get("arrival") or "") < (a.get("departure") or "")


def _mismo_huesped(a, b):
    """¿Son estas dos reservas (que no se solapan) del mismo huésped? Sí si el nombre completo es igual (al menos nombre y
    apellido). Y también si un nombre está contenido en el otro (p. ej. la noche extra creada a mano solo con el nombre de
    pila) pero SOLO cuando una estancia termina el mismo día que empieza la otra: sin hueco entre ambas."""
    na, nb = normalizar_nombre(_extraer_nombre_huesped_beds24(a)), normalizar_nombre(_extraer_nombre_huesped_beds24(b))
    if not na or not nb or _se_solapan(a, b):
        return False
    if na == nb and len(na) >= 2:
        return True
    seguidas = a.get("departure") == b.get("arrival") or b.get("departure") == a.get("arrival")
    return seguidas and (na <= nb or nb <= na)


def es_cadena_mismo_huesped(reservas):
    """True si todas son del MISMO huésped con estancias que no se solapan (ver _mismo_huesped, entre cada una y la
    siguiente por fecha). Dos personas con el mismo nombre alojadas a la vez se solapan y NO cuentan: así nunca se le
    muestra a alguien la habitación de otro."""
    orden = sorted(reservas, key=lambda b: b.get("arrival") or "")
    if any(_se_solapan(a, b) for i, a in enumerate(orden) for b in orden[i + 1:]):
        return False
    return all(_mismo_huesped(a, b) for a, b in zip(orden, orden[1:]))


def reserva_actual_de_cadena(reservas, hoy):
    """La que toca ahora: de las ya empezadas (llegada <= hoy), la que termina antes (el día del cambio, por la mañana, es
    la habitación que aún ocupa); si ninguna ha empezado, la de llegada más próxima. Las ya terminadas no deben venir aquí."""
    en_curso = [b for b in reservas if (b.get("arrival") or "") <= hoy.isoformat()]
    return min(en_curso, key=lambda b: b.get("departure") or "") if en_curso else min(reservas, key=lambda b: b.get("arrival") or "")


_CACHE_RECIENTES = {}   # {property_id: (instante, [reservas])}
TTL_RECIENTES = 300


def _reservas_recientes(property_id):
    """Reservas (crudas) de una propiedad con llegada entre hace 60 días y mañana: las que pueden estar en curso o empezar
    ya. Caché de 5 min por propiedad (la web consulta a menudo y Beds24 limita las llamadas)."""
    ahora = time.time()
    cache = _CACHE_RECIENTES.get(property_id)
    if cache and ahora - cache[0] < TTL_RECIENTES:
        return cache[1]
    token = get_beds24_access_token()
    hoy = hoy_madrid()
    reservas, vistos = [], set()
    for pagina in range(1, 6):
        datos = _pedir_reservas(token, {"propertyId": property_id, "arrivalFrom": (hoy - timedelta(days=60)).isoformat(),
                                        "arrivalTo": (hoy + timedelta(days=1)).isoformat(), "page": pagina})
        nuevas = [b for b in datos if b.get("id") not in vistos]
        if not nuevas:
            break
        vistos.update(b.get("id") for b in nuevas)
        reservas += nuevas
        if len(datos) < 100:
            break
    _CACHE_RECIENTES[property_id] = (ahora, reservas)
    return reservas


def reservas_encadenadas(booking):
    """
    Otras reservas VIGENTES del mismo huésped que `booking` (mismo nombre completo y sin solaparse con ella): p. ej. quien
    reserva una noche y después otra, quizá en otra habitación. Vigente = no cancelada, no terminada (antes de las 12:00 del
    día de salida) y con llegada como muy tarde mañana (el registro se puede hacer desde el día antes). No incluye las
    habitaciones de su mismo grupo (eso es reservas_del_grupo). Ordenadas por llegada; [] si no hay o falla la consulta.
    """
    ahora = ahora_madrid()
    hoy = ahora.date()
    try:
        recientes = _reservas_recientes(booking.get("propertyId")) if booking.get("propertyId") else \
            [b for pid in BEDS24_PROPERTY_IDS for b in _reservas_recientes(pid)]
    except Exception as e:
        logger.error(f"[check-in] No se pudieron consultar las otras reservas del huésped: {e}")
        return []
    propio_grupo = clave_grupo(booking)
    otras = []
    for b in recientes:
        if str(b.get("id")) == str(booking.get("id")) or clave_grupo(b) == propio_grupo:
            continue
        if str(b.get("status", "")).lower() == "cancelled" or es_bloqueo_beds24(b):
            continue
        try:
            llegada, salida = date.fromisoformat(b.get("arrival") or ""), date.fromisoformat(b.get("departure") or "")
        except ValueError:
            continue
        if hoy > salida or (hoy == salida and ahora.hour >= HORA_CHECKOUT) or llegada > hoy + timedelta(days=1):
            continue
        if es_cadena_mismo_huesped([booking, b]):
            otras.append(b)
    vistos, unicas = set(), []
    for b in sorted(otras, key=lambda b: b.get("arrival") or ""):
        if clave_grupo(b) not in vistos:   # de un grupo de habitaciones basta con una (la web mostrará el grupo entero)
            vistos.add(clave_grupo(b))
            unicas.append(b)
    return unicas


def buscar_booking_por_nombre(nombre_query, max_dias_estancia=60):
    """
    Busca una reserva por el nombre del huésped — es la vía principal de
    identificación en la web de check-in (no hay un número de reserva
    universal: La Casa de la Primavera recibe reservas de Booking, Airbnb y
    Holidu, cada plataforma con su propio formato de referencia).

    Se restringe a los candidatos relevantes AHORA MISMO, sin que el
    huésped tenga que decir nada de fechas — el sistema ya lo sabe:
      - llegada HOY o MAÑANA (el enlace se envía el día antes o el mismo
        día del check-in), o
      - huésped YA ALOJADO (llegada ≤ hoy ≤ salida)
    Esto evita comparar contra reservas de todo el año, mucho más propenso
    a confundir huéspedes con nombres parecidos.

    El emparejamiento en sí (normalización determinista, y Groq como
    respaldo para erratas de escritura) vive en services/guest_match.py —
    aquí solo se arma la lista de candidatos y se acota por fecha.

    Devuelve (booking, ambiguo): booking es el dict de Beds24 si hay una
    única coincidencia razonablemente segura, o None si no hay ninguna o
    sigue siendo ambiguo tras pasar por Groq (ambiguo=True en ese caso, para
    poder distinguir "no encontrado" de "hacen falta más datos").
    """
    if not nombre_query or not nombre_query.strip():
        return None, False

    try:
        token = get_beds24_access_token()
    except Exception as e:
        logger.error(f"[check-in] Beds24 auth error en buscar_booking_por_nombre: {e}")
        return None, False

    ahora  = ahora_madrid()
    hoy    = ahora.date()
    manana = hoy + timedelta(days=1)
    # arrivalFrom amplio para no perder a huéspedes ya alojados con una
    # estancia larga en curso; el filtro real de relevancia es en Python.
    desde_total = hoy - timedelta(days=max_dias_estancia)
    hasta_total = manana

    # IMPORTANTE: igual que en mobile_routes.py::_load_bookings, Beds24
    # devuelve resultados incompletos cuando el rango arrivalFrom→arrivalTo
    # es amplio en una sola llamada (confirmado con un caso real: una
    # reserva con llegada HOY, dentro del rango pedido, no aparecía en la
    # respuesta). Se trocea en ventanas de 30 días y se pide "limit": 500
    # explícito, igual que las demás consultas amplias de este proyecto.
    CHUNK_DAYS = 30
    candidatos_con_nombre = []   # vigentes: llegada hoy/mañana o estancia en curso
    candidatos_terminados = []   # estancia que acaba de terminar (pasadas las 12:00 del día de salida)
    seen_ids = set()
    for property_id in BEDS24_PROPERTY_IDS:
        chunk_start = desde_total
        while chunk_start <= hasta_total:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), hasta_total)
            try:
                resp = requests.get(
                    f"{BEDS24_API_BASE}/bookings",
                    headers={"token": token, "accept": "application/json"},
                    params={"propertyId": property_id,
                            "arrivalFrom": chunk_start.isoformat(),
                            "arrivalTo": chunk_end.isoformat(),
                            "limit": 500},
                    timeout=20,
                )
                resp.raise_for_status()
                bookings = resp.json().get("data", [])
            except Exception as e:
                logger.error(
                    f"[check-in] Error consultando Beds24 por nombre "
                    f"(property {property_id}, {chunk_start}→{chunk_end}): {e}"
                )
                bookings = []

            for b in bookings:
                bid = b.get("id")
                if bid is not None and bid in seen_ids:
                    continue  # evitar duplicados en los bordes de cada trozo
                if str(b.get("status", "")).lower() == "cancelled":
                    continue
                try:
                    arrival_date   = date.fromisoformat(b.get("arrival", ""))
                    departure_date = date.fromisoformat(b.get("departure", ""))
                except (ValueError, TypeError):
                    continue
                llegada_inminente = arrival_date in (hoy, manana)
                # Una estancia que ya terminó (pasadas las 12:00 del día de salida) no compite con las vigentes: si no, quien
                # encadena dos reservas (p. ej. una de ayer y otra de hoy) saldría con DOS coincidencias y no se le reconocería.
                estancia_terminada = hoy == departure_date and ahora.hour >= HORA_CHECKOUT
                ya_alojado         = arrival_date <= hoy <= departure_date
                if not (llegada_inminente or ya_alojado):
                    continue
                if bid is not None:
                    seen_ids.add(bid)
                destino = candidatos_terminados if (estancia_terminada and not llegada_inminente) else candidatos_con_nombre
                destino.append((b, _extraer_nombre_huesped_beds24(b)))

            chunk_start = chunk_end + timedelta(days=1)

    # Una reserva de varias habitaciones llega como una reserva por habitación con el MISMO nombre: contarlas como varias
    # coincidencias la dejaría «ambigua». Se deja una por grupo (la principal); /check-in añade después todas las habitaciones.
    candidatos_con_nombre = _colapsar_grupos(candidatos_con_nombre)
    candidatos_terminados = _colapsar_grupos(candidatos_terminados)

    desde, hasta = desde_total.isoformat(), hasta_total.isoformat()

    # El mismo huésped con varias reservas seguidas (p. ej. una noche y luego otra en otra habitación) sale con varias
    # coincidencias iguales: no es ambiguo. Se elige la que toca ahora y /check-in ofrece las demás («otras_reservas»).
    q_tokens = normalizar_nombre(nombre_query)
    exactos = [b for b, n in candidatos_con_nombre if q_tokens and _match_exacto(q_tokens, [normalizar_nombre(n)])]
    if len(exactos) > 1 and es_cadena_mismo_huesped(exactos):
        actual = reserva_actual_de_cadena(exactos, hoy)
        logger.info(f"[check-in] '{nombre_query}': {len(exactos)} reservas encadenadas del mismo huésped → book_id={actual.get('id')}")
        return actual, False

    booking, ambiguo = emparejar_nombre(nombre_query, candidatos_con_nombre)
    if booking is None:
        # Sin ninguna coincidencia VIGENTE: puede ser alguien cuya estancia acaba de terminar (que vea «estancia finalizada»
        # en vez de «no encontrado»). Solo coincidencia directa y nunca si hay vigentes que coincidan (ahí manda lo ambiguo).
        q_tokens = normalizar_nombre(nombre_query)
        if q_tokens and not _match_exacto(q_tokens, [normalizar_nombre(n) for _, n in candidatos_con_nombre]):
            previas = [c for c in candidatos_terminados if _match_exacto(q_tokens, [normalizar_nombre(c[1])])]
            if previas:
                booking, ambiguo = max(previas, key=lambda c: c[0].get("departure") or "")[0], False
    if booking is not None:
        logger.info(
            f"[check-in] Reserva encontrada por nombre: query='{nombre_query}' "
            f"→ book_id={booking.get('id')} room={booking.get('roomId')}"
        )
    elif ambiguo:
        logger.warning(
            f"[check-in] Nombre ambiguo o inseguro tras Groq: query='{nombre_query}' "
            f"entre {len(candidatos_con_nombre)} candidato(s) en el rango {desde}→{hasta}."
        )
    else:
        logger.warning(f"[check-in] nombre='{nombre_query}': no encontrado en el rango {desde}→{hasta}.")
    return booking, ambiguo


# Nombre de habitación en el formato original (el que usa registroparteviajeros.com
# y el que se mostraba antes en el resumen de WhatsApp), para no cambiar el
# formato del mensaje aunque los datos ahora vengan de Beds24.
ROOM_ID_DISPLAY_NAME = {
    "702397": "Habitación Simple 1",
    "702398": "Habitación Simple 2",
    "702399": "Habitación Simple 3",
    "702396": "Habitación Doble 4",
    "702395": "Habitación Deluxe 5",
}


CANAL_LABELS = {
    "booking": "Booking.com",
    "airbnb": "Airbnb",
    "direct": "Directo",
    "vrbo": "Vrbo",
    "expedia": "Expedia",
    "holidu": "Holidu",
    "homeaway": "HomeAway",
    "hostelworld": "Hostelworld",
    "tripcom": "Trip.com",
}


def _canal_legible(booking):
    """Nombre del canal de la reserva para mostrar en avisos de WhatsApp.
    apiSource ya viene legible ('Booking.com', 'Direct'...) en la mayoría de
    reservas; si falta, se cae a 'channel' (código corto tipo 'booking')
    traducido con CANAL_LABELS."""
    api_source = (booking.get("apiSource") or "").strip()
    if api_source:
        return api_source
    channel = (booking.get("channel") or "").strip().lower()
    if not channel:
        return "Desconocido"
    return CANAL_LABELS.get(channel, channel.capitalize())


def _extraer_nombre_huesped_beds24(booking):
    """Beds24 puede devolver el nombre del huésped en distintos campos según
    la versión/canal. Probamos varias claves habituales antes de rendirnos."""
    guest = booking.get("guest") or {}
    nombre = guest.get("firstName") or booking.get("firstName") or booking.get("guestFirstName") or ""
    apellido = guest.get("lastName") or booking.get("lastName") or booking.get("guestLastName") or ""
    nombre_completo = f"{nombre} {apellido}".strip()
    return nombre_completo or booking.get("guestName") or "Huésped sin nombre"


EMAIL_BLOQUEO_APP = "bloqueo@bloqueo.com"
HORA_CHECKOUT = 12   # a esa hora (Madrid) del día de salida la estancia se da por terminada (igual que en routes/checkin.py)


def es_bloqueo_beds24(booking):
    """True si esta «reserva» de Beds24 es un bloqueo de calendario y no un huésped: estado «black», la reserva
    ficticia que crea la app de gestión (email bloqueo@bloqueo.com, nombre «Fecha bloqueada») o un bloqueo importado
    por iCal de otro calendario («BLOCKED by Holidu»). Los bloqueos no tienen parte de viajeros: no deben salir como
    entradas o salidas en los resúmenes ni en los avisos, ni medirse contra RPV. Las importaciones por iCal con otro
    nombre NO son bloqueos (son reservas reales de Holidu)."""
    if str(booking.get("status", "")).lower() == "black":
        return True
    guest = booking.get("guest") or {}
    email = (guest.get("email") or booking.get("email") or "").strip().lower()
    if email == EMAIL_BLOQUEO_APP:
        return True
    nombre = f"{guest.get('firstName') or booking.get('firstName') or ''} {guest.get('lastName') or booking.get('lastName') or ''}".strip().lower()
    return nombre.startswith(("fecha bloqueada", "blocked"))


def obtener_bookings_dia_beds24(fecha_iso, tipo="checkin"):
    """
    Consulta en Beds24 las reservas con entrada (arrival) o salida (departure)
    en una fecha concreta (fecha_iso formato YYYY-MM-DD). tipo: "checkin" o "checkout".

    IMPORTANTE: los parámetros de filtro por fecha de la API (arrivalFrom/
    arrivalTo, departureFrom/departureTo) no son fiables al 100% — se detectó
    que en algunos casos Beds24 devuelve reservas de fechas no solicitadas.
    Por eso, además de pasar el filtro en la petición, SIEMPRE se vuelve a
    filtrar explícitamente en Python comparando el campo real 'arrival' o
    'departure' de cada reserva contra fecha_iso de forma exacta.

    Recorre TODAS las propiedades de BEDS24_PROPERTY_IDS (hostal + La Casa
    de la Primavera) — si solo se consultara BEDS24_PROPERTY_ID, el resumen
    diario nunca mostraría las entradas/salidas de la segunda propiedad.

    Devuelve una lista de dicts: {room_id, nombre_habitacion, huesped, book_id,
    arrival, departure, canal, email, country}.
    Si falla la consulta (token, red, etc.) devuelve lista vacía y loguea el error,
    para no romper el resumen diario por un problema puntual de Beds24.
    """
    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        logger.error(f"[CONTROL] Beds24 auth falló al consultar reservas de {tipo} ({fecha_iso}): {e}")
        return []

    campo_fecha = "arrival" if tipo == "checkin" else "departure"

    resultado = []
    for property_id in BEDS24_PROPERTY_IDS:
        params = {"propertyId": property_id, "includePersonalInfo": "true"}
        if tipo == "checkin":
            params["arrivalFrom"] = fecha_iso
            params["arrivalTo"] = fecha_iso
        else:
            params["departureFrom"] = fecha_iso
            params["departureTo"] = fecha_iso

        try:
            resp = requests.get(
                f"{BEDS24_API_BASE}/bookings",
                headers={"token": access_token, "accept": "application/json"},
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as e:
            logger.error(f"[CONTROL] Error consultando reservas de {tipo} para {fecha_iso} (property {property_id}): {e}")
            continue

        descartadas_por_fecha = 0
        for b in data:
            # Filtrado explícito: NO confiar en que la API ya haya filtrado bien.
            if b.get(campo_fecha) != fecha_iso:
                descartadas_por_fecha += 1
                continue
            if str(b.get("status", "")).lower() == "cancelled":
                continue
            if es_bloqueo_beds24(b):
                continue
            room_id = str(b.get("roomId", ""))
            if not room_id:
                continue
            guest = b.get("guest") or {}
            resultado.append({
                "room_id": room_id,
                "nombre_habitacion": ROOM_ID_DISPLAY_NAME.get(room_id, ROOM_CONFIG.get(room_id, {}).get("nombre", f"Room {room_id}")),
                "huesped": _extraer_nombre_huesped_beds24(b),
                "book_id": b.get("id"),
                "arrival": b.get("arrival"),
                "departure": b.get("departure"),
                "canal": _canal_legible(b),
                "email": guest.get("email") or b.get("email"),
                "country": guest.get("country") or b.get("country"),
                "creada": b.get("bookingTime"),
                "master_id": b.get("masterId"),
            })

        if descartadas_por_fecha:
            logger.info(
                f"obtener_bookings_dia_beds24({tipo}, {fecha_iso}, property {property_id}): la API devolvió "
                f"{len(data)} reservas, {descartadas_por_fecha} descartadas por no "
                f"coincidir la fecha exacta de '{campo_fecha}' (filtro de la API no fiable)."
            )
    return resultado


def obtener_bookings_rango_beds24(fecha_desde_iso, fecha_hasta_iso, tipo="checkin"):
    """
    Como obtener_bookings_dia_beds24, pero para un RANGO de fechas en una
    sola llamada por propiedad (2 llamadas a Beds24 en total, no una por
    día) — pensada para escaneos periódicos de una ventana de varios días
    (ver services/registro_completado_avisos.py) sin repetir el patrón de
    demasiadas llamadas encadenadas que causó el incidente de "Credit limit
    exceeded" de sep 2026 (ver memoria beds24-integracion).

    Devuelve la misma forma de dict que obtener_bookings_dia_beds24:
    {room_id, nombre_habitacion, huesped, book_id, arrival, departure,
    canal, email, country}.
    """
    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        logger.error(f"[CONTROL] Beds24 auth falló al consultar rango de {tipo} ({fecha_desde_iso}→{fecha_hasta_iso}): {e}")
        return []

    campo_fecha = "arrival" if tipo == "checkin" else "departure"

    resultado = []
    for property_id in BEDS24_PROPERTY_IDS:
        params = {"propertyId": property_id, "includePersonalInfo": "true"}
        if tipo == "checkin":
            params["arrivalFrom"] = fecha_desde_iso
            params["arrivalTo"] = fecha_hasta_iso
        else:
            params["departureFrom"] = fecha_desde_iso
            params["departureTo"] = fecha_hasta_iso

        try:
            resp = requests.get(
                f"{BEDS24_API_BASE}/bookings",
                headers={"token": access_token, "accept": "application/json"},
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as e:
            logger.error(f"[CONTROL] Error consultando rango de {tipo} ({fecha_desde_iso}→{fecha_hasta_iso}, property {property_id}): {e}")
            continue

        for b in data:
            fecha_campo = b.get(campo_fecha)
            # Filtrado explícito por rango — igual que en obtener_bookings_dia_beds24,
            # no confiar en que la API ya haya filtrado bien.
            if not fecha_campo or not (fecha_desde_iso <= fecha_campo <= fecha_hasta_iso):
                continue
            if str(b.get("status", "")).lower() == "cancelled":
                continue
            if es_bloqueo_beds24(b):
                continue
            room_id = str(b.get("roomId", ""))
            if not room_id:
                continue
            guest = b.get("guest") or {}
            resultado.append({
                "room_id": room_id,
                "nombre_habitacion": ROOM_ID_DISPLAY_NAME.get(room_id, ROOM_CONFIG.get(room_id, {}).get("nombre", f"Room {room_id}")),
                "huesped": _extraer_nombre_huesped_beds24(b),
                "book_id": b.get("id"),
                "arrival": b.get("arrival"),
                "departure": b.get("departure"),
                "canal": _canal_legible(b),
                "email": guest.get("email") or b.get("email"),
                "country": guest.get("country") or b.get("country"),
                "creada": b.get("bookingTime"),
                "modificada": b.get("modifiedTime"),
                "status": b.get("status"),
                "master_id": b.get("masterId"),
                "referer": b.get("referer"),
                "api_reference": b.get("apiReference"),
                "nombre_huesped": {"nombre": guest.get("firstName") or b.get("firstName") or "",
                                   "apellidos": guest.get("lastName") or b.get("lastName") or ""},
                "telefono": guest.get("phone") or b.get("phone"),
                "movil": guest.get("mobile") or b.get("mobile"),
            })

    return resultado

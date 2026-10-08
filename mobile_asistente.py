"""
mobile_asistente.py — Asistente de la app móvil: responde preguntas sobre reservas,
ocupación y finanzas y puede dibujar gráficos.

No se le pasan los datos en el prompt (con cientos de reservas superaba el límite de
tamaño de Groq y el modelo calculaba mal): se le dan HERRAMIENTAS de solo lectura
(function calling) y las consulta él. Las cifras las calcula siempre este código, no el
modelo:
  - listar_reservas: reservas concretas, con filtros (canal, habitación, huésped, fechas).
  - resumen: recuentos, noches, ocupación e ingresos agrupados por canal, habitación,
    tipo de habitación, mes o en total.
  - grafico: calcula lo mismo que resumen y lo devuelve a la app como gráfico (tarta,
    barras o línea). El modelo no transcribe números: solo elige qué dibujar.
  - finanzas_mes: el informe de la pestaña Finanzas (incluida la rentabilidad estimada).

Permisos (los decide el servidor, nunca el modelo): los datos económicos del Hostal solo
con el PIN de administrador; los de La Casa de la Primavera con el PIN normal si la app
tiene Finanzas desbloqueado (ese desbloqueo es una comprobación del cliente, igual que en
la pestaña Finanzas). Sin permiso económico, ni siquiera se devuelven los precios.

Es solo lectura: no crea, modifica ni cancela nada.
"""
import json
import logging
import time
import unicodedata
from datetime import date, timedelta

import requests

from config import GROQ_API_KEY, GROQ_API_URL, GROQ_MODEL_FALL, GROQ_MODEL_PRI
from services.fechas import hoy_madrid

logger = logging.getLogger(__name__)

DIAS_ATRAS = 400          # las reservas se descargan con llegada entre hoy-400 y hoy+180 días
DIAS_ADELANTE = 180
TTL_CACHE = 10 * 60       # copia en memoria de las reservas de cada propiedad (Beds24 limita las llamadas)
TTL_CACHE_INCOMPLETA = 60  # si Beds24 falló en algún tramo, se vuelve a intentar pronto
MAX_PASOS = 6             # rondas modelo → herramientas como máximo
PRESUPUESTO_SEG = 45      # gunicorn mata la petición a los 60 s
MAX_MENSAJES = 12         # mensajes de la conversación que se envían al modelo
MAX_CHARS_MENSAJE = 1500
MAX_GRAFICOS = 3
MAX_LISTADO = 25

DIAS_SEMANA = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
AGRUPACIONES = ("canal", "habitacion", "tipo_habitacion", "mes", "total")
METRICAS_DINERO = ("ingresos_brutos", "ingresos_netos", "comisiones")
METRICAS = METRICAS_DINERO + ("reservas", "noches", "ocupacion_pct")
UNIDADES = {"ingresos_brutos": "eur", "ingresos_netos": "eur", "comisiones": "eur",
            "reservas": "reservas", "noches": "noches", "ocupacion_pct": "%"}

_cache = {}   # property_id -> {"t": epoch, "reservas": [...], "incompleto": bool}


class ErrorHerramienta(Exception):
    """Error que se le devuelve al modelo como resultado de la herramienta para que se corrija
    o se lo explique al usuario."""


# ── Datos ───────────────────────────────────────────────────────────────────

def _mr():
    import mobile_routes  # import tardío: mobile_routes importa este módulo
    return mobile_routes


def _norm(texto):
    sin_tildes = unicodedata.normalize("NFD", str(texto or "")).encode("ascii", "ignore").decode()
    return sin_tildes.lower().strip()


def _ids_propiedad(clave):
    mr = _mr()
    if clave == "hostal":
        return mr.PROPERTY_ID
    if clave == "primavera":
        return mr.PROPERTY_ID_CASA_PRIMAVERA
    raise ErrorHerramienta('propiedad debe ser "hostal" o "primavera"')


def _habitaciones(property_id):
    """[{room_id, nombre, tipo}] de la propiedad."""
    mr = _mr()
    nombres = {str(r["id"]): r["name"] for r in mr.ROOMS}
    if property_id == mr.PROPERTY_ID_CASA_PRIMAVERA:
        ids = ["720841"]
    else:
        ids = [rid for g in mr.HOSTAL_GRUPOS_HABITACION for rid in g["room_ids"]]
    return [{"room_id": rid, "nombre": nombres.get(rid, f"Room {rid}"), "tipo": mr._finance_tipo_habitacion(property_id, rid)} for rid in ids]


def _reservas(property_id):
    """Reservas no canceladas ni bloqueos de la propiedad (copia en memoria de 10 min):
    [{id, checkin, checkout, noches, room_id, habitacion, tipo, canal, huesped, bruto, comision}].
    Devuelve (reservas, incompleto)."""
    ahora = time.time()
    c = _cache.get(property_id)
    if c and ahora - c["t"] < (TTL_CACHE_INCOMPLETA if c["incompleto"] else TTL_CACHE):
        return c["reservas"], c["incompleto"]

    mr = _mr()
    hoy = hoy_madrid()
    raw, fallidos = mr._fetch_bookings_finance(property_id, hoy - timedelta(days=DIAS_ATRAS), hoy + timedelta(days=DIAS_ADELANTE))
    nombres = {h["room_id"]: h for h in _habitaciones(property_id)}
    reservas = []
    for b in raw:
        if str(b.get("status", "")).lower() == "cancelled" or mr._finance_es_bloqueo(b):
            continue
        try:
            checkin = date.fromisoformat((b.get("arrival") or "")[:10])
            checkout = date.fromisoformat((b.get("departure") or "")[:10])
        except Exception:
            continue
        noches = (checkout - checkin).days
        if noches <= 0:
            continue
        room_id = str(b.get("roomId") or "")
        canal = mr._finance_channel_label(b)
        bruto = float(b.get("price") or 0)
        comision = float(b.get("commission") or 0)
        if comision <= 0:   # igual que Finanzas: sin comisión real en Beds24 se estima por portal
            comision = bruto * mr._finance_comision_fallback_pct(canal)
        guest = b.get("guest") or {}
        huesped = f"{guest.get('firstName') or b.get('firstName') or ''} {guest.get('lastName') or b.get('lastName') or ''}".strip()
        hab = nombres.get(room_id) or {"nombre": "Desconocida", "tipo": "Desconocido"}
        reservas.append({"id": b.get("id"), "checkin": checkin, "checkout": checkout, "noches": noches, "room_id": room_id,
                         "habitacion": hab["nombre"], "tipo": hab["tipo"], "canal": canal, "huesped": huesped or "Desconocido",
                         "bruto": bruto, "comision": comision})
    _cache[property_id] = {"t": ahora, "reservas": reservas, "incompleto": fallidos > 0}
    return reservas, fallidos > 0


def _puede_dinero(property_id, ctx):
    mr = _mr()
    if ctx.get("es_admin"):
        return True
    return bool(ctx.get("finanzas")) and property_id == mr.PROPERTY_ID_CASA_PRIMAVERA


def _fecha(valor, nombre):
    try:
        return date.fromisoformat(str(valor)[:10])
    except Exception:
        raise ErrorHerramienta(f"{nombre} debe tener formato YYYY-MM-DD")


def _rango(a, obligatorio):
    hoy = hoy_madrid()
    if obligatorio and not (a.get("desde") and a.get("hasta")):
        raise ErrorHerramienta("Faltan desde y hasta (YYYY-MM-DD)")
    desde = _fecha(a["desde"], "desde") if a.get("desde") else hoy - timedelta(days=DIAS_ATRAS)
    hasta = _fecha(a["hasta"], "hasta") if a.get("hasta") else hoy + timedelta(days=DIAS_ADELANTE)
    if hasta < desde:
        raise ErrorHerramienta("hasta no puede ser anterior a desde")
    if (hasta - desde).days > 800:
        raise ErrorHerramienta("El rango máximo es de 800 días")
    return desde, hasta


def _avisos(incompleto, desde):
    avisos = []
    inicio = hoy_madrid() - timedelta(days=DIAS_ATRAS)
    if desde < inicio:
        avisos.append(f"Solo hay datos de reservas con llegada desde el {inicio.isoformat()}")
    if incompleto:
        avisos.append("Beds24 no respondió para algún tramo de fechas: las cifras pueden estar incompletas")
    return avisos


# ── Herramientas ────────────────────────────────────────────────────────────

def _t_listar_reservas(a, ctx):
    property_id = _ids_propiedad(a.get("propiedad"))
    desde, hasta = _rango(a, obligatorio=False)
    dinero = _puede_dinero(property_id, ctx)
    reservas, incompleto = _reservas(property_id)
    hoy = hoy_madrid()
    momento = a.get("momento") or "todas"
    filtros = {"canal": _norm(a.get("canal")), "habitacion": _norm(a.get("habitacion")), "huesped": _norm(a.get("huesped"))}

    def coincide(r):
        if not desde <= r["checkin"] <= hasta:
            return False
        if momento == "pasadas" and not r["checkout"] <= hoy:
            return False
        if momento == "en_curso" and not r["checkin"] <= hoy < r["checkout"]:
            return False
        if momento == "futuras" and not r["checkin"] > hoy:
            return False
        if filtros["canal"] and filtros["canal"] not in _norm(r["canal"]):
            return False
        if filtros["habitacion"] and filtros["habitacion"] not in _norm(r["habitacion"]) and filtros["habitacion"] not in _norm(r["tipo"]):
            return False
        return not filtros["huesped"] or filtros["huesped"] in _norm(r["huesped"])

    elegidas = sorted((r for r in reservas if coincide(r)), key=lambda r: (r["checkin"], r["checkout"]),
                      reverse=(a.get("orden") or "recientes") != "antiguas")
    try:
        limite = max(1, min(int(a.get("limite") or 10), MAX_LISTADO))
    except (TypeError, ValueError):
        limite = 10
    filas = []
    for r in elegidas[:limite]:
        fila = {"checkin": r["checkin"].isoformat(), "checkout": r["checkout"].isoformat(), "noches": r["noches"],
                "habitacion": r["habitacion"], "huesped": r["huesped"], "canal": r["canal"]}
        if dinero:
            fila["precio_eur"] = round(r["bruto"], 2)
        filas.append(fila)
    resultado = {"total_coinciden": len(elegidas), "mostrando": len(filas), "reservas": filas}
    if not dinero:
        resultado["nota"] = "Sin acceso a datos económicos: no se incluyen precios"
    if avisos := _avisos(incompleto, desde):
        resultado["avisos"] = avisos
    return resultado


def _meses(desde, hasta):
    """[(clave 'YYYY-MM', primer día, último día)] de cada mes que toca el rango."""
    meses, cur = [], date(desde.year, desde.month, 1)
    while cur <= hasta:
        siguiente = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        meses.append((f"{cur:%Y-%m}", max(cur, desde), min(siguiente - timedelta(days=1), hasta)))
        cur = siguiente
    return meses


def _calcular_grupos(property_id, desde, hasta, agrupar_por, dinero):
    """Reparte noches e ingresos de cada reserva proporcionalmente por noches dentro del rango
    (igual que la pestaña Finanzas). Devuelve (grupos, totales, incompleto)."""
    if agrupar_por not in AGRUPACIONES:
        raise ErrorHerramienta(f"agrupar_por debe ser uno de {', '.join(AGRUPACIONES)}")
    reservas, incompleto = _reservas(property_id)
    habitaciones = _habitaciones(property_id)
    dias = (hasta - desde).days + 1
    fin_excl = hasta + timedelta(days=1)

    grupos = {}   # clave -> acumulador, en el orden en que se siembran

    def grupo(clave, disponibles):
        return grupos.setdefault(clave, {"ids": set(), "noches": 0, "bruto": 0.0, "comision": 0.0, "disponibles": disponibles})

    if agrupar_por == "habitacion":
        for h in habitaciones:
            grupo(h["nombre"], dias)
    elif agrupar_por == "tipo_habitacion":
        for h in habitaciones:
            g = grupo(h["tipo"], 0)
            g["disponibles"] += dias
    elif agrupar_por == "total":
        grupo("Total", len(habitaciones) * dias)
    elif agrupar_por == "mes":
        for clave, ini, fin in _meses(desde, hasta):
            grupo(clave, len(habitaciones) * ((fin - ini).days + 1))

    total = {"ids": set(), "noches": 0, "bruto": 0.0, "comision": 0.0}
    for r in reservas:
        ini, fin = max(r["checkin"], desde), min(r["checkout"], fin_excl)
        if (fin - ini).days <= 0:
            continue
        # tramos (clave, noches) en los que cae esta reserva dentro del rango
        if agrupar_por == "mes":
            tramos = [(clave, (min(fin, f + timedelta(days=1)) - max(ini, i)).days) for clave, i, f in _meses(desde, hasta)
                      if min(fin, f + timedelta(days=1)) > max(ini, i)]
        else:
            clave = {"canal": r["canal"], "habitacion": r["habitacion"], "tipo_habitacion": r["tipo"], "total": "Total"}[agrupar_por]
            tramos = [(clave, (fin - ini).days)]
        for clave, n in tramos:
            frac = n / r["noches"]
            for acc in (grupo(clave, 0), total):
                acc["ids"].add(r["id"])
                acc["noches"] += n
                acc["bruto"] += r["bruto"] * frac
                acc["comision"] += r["comision"] * frac

    def volcar(clave, acc, con_ocupacion):
        fila = {"grupo": clave, "reservas": len(acc["ids"]), "noches": acc["noches"]}
        if con_ocupacion:
            fila["ocupacion_pct"] = round(100 * acc["noches"] / acc["disponibles"], 1) if acc["disponibles"] else 0.0
            fila["noches_libres"] = acc["disponibles"] - acc["noches"]
        if dinero:
            fila["ingresos_brutos"] = round(acc["bruto"], 2)
            fila["comisiones"] = round(acc["comision"], 2)
            fila["ingresos_netos"] = round(acc["bruto"] - acc["comision"], 2)
        return fila

    por_canal = agrupar_por == "canal"   # la ocupación solo tiene sentido por habitación, tipo, mes o total
    filas = [volcar(k, g, not por_canal) for k, g in grupos.items()]
    if agrupar_por != "mes":
        filas.sort(key=lambda f: -(f.get("ingresos_brutos", 0) if dinero else f["noches"]))
    total["disponibles"] = len(habitaciones) * dias
    return filas, volcar("Total", total, True), incompleto


def _t_resumen(a, ctx):
    property_id = _ids_propiedad(a.get("propiedad"))
    desde, hasta = _rango(a, obligatorio=True)
    dinero = _puede_dinero(property_id, ctx)
    filas, totales, incompleto = _calcular_grupos(property_id, desde, hasta, a.get("agrupar_por") or "total", dinero)
    resultado = {"propiedad": a["propiedad"], "desde": desde.isoformat(), "hasta": hasta.isoformat(), "agrupado_por": a.get("agrupar_por") or "total",
                 "grupos": filas, "totales": totales,
                 "nota": "Noches e ingresos repartidos proporcionalmente por noche dentro del periodo. Ingresos brutos = precio de la reserva; netos = brutos menos comisión del canal."}
    if not dinero:
        resultado["nota"] += " Sin acceso a datos económicos: no se incluyen importes."
    if avisos := _avisos(incompleto, desde):
        resultado["avisos"] = avisos
    return resultado


def _t_grafico(a, ctx):
    if len(ctx.setdefault("graficos", [])) >= MAX_GRAFICOS:
        raise ErrorHerramienta(f"Máximo {MAX_GRAFICOS} gráficos por respuesta")
    tipo = a.get("tipo")
    if tipo not in ("tarta", "barras", "linea"):
        raise ErrorHerramienta('tipo debe ser "tarta", "barras" o "linea"')
    metrica = a.get("metrica")
    if metrica not in METRICAS:
        raise ErrorHerramienta(f"metrica debe ser una de {', '.join(METRICAS)}")
    agrupar_por = a.get("agrupar_por")
    if agrupar_por not in AGRUPACIONES or agrupar_por == "total":
        raise ErrorHerramienta("agrupar_por debe ser canal, habitacion, tipo_habitacion o mes")
    if metrica == "ocupacion_pct" and agrupar_por == "canal":
        raise ErrorHerramienta("La ocupación no se puede agrupar por canal")
    if tipo == "tarta" and metrica == "ocupacion_pct":
        raise ErrorHerramienta("La ocupación no suma 100%: usa barras o línea")
    if tipo == "linea" and agrupar_por != "mes":
        raise ErrorHerramienta("El gráfico de línea solo sirve para agrupar_por=mes")
    property_id = _ids_propiedad(a.get("propiedad"))
    desde, hasta = _rango(a, obligatorio=True)
    dinero = _puede_dinero(property_id, ctx)
    if metrica in METRICAS_DINERO and not dinero:
        raise ErrorHerramienta("Sin acceso a datos económicos de esta propiedad con este usuario")
    filas, _totales, incompleto = _calcular_grupos(property_id, desde, hasta, agrupar_por, dinero)
    if tipo == "tarta":
        filas = [f for f in filas if f[metrica] > 0]
    if not filas:
        raise ErrorHerramienta("No hay datos que dibujar en ese periodo")
    grafico = {"tipo": tipo, "titulo": str(a.get("titulo") or "")[:90], "unidad": UNIDADES[metrica],
               "etiquetas": [f["grupo"] for f in filas], "valores": [f[metrica] for f in filas]}
    ctx["graficos"].append(grafico)
    resultado = {"ok": True, "grafico_mostrado_al_usuario": True, "datos": [{"etiqueta": e, "valor": v} for e, v in zip(grafico["etiquetas"], grafico["valores"])]}
    if avisos := _avisos(incompleto, desde):
        resultado["avisos"] = avisos
    return resultado


def _t_finanzas_mes(a, ctx):
    mr = _mr()
    property_id = _ids_propiedad(a.get("propiedad"))
    if not _puede_dinero(property_id, ctx):
        raise ErrorHerramienta("Sin acceso a datos económicos de esta propiedad con este usuario")
    try:
        informe = mr._informe_financiero(property_id, str(a.get("mes") or ""))
    except mr.InformeError as e:
        raise ErrorHerramienta(str(e))
    informe.pop("reservas_detalle", None)
    informe.pop("ok", None)
    informe["nota"] = "La rentabilidad usa costes fijos y de limpieza ESTIMADOS (no son facturas reales)."
    return informe


HERRAMIENTAS = {"listar_reservas": _t_listar_reservas, "resumen": _t_resumen, "grafico": _t_grafico, "finanzas_mes": _t_finanzas_mes}

_PROP = {"type": "string", "enum": ["hostal", "primavera"], "description": "hostal = Hostal ALC Homes San Blas; primavera = La Casa de la Primavera"}
_FECHA = {"type": "string", "description": "Fecha ISO YYYY-MM-DD"}
_AGRUPAR = {"type": "string", "enum": ["canal", "habitacion", "tipo_habitacion", "mes", "total"]}

TOOLS = [
    {"type": "function", "function": {
        "name": "listar_reservas",
        "description": "Lista reservas concretas (la última de un canal, quién se alojó en una habitación, próximas llegadas…). Filtra por fecha de ENTRADA. Por defecto las más recientes primero.",
        "parameters": {"type": "object", "properties": {
            "propiedad": _PROP, "desde": _FECHA, "hasta": _FECHA,
            "canal": {"type": "string", "description": "Texto contenido en el canal, p. ej. booking, airbnb, trip, directo"},
            "habitacion": {"type": "string", "description": "Texto contenido en el nombre o tipo de habitación"},
            "huesped": {"type": "string", "description": "Texto contenido en el nombre del huésped"},
            "momento": {"type": "string", "enum": ["todas", "pasadas", "en_curso", "futuras"]},
            "orden": {"type": "string", "enum": ["recientes", "antiguas"]},
            "limite": {"type": "integer", "description": "Máximo 25, por defecto 10"}},
            "required": ["propiedad"]}}},
    {"type": "function", "function": {
        "name": "resumen",
        "description": "Cifras de un periodo: nº de reservas, noches, ocupación (%), noches libres e ingresos brutos/comisiones/netos, agrupadas por canal, habitación, tipo de habitación, mes o en total.",
        "parameters": {"type": "object", "properties": {
            "propiedad": _PROP, "desde": _FECHA, "hasta": _FECHA, "agrupar_por": _AGRUPAR},
            "required": ["propiedad", "desde", "hasta", "agrupar_por"]}}},
    {"type": "function", "function": {
        "name": "grafico",
        "description": "Dibuja un gráfico en la app calculando él mismo los datos (no pases números). tarta = proporciones; barras = comparar grupos; linea = evolución por mes.",
        "parameters": {"type": "object", "properties": {
            "tipo": {"type": "string", "enum": ["tarta", "barras", "linea"]},
            "titulo": {"type": "string", "description": "Título corto del gráfico"},
            "propiedad": _PROP, "desde": _FECHA, "hasta": _FECHA,
            "agrupar_por": {"type": "string", "enum": ["canal", "habitacion", "tipo_habitacion", "mes"]},
            "metrica": {"type": "string", "enum": list(METRICAS)}},
            "required": ["tipo", "titulo", "propiedad", "desde", "hasta", "agrupar_por", "metrica"]}}},
    {"type": "function", "function": {
        "name": "finanzas_mes",
        "description": "Informe financiero de un mes (el de la pestaña Finanzas): ingresos, comisiones, ocupación y rentabilidad estimada (beneficio tras costes e impuestos).",
        "parameters": {"type": "object", "properties": {"propiedad": _PROP, "mes": {"type": "string", "description": "YYYY-MM"}},
                       "required": ["propiedad", "mes"]}}},
]


def ejecutar_herramienta(nombre, argumentos, ctx):
    """Ejecuta una herramienta y devuelve SIEMPRE un dict serializable (los errores, como {"error": …})."""
    funcion = HERRAMIENTAS.get(nombre)
    if funcion is None:
        return {"error": f"Herramienta desconocida: {nombre}"}
    try:
        args = json.loads(argumentos) if isinstance(argumentos, str) else (argumentos or {})
        if not isinstance(args, dict):
            raise ValueError("argumentos no es un objeto")
    except ValueError:
        return {"error": "Los argumentos no son un JSON válido"}
    try:
        return funcion(args, ctx)
    except ErrorHerramienta as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"[asistente] Error en la herramienta {nombre}: {e}")
        return {"error": "No se pudieron consultar los datos ahora mismo (Beds24 no ha respondido). Vuelve a intentarlo en un momento."}


# ── Modelo ──────────────────────────────────────────────────────────────────

def _system(ctx):
    mr = _mr()
    hoy = hoy_madrid()
    if ctx.get("es_admin"):
        permisos = "Este usuario tiene acceso a los datos económicos de las dos propiedades."
    elif ctx.get("finanzas"):
        permisos = ("Este usuario tiene acceso a los datos económicos de La Casa de la Primavera, pero NO a los del Hostal "
                    "(precios, ingresos, comisiones): no los menciones; si los pide, di que requieren el PIN de administrador.")
    else:
        permisos = ("Este usuario NO tiene los datos económicos desbloqueados (precios, ingresos, comisiones, beneficio): no los menciones ni "
                    "los consultes; si los pide, di que primero debe desbloquear la pestaña Finanzas. Sí puedes dar reservas, ocupación y fechas.")
    hostal = ", ".join(h["nombre"] for h in _habitaciones(mr.PROPERTY_ID))
    return f"""Eres el asistente de la app de gestión de ALC Homes. Ayudas al personal y a la propiedad a entender sus reservas, ocupación y finanzas, y puedes dibujar gráficos. Hoy es {DIAS_SEMANA[hoy.weekday()]} {hoy.isoformat()}.

Propiedades: «hostal» (Hostal ALC Homes San Blas, habitaciones: {hostal}) y «primavera» (La Casa de la Primavera, una vivienda completa que se alquila entera).

Reglas:
1. Todas las cifras, fechas y nombres salen de las herramientas: nunca calcules de cabeza ni inventes. Si no hay datos, dilo.
2. Si la pregunta es ambigua en algo que cambia el resultado (propiedad, periodo, bruto o neto, qué quiere ver exactamente), haz UNA pregunta corta con 2-4 opciones antes de consultar. Si hay una suposición razonable (p. ej. el mes en curso), úsala y dila en una frase. No preguntes por cosas que ya sabes.
3. Para gráficos usa la herramienta grafico: tarta para proporciones, barras para comparar, linea para evolución por mes. Después añade 1-2 frases con lo más llamativo (no repitas todos los números). Máximo 2 gráficos por respuesta.
4. Ingresos brutos = precio de las reservas; netos = brutos menos la comisión del canal. Las noches y los ingresos de una estancia que cruza varios meses se reparten por noches. La rentabilidad de finanzas_mes usa costes estimados: avísalo.
5. Responde en español, breve y directo, en texto plano (sin markdown ni tablas; para listas usa «•»). Importes en euros, con separador de miles y sin decimales salvo que importen.
6. Solo lees datos: no puedes crear, modificar ni cancelar reservas, ni cambiar precios ni bloqueos; si te lo piden, indica que lo hagan desde las pantallas de la app.
7. No tienes teléfonos ni correos de huéspedes.
{permisos}"""


def _limpiar_mensajes(mensajes):
    limpios = []
    for m in (mensajes or [])[-MAX_MENSAJES:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip():
            limpios.append({"role": m["role"], "content": m["content"][:MAX_CHARS_MENSAJE]})
    while limpios and limpios[0]["role"] != "user":
        limpios.pop(0)
    return limpios


class _Agotado(Exception):
    pass


def _llamar_groq(messages, deadline, con_herramientas):
    """Una llamada al modelo principal; si está saturado (429/503/413) usa el de respaldo, y si genera
    una llamada a herramienta mal formada (400 tool_use_failed) reintenta una vez."""
    resp = None
    for modelo in (GROQ_MODEL_PRI, GROQ_MODEL_FALL):
        for _intento in range(2):
            restante = deadline - time.time()
            if restante < 5:
                raise _Agotado()
            cuerpo = {"model": modelo, "messages": messages, "max_tokens": 1200, "temperature": 0.2}
            if con_herramientas:
                cuerpo["tools"] = TOOLS
                cuerpo["tool_choice"] = "auto"
            resp = requests.post(GROQ_API_URL, headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                                 json=cuerpo, timeout=min(30, restante))
            if _herramienta_mal_formada(resp):
                logger.warning(f"[asistente] {modelo} generó una llamada a herramienta mal formada, se reintenta")
                continue
            break
        if resp.status_code not in (413, 429, 503) and not _herramienta_mal_formada(resp):
            return resp
    return resp


def _herramienta_mal_formada(resp):
    return resp.status_code == 400 and "tool_use_failed" in resp.text


def responder(mensajes, ctx):
    """
    Conversa con el modelo hasta tener la respuesta final, ejecutando las herramientas que pida.
    ctx = {"es_admin": bool, "finanzas": bool}. Devuelve (dict, status HTTP):
    {"ok": True, "respuesta": str, "graficos": [...]} o {"ok": False, "error": str}.
    """
    historial = _limpiar_mensajes(mensajes)
    if not historial or historial[-1]["role"] != "user":
        return {"ok": False, "error": "Falta la pregunta"}, 400
    ctx = {"es_admin": bool(ctx.get("es_admin")), "finanzas": bool(ctx.get("finanzas")), "graficos": []}
    messages = [{"role": "system", "content": _system(ctx)}] + historial
    deadline = time.time() + PRESUPUESTO_SEG
    try:
        for paso in range(MAX_PASOS):
            ultima_ronda = paso == MAX_PASOS - 1
            resp = _llamar_groq(messages, deadline, con_herramientas=not ultima_ronda)
            if resp.status_code != 200:
                logger.error(f"[asistente] Groq {resp.status_code}: {resp.text[:300]}")
                return {"ok": False, "error": f"Error consultando el asistente: {resp.status_code}. Inténtalo de nuevo en un minuto."}, 500
            mensaje = resp.json()["choices"][0]["message"]
            llamadas = mensaje.get("tool_calls") or []
            if not llamadas:
                texto = (mensaje.get("content") or "").strip()
                if not texto and ctx["graficos"]:
                    texto = "Aquí tienes el gráfico."
                if not texto:
                    return {"ok": False, "error": "El asistente no ha dado una respuesta. Prueba a reformular la pregunta."}, 500
                return {"ok": True, "respuesta": texto, "graficos": ctx["graficos"]}, 200
            messages.append({"role": "assistant", "content": mensaje.get("content") or "", "tool_calls": llamadas})
            for llamada in llamadas:
                funcion = llamada.get("function") or {}
                resultado = ejecutar_herramienta(funcion.get("name"), funcion.get("arguments"), ctx)
                messages.append({"role": "tool", "tool_call_id": llamada.get("id"), "content": json.dumps(resultado, ensure_ascii=False)})
    except _Agotado:
        return {"ok": False, "error": "La consulta está tardando demasiado. Prueba con una pregunta más concreta."}, 504
    except requests.RequestException as e:
        logger.error(f"[asistente] Error de red con Groq: {e}")
        return {"ok": False, "error": "No se pudo conectar con el asistente. Inténtalo de nuevo."}, 502
    return {"ok": False, "error": "No he podido completar la consulta. Prueba a reformular la pregunta."}, 500

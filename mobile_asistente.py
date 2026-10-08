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
  - comparar: dos periodos (p. ej. este mes y el mismo del año pasado) con las diferencias.
  - cancelaciones: reservas canceladas por canal, tasa de cancelación e importe perdido.
  - agenda: llegadas, salidas y alojados de un día, con el estado del parte de viajeros.
  - partes: estado del parte de viajeros (RPV) de las próximas llegadas.
  - disponibilidad: noches libres, ocupadas y bloqueadas con el precio de calendario.
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
from datetime import date, datetime, timedelta

import requests

from config import GROQ_API_KEY, GROQ_API_URL, GROQ_MODEL_FALL, GROQ_MODEL_PRI
from services.fechas import hoy_madrid
from services.resumen import texto_estado_parte
from services.rpv import VENTANA_DIAS as VENTANA_RPV_DIAS, obtener_estado_partes

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
AGRUPACIONES = ("canal", "habitacion", "tipo_habitacion", "mes", "pais", "total")
METRICAS_DINERO = ("ingresos_brutos", "ingresos_netos", "comisiones", "precio_medio_noche", "revpar")
METRICAS_SUMABLES = ("ingresos_brutos", "ingresos_netos", "comisiones", "reservas", "noches")   # las únicas que valen en una tarta
METRICAS = METRICAS_DINERO + ("reservas", "noches", "ocupacion_pct", "estancia_media", "antelacion_media_dias")
UNIDADES = {"ingresos_brutos": "eur", "ingresos_netos": "eur", "comisiones": "eur", "precio_medio_noche": "eur", "revpar": "eur",
            "reservas": "reservas", "noches": "noches", "ocupacion_pct": "%", "estancia_media": "noches", "antelacion_media_dias": "días"}
MAX_DISPONIBILIDAD_DIAS = 120
MAX_AGENDA = 30
MAX_PARTES = 40
MAX_TRAMOS_LIBRES = 6
MAX_SECTORES = 10         # grupos máximos en un gráfico; el resto va a «Otros»

PAISES = {"ES": "España", "FR": "Francia", "DE": "Alemania", "GB": "Reino Unido", "IT": "Italia", "PT": "Portugal", "NL": "Países Bajos",
          "BE": "Bélgica", "US": "Estados Unidos", "IE": "Irlanda", "CH": "Suiza", "AT": "Austria", "PL": "Polonia", "SE": "Suecia",
          "NO": "Noruega", "DK": "Dinamarca", "FI": "Finlandia", "CZ": "Chequia", "RU": "Rusia", "UA": "Ucrania", "RO": "Rumanía",
          "MX": "México", "AR": "Argentina", "CO": "Colombia", "BR": "Brasil", "CL": "Chile", "PE": "Perú", "CA": "Canadá",
          "AU": "Australia", "CN": "China", "JP": "Japón", "KR": "Corea del Sur", "IN": "India", "MA": "Marruecos", "DZ": "Argelia",
          "TR": "Turquía", "IL": "Israel", "HU": "Hungría", "GR": "Grecia", "LT": "Lituania", "LV": "Letonia", "EE": "Estonia"}

_cache = {}   # property_id -> {"t": epoch, "reservas": [...], "canceladas": [...], "incompleto": bool}


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


def _datos(property_id):
    """Reservas de la propiedad (copia en memoria de 10 min): {"reservas": [...], "canceladas": [...], "incompleto": bool}.
    Reserva: {id, checkin, checkout, noches, room_id, habitacion, tipo, canal, huesped, pais, creada, antelacion, bruto, comision};
    las canceladas llevan {id, checkin, canal, bruto}. Los bloqueos de calendario no cuentan como reservas."""
    ahora = time.time()
    c = _cache.get(property_id)
    if c and ahora - c["t"] < (TTL_CACHE_INCOMPLETA if c["incompleto"] else TTL_CACHE):
        return c

    mr = _mr()
    hoy = hoy_madrid()
    raw, fallidos = mr._fetch_bookings_finance(property_id, hoy - timedelta(days=DIAS_ATRAS), hoy + timedelta(days=DIAS_ADELANTE))
    nombres = {h["room_id"]: h for h in _habitaciones(property_id)}
    reservas, canceladas = [], []
    for b in raw:
        if mr._finance_es_bloqueo(b):
            continue
        try:
            checkin = date.fromisoformat((b.get("arrival") or "")[:10])
            checkout = date.fromisoformat((b.get("departure") or "")[:10])
        except Exception:
            continue
        noches = (checkout - checkin).days
        if noches <= 0:
            continue
        canal = mr._finance_channel_label(b)
        bruto = float(b.get("price") or 0)
        if str(b.get("status", "")).lower() == "cancelled":
            canceladas.append({"id": b.get("id"), "checkin": checkin, "canal": canal, "bruto": bruto})
            continue
        room_id = str(b.get("roomId") or "")
        comision = float(b.get("commission") or 0)
        if comision <= 0:   # igual que Finanzas: sin comisión real en Beds24 se estima por portal
            comision = bruto * mr._finance_comision_fallback_pct(canal)
        guest = b.get("guest") or {}
        huesped = f"{guest.get('firstName') or b.get('firstName') or ''} {guest.get('lastName') or b.get('lastName') or ''}".strip()
        hab = nombres.get(room_id) or {"nombre": "Desconocida", "tipo": "Desconocido"}
        creada = b.get("bookingTime")
        reservas.append({"id": b.get("id"), "checkin": checkin, "checkout": checkout, "noches": noches, "room_id": room_id,
                         "habitacion": hab["nombre"], "tipo": hab["tipo"], "canal": canal, "huesped": huesped or "Desconocido",
                         "pais": _pais(guest.get("country") or b.get("country")), "creada": creada, "antelacion": _antelacion(creada, checkin),
                         "bruto": bruto, "comision": comision})
    _cache[property_id] = {"t": ahora, "reservas": reservas, "canceladas": canceladas, "incompleto": fallidos > 0}
    return _cache[property_id]


def _reservas(property_id):
    """(reservas, incompleto) de la propiedad."""
    d = _datos(property_id)
    return d["reservas"], d["incompleto"]


def _pais(codigo):
    c = str(codigo or "").strip()
    if not c:
        return "Desconocido"
    return PAISES.get(c.upper(), c.upper() if len(c) == 2 else c)


def _antelacion(creada, checkin):
    """Días entre que se hizo la reserva y la entrada; None si Beds24 no da la fecha o no se entiende."""
    try:
        dias = (checkin - datetime.fromisoformat(str(creada).replace("Z", "+00:00")).date()).days
        return dias if dias >= 0 else None
    except Exception:
        return None


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

    def acumulador(disponibles):
        return {"ids": set(), "noches": 0, "bruto": 0.0, "comision": 0.0, "disponibles": disponibles, "antelaciones": {}}

    def grupo(clave, disponibles):
        return grupos.setdefault(clave, acumulador(disponibles))

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

    total = acumulador(len(habitaciones) * dias)
    for r in reservas:
        ini, fin = max(r["checkin"], desde), min(r["checkout"], fin_excl)
        if (fin - ini).days <= 0:
            continue
        # tramos (clave, noches) en los que cae esta reserva dentro del rango
        if agrupar_por == "mes":
            tramos = [(clave, (min(fin, f + timedelta(days=1)) - max(ini, i)).days) for clave, i, f in _meses(desde, hasta)
                      if min(fin, f + timedelta(days=1)) > max(ini, i)]
        else:
            clave = {"canal": r["canal"], "habitacion": r["habitacion"], "tipo_habitacion": r["tipo"], "pais": r["pais"], "total": "Total"}[agrupar_por]
            tramos = [(clave, (fin - ini).days)]
        for clave, n in tramos:
            frac = n / r["noches"]
            for acc in (grupo(clave, 0), total):
                acc["ids"].add(r["id"])
                acc["noches"] += n
                acc["bruto"] += r["bruto"] * frac
                acc["comision"] += r["comision"] * frac
                if r["antelacion"] is not None:
                    acc["antelaciones"][r["id"]] = r["antelacion"]

    def volcar(clave, acc, con_ocupacion):
        n_reservas = len(acc["ids"])
        fila = {"grupo": clave, "reservas": n_reservas, "noches": acc["noches"],
                "estancia_media": round(acc["noches"] / n_reservas, 1) if n_reservas else 0.0}
        if acc["antelaciones"]:
            fila["antelacion_media_dias"] = round(sum(acc["antelaciones"].values()) / len(acc["antelaciones"]), 1)
        if con_ocupacion:
            fila["ocupacion_pct"] = round(100 * acc["noches"] / acc["disponibles"], 1) if acc["disponibles"] else 0.0
            fila["noches_libres"] = acc["disponibles"] - acc["noches"]
        if dinero:
            fila["ingresos_brutos"] = round(acc["bruto"], 2)
            fila["comisiones"] = round(acc["comision"], 2)
            fila["ingresos_netos"] = round(acc["bruto"] - acc["comision"], 2)
            fila["precio_medio_noche"] = round(acc["bruto"] / acc["noches"], 2) if acc["noches"] else 0.0
            if con_ocupacion:   # RevPAR: ingresos por noche disponible (ocupación × precio medio)
                fila["revpar"] = round(acc["bruto"] / acc["disponibles"], 2) if acc["disponibles"] else 0.0
        return fila

    con_ocupacion = agrupar_por in ("habitacion", "tipo_habitacion", "mes", "total")
    filas = [volcar(k, g, con_ocupacion) for k, g in grupos.items()]
    if agrupar_por != "mes":
        filas.sort(key=lambda f: -(f.get("ingresos_brutos", 0) if dinero else f["noches"]))
    return filas, volcar("Total", total, True), incompleto


def _t_resumen(a, ctx):
    property_id = _ids_propiedad(a.get("propiedad"))
    desde, hasta = _rango(a, obligatorio=True)
    dinero = _puede_dinero(property_id, ctx)
    filas, totales, incompleto = _calcular_grupos(property_id, desde, hasta, a.get("agrupar_por") or "total", dinero)
    resultado = {"propiedad": a["propiedad"], "desde": desde.isoformat(), "hasta": hasta.isoformat(), "agrupado_por": a.get("agrupar_por") or "total",
                 "grupos": filas, "totales": totales,
                 "nota": "Noches e ingresos repartidos proporcionalmente por noche dentro del periodo. Ingresos brutos = precio de la reserva; netos = brutos menos comisión del canal. "
                         "precio_medio_noche = brutos/noches; revpar = brutos/noches disponibles."}
    if not dinero:
        resultado["nota"] += " Sin acceso a datos económicos: no se incluyen importes."
    if avisos := _avisos(incompleto, desde):
        resultado["avisos"] = avisos
    return resultado


def _recortar(filas, metrica, tipo):
    """Más de MAX_SECTORES grupos no se leen en un gráfico: se dejan los mayores y, si la métrica suma
    (reservas, noches, importes), el resto va a «Otros»."""
    if tipo == "linea" or len(filas) <= MAX_SECTORES:
        return filas
    ordenadas = sorted(filas, key=lambda f: -f[metrica])
    cabeza, cola = ordenadas[:MAX_SECTORES - 1], ordenadas[MAX_SECTORES - 1:]
    if metrica in METRICAS_SUMABLES:
        return cabeza + [{"grupo": "Otros", metrica: round(sum(f[metrica] for f in cola), 2)}]
    return ordenadas[:MAX_SECTORES]


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
        raise ErrorHerramienta("agrupar_por debe ser canal, habitacion, tipo_habitacion, mes o pais")
    if metrica in ("ocupacion_pct", "revpar") and agrupar_por in ("canal", "pais"):
        raise ErrorHerramienta("La ocupación y el RevPAR no se pueden agrupar por canal ni país: usa habitacion, tipo_habitacion o mes")
    if tipo == "tarta" and metrica not in METRICAS_SUMABLES:
        raise ErrorHerramienta(f"{metrica} no suma 100%: usa barras o línea")
    if tipo == "linea" and agrupar_por != "mes":
        raise ErrorHerramienta("El gráfico de línea solo sirve para agrupar_por=mes")
    property_id = _ids_propiedad(a.get("propiedad"))
    desde, hasta = _rango(a, obligatorio=True)
    dinero = _puede_dinero(property_id, ctx)
    if metrica in METRICAS_DINERO and not dinero:
        raise ErrorHerramienta("Sin acceso a datos económicos de esta propiedad con este usuario")
    filas, _totales, incompleto = _calcular_grupos(property_id, desde, hasta, agrupar_por, dinero)
    filas = [f for f in filas if metrica in f and f[metrica] is not None]   # p. ej. grupos sin fecha de creación no tienen antelación
    if tipo == "tarta":
        filas = [f for f in filas if f[metrica] > 0]
    if not filas:
        raise ErrorHerramienta("No hay datos que dibujar en ese periodo")
    filas = _recortar(filas, metrica, tipo)
    grafico = {"tipo": tipo, "titulo": str(a.get("titulo") or "")[:90], "unidad": UNIDADES[metrica],
               "etiquetas": [f["grupo"] for f in filas], "valores": [f[metrica] for f in filas]}
    ctx["graficos"].append(grafico)
    resultado = {"ok": True, "grafico_mostrado_al_usuario": True, "datos": [{"etiqueta": e, "valor": v} for e, v in zip(grafico["etiquetas"], grafico["valores"])]}
    if avisos := _avisos(incompleto, desde):
        resultado["avisos"] = avisos
    return resultado


METRICAS_COMPARAR = ("reservas", "noches", "estancia_media", "ocupacion_pct", "ingresos_brutos", "ingresos_netos", "comisiones", "precio_medio_noche", "revpar")


def _variacion(x, y):
    return {"a": x, "b": y, "diferencia": round(x - y, 2), "variacion_pct": round(100 * (x - y) / y, 1) if y else None}


def _t_comparar(a, ctx):
    property_id = _ids_propiedad(a.get("propiedad"))
    periodo_a = _rango({"desde": a.get("desde_a"), "hasta": a.get("hasta_a")}, obligatorio=True)
    periodo_b = _rango({"desde": a.get("desde_b"), "hasta": a.get("hasta_b")}, obligatorio=True)
    agrupar_por = a.get("agrupar_por") or "total"
    if agrupar_por == "mes":
        raise ErrorHerramienta("comparar no admite agrupar_por=mes: usa resumen por mes para cada periodo")
    dinero = _puede_dinero(property_id, ctx)
    filas_a, tot_a, inc_a = _calcular_grupos(property_id, *periodo_a, agrupar_por, dinero)
    filas_b, tot_b, inc_b = _calcular_grupos(property_id, *periodo_b, agrupar_por, dinero)
    resultado = {"periodo_a": {"desde": periodo_a[0].isoformat(), "hasta": periodo_a[1].isoformat(), "dias": (periodo_a[1] - periodo_a[0]).days + 1},
                 "periodo_b": {"desde": periodo_b[0].isoformat(), "hasta": periodo_b[1].isoformat(), "dias": (periodo_b[1] - periodo_b[0]).days + 1},
                 "totales": {m: _variacion(tot_a[m], tot_b[m]) for m in METRICAS_COMPARAR if m in tot_a},
                 "nota": "variacion_pct = (a - b) / b. a es el periodo principal y b el de comparación."}
    if resultado["periodo_a"]["dias"] != resultado["periodo_b"]["dias"]:
        resultado["nota"] += " Los periodos tienen distinta duración: compara ocupación, medias y precios, no los totales."
    if agrupar_por != "total":
        por_a, por_b = {f["grupo"]: f for f in filas_a}, {f["grupo"]: f for f in filas_b}
        vacio = {"reservas": 0, "noches": 0, "ingresos_brutos": 0.0}
        resultado["grupos"] = [{"grupo": g, **{m: _variacion(por_a.get(g, vacio).get(m, 0), por_b.get(g, vacio).get(m, 0))
                                               for m in ("reservas", "noches", "ingresos_brutos") if m in tot_a}}
                               for g in list(por_a) + [g for g in por_b if g not in por_a]]
    if not dinero:
        resultado["nota"] += " Sin acceso a datos económicos: no se incluyen importes."
    if avisos := _avisos(inc_a or inc_b, min(periodo_a[0], periodo_b[0])):
        resultado["avisos"] = avisos
    return resultado


def _t_cancelaciones(a, ctx):
    property_id = _ids_propiedad(a.get("propiedad"))
    desde, hasta = _rango(a, obligatorio=True)
    dinero = _puede_dinero(property_id, ctx)
    datos = _datos(property_id)
    por_canal = {}
    for lista, clave in ((datos["reservas"], "confirmadas"), (datos["canceladas"], "canceladas")):
        for r in lista:
            if desde <= r["checkin"] <= hasta:
                c = por_canal.setdefault(r["canal"], {"confirmadas": 0, "canceladas": 0, "importe_perdido": 0.0})
                c[clave] += 1
                if clave == "canceladas":
                    c["importe_perdido"] += r["bruto"]

    def fila(nombre, c):
        n = c["confirmadas"] + c["canceladas"]
        f = {"grupo": nombre, "confirmadas": c["confirmadas"], "canceladas": c["canceladas"], "tasa_cancelacion_pct": round(100 * c["canceladas"] / n, 1) if n else 0.0}
        if dinero:
            f["importe_cancelado_eur"] = round(c["importe_perdido"], 2)
        return f

    total = {"confirmadas": sum(c["confirmadas"] for c in por_canal.values()), "canceladas": sum(c["canceladas"] for c in por_canal.values()),
             "importe_perdido": sum(c["importe_perdido"] for c in por_canal.values())}
    resultado = {"propiedad": a["propiedad"], "desde": desde.isoformat(), "hasta": hasta.isoformat(),
                 "por_canal": sorted((fila(k, v) for k, v in por_canal.items()), key=lambda f: -f["canceladas"]), "total": fila("Total", total),
                 "nota": "Reservas por fecha de ENTRADA. Solo cuentan las canceladas que Beds24 conserva con estado cancelado."}
    if avisos := _avisos(datos["incompleto"], desde):
        resultado["avisos"] = avisos
    return resultado


# ── Partes de viajeros (RPV) y agenda ────────────────────────────────────────

def _estados_rpv():
    """(estados, sin_verificar) de RPV; si RPV falla, sin_verificar es None (= todo sin verificar)."""
    try:
        return obtener_estado_partes(max_age=60)
    except Exception as e:
        logger.error(f"[asistente] No se pudo consultar RPV: {e}")
        return {}, None


def _parte(r, fecha, estados, sin_verificar, hoy):
    """Texto del estado del parte de una reserva con entrada en `fecha` (mismas reglas que los resúmenes de WhatsApp)."""
    if fecha < hoy:
        return "sin dato (RPV solo informa de las entradas de hoy en adelante)"
    if fecha > hoy + timedelta(days=VENTANA_RPV_DIAS - 1):
        return f"fuera de la ventana de consulta de RPV ({VENTANA_RPV_DIAS} días)"
    if sin_verificar is None:
        return "sin verificar (RPV no responde)"
    texto = texto_estado_parte({"room_id": r["room_id"], "creada": r["creada"]}, fecha.isoformat(), estados, sin_verificar)
    return texto.split(" ", 1)[1] if " " in texto else texto   # quita el emoji del principio


def _propiedades(a):
    return [a["propiedad"]] if a.get("propiedad") else ["hostal", "primavera"]


def _t_agenda(a, ctx):
    hoy = hoy_madrid()
    fecha = _fecha(a["fecha"], "fecha") if a.get("fecha") else hoy
    estados, sin_verificar = _estados_rpv()
    resultado = {"fecha": fecha.isoformat()}
    avisos = []
    for clave in _propiedades(a):
        property_id = _ids_propiedad(clave)
        reservas, incompleto = _reservas(property_id)
        if incompleto:
            avisos.append("Beds24 no respondió para algún tramo de fechas: la lista puede estar incompleta")
        llegadas = sorted((r for r in reservas if r["checkin"] == fecha), key=lambda r: r["habitacion"])
        salidas = sorted((r for r in reservas if r["checkout"] == fecha), key=lambda r: r["habitacion"])
        se_quedan = sorted((r for r in reservas if r["checkin"] < fecha < r["checkout"]), key=lambda r: r["habitacion"])
        resultado[clave] = {
            "llegadas": [{"habitacion": r["habitacion"], "huesped": r["huesped"], "canal": r["canal"], "noches": r["noches"],
                          "sale": r["checkout"].isoformat(), "parte": _parte(r, fecha, estados, sin_verificar, hoy)} for r in llegadas[:MAX_AGENDA]],
            "salidas": [{"habitacion": r["habitacion"], "huesped": r["huesped"], "canal": r["canal"]} for r in salidas[:MAX_AGENDA]],
            "se_quedan": [{"habitacion": r["habitacion"], "huesped": r["huesped"], "sale": r["checkout"].isoformat()} for r in se_quedan[:MAX_AGENDA]],
            "habitaciones_ocupadas_esa_noche": len(llegadas) + len(se_quedan),
            "habitaciones_en_total": len(_habitaciones(property_id)),
        }
    if avisos:
        resultado["avisos"] = sorted(set(avisos))
    return resultado


def _t_partes(a, ctx):
    hoy = hoy_madrid()
    ultimo = hoy + timedelta(days=VENTANA_RPV_DIAS - 1)
    desde = max(_fecha(a["desde"], "desde") if a.get("desde") else hoy, hoy)
    hasta = min(_fecha(a["hasta"], "hasta") if a.get("hasta") else ultimo, ultimo)
    if hasta < desde:
        raise ErrorHerramienta(f"RPV solo informa de las entradas de hoy a dentro de {VENTANA_RPV_DIAS} días")
    estados, sin_verificar = _estados_rpv()
    filas, recuento, avisos = [], {}, []
    for clave in _propiedades(a):
        reservas, incompleto = _reservas(_ids_propiedad(clave))
        if incompleto:
            avisos.append("Beds24 no respondió para algún tramo de fechas: la lista puede estar incompleta")
        for r in reservas:
            if desde <= r["checkin"] <= hasta:
                parte = _parte(r, r["checkin"], estados, sin_verificar, hoy)
                recuento[parte] = recuento.get(parte, 0) + 1
                if not (a.get("solo_pendientes") and parte.startswith("parte recibido")):
                    filas.append({"propiedad": clave, "llegada": r["checkin"].isoformat(), "habitacion": r["habitacion"],
                                  "huesped": r["huesped"], "canal": r["canal"], "parte": parte})
    filas.sort(key=lambda f: (f["llegada"], f["habitacion"]))
    resultado = {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "recuento": recuento, "total_llegadas": sum(recuento.values()),
                 "mostrando": min(len(filas), MAX_PARTES), "llegadas": filas[:MAX_PARTES]}
    if avisos:
        resultado["avisos"] = sorted(set(avisos))
    return resultado


# ── Disponibilidad y precios de calendario ───────────────────────────────────

def _es_bloqueo_calendario(b):
    """Reserva de la app móvil que en realidad bloquea fechas (no es un huésped)."""
    email = str(b.get("email") or "").strip().lower()
    nombre = str(b.get("guestName") or "").strip().lower()
    return email == "bloqueo@bloqueo.com" or nombre.startswith(("blocked", "fecha bloqueada"))


def _rangos(dias):
    """Agrupa fechas ordenadas en tramos consecutivos: [{desde, hasta_noche, noches}]."""
    tramos = []
    for d in dias:
        if tramos and d == tramos[-1]["fin"] + timedelta(days=1):
            tramos[-1]["fin"] = d
        else:
            tramos.append({"ini": d, "fin": d})
    return [{"desde": t["ini"].isoformat(), "ultima_noche": t["fin"].isoformat(), "noches": (t["fin"] - t["ini"]).days + 1} for t in tramos]


def _t_disponibilidad(a, ctx):
    mr = _mr()
    property_id = _ids_propiedad(a.get("propiedad"))
    desde, hasta = _rango(a, obligatorio=True)
    if (hasta - desde).days + 1 > MAX_DISPONIBILIDAD_DIAS:
        raise ErrorHerramienta(f"El rango máximo para disponibilidad es de {MAX_DISPONIBILIDAD_DIAS} días")
    if not mr._ensure_loaded():
        raise ErrorHerramienta("No se pudo cargar el calendario de Beds24 ahora mismo. Vuelve a intentarlo en un momento.")
    filtro = _norm(a.get("habitacion"))
    habitaciones = [h for h in _habitaciones(property_id) if not filtro or filtro in _norm(h["nombre"]) or filtro in _norm(h["tipo"])]
    if not habitaciones:
        raise ErrorHerramienta("Ninguna habitación coincide con ese nombre")
    estado = mr._state
    overrides, bloqueos_vivos, precios_vivos = estado.get("overrides") or {}, estado.get("live_blocked") or {}, estado.get("live_prices") or {}
    dias = [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]
    resultado_hab, libres_total = [], 0
    for h in habitaciones:
        rid = h["room_id"]
        ocupadas, bloqueadas = set(), set()
        for b in estado.get("bookings") or []:
            if str(b.get("roomId")) != rid or str(b.get("status", "")).lower() == "cancelled":
                continue
            try:
                ini, fin = date.fromisoformat(b["arrival"]), date.fromisoformat(b["departure"])
            except Exception:
                continue
            destino = bloqueadas if _es_bloqueo_calendario(b) else ocupadas
            destino.update(ini + timedelta(days=i) for i in range((fin - ini).days))
        libres, precios, n_ocupadas, n_bloqueadas = [], [], 0, 0
        for d in dias:   # cada noche cuenta una sola vez: ocupada (reserva), bloqueada o libre
            ds = d.isoformat()
            ov = (overrides.get(rid) or {}).get(ds) or {}
            if d in ocupadas:
                n_ocupadas += 1
            elif d in bloqueadas or ov.get("numAvail") == 0 or ("numAvail" not in ov and (bloqueos_vivos.get(rid) or {}).get(ds)):
                n_bloqueadas += 1
            else:
                libres.append(d)
                precio = ov.get("price1") or (precios_vivos.get(rid) or {}).get(ds) or (mr.BASE_PRICES.get(int(rid)) or mr.BASE_PRICES.get(rid) or {}).get(ds)
                if precio:
                    precios.append(float(precio))
        libres_total += len(libres)
        resultado_hab.append({
            "habitacion": h["nombre"], "noches_libres": len(libres), "noches_ocupadas": n_ocupadas, "noches_bloqueadas": n_bloqueadas,
            "precio_calendario_noches_libres": {"minimo": min(precios), "maximo": max(precios), "medio": round(sum(precios) / len(precios), 2)} if precios else None,
            "tramos_libres": _rangos(libres)[:MAX_TRAMOS_LIBRES]})
    return {"propiedad": a["propiedad"], "desde": desde.isoformat(), "hasta": hasta.isoformat(), "noches_libres_total": libres_total, "habitaciones": resultado_hab,
            "nota": "Los precios son los del calendario de venta (tarifa publicada), no lo que ha pagado cada huésped."}


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


HERRAMIENTAS = {"listar_reservas": _t_listar_reservas, "resumen": _t_resumen, "grafico": _t_grafico, "comparar": _t_comparar,
                "cancelaciones": _t_cancelaciones, "agenda": _t_agenda, "partes": _t_partes, "disponibilidad": _t_disponibilidad,
                "finanzas_mes": _t_finanzas_mes}

_PROP = {"type": "string", "enum": ["hostal", "primavera"], "description": "hostal = Hostal ALC Homes San Blas; primavera = La Casa de la Primavera"}
_PROP_OPC = {"type": "string", "enum": ["hostal", "primavera"], "description": "Si se omite, las dos propiedades"}
_FECHA = {"type": "string", "description": "Fecha ISO YYYY-MM-DD"}
_AGRUPAR = {"type": "string", "enum": list(AGRUPACIONES)}


def _herramienta(nombre, descripcion, propiedades, obligatorios):
    return {"type": "function", "function": {"name": nombre, "description": descripcion,
                                              "parameters": {"type": "object", "properties": propiedades, "required": obligatorios}}}


TOOLS = [
    _herramienta(
        "listar_reservas",
        "Lista reservas concretas (la última de un canal, quién se alojó en una habitación, próximas llegadas…). Filtra por fecha de ENTRADA. Por defecto las más recientes primero.",
        {"propiedad": _PROP, "desde": _FECHA, "hasta": _FECHA,
         "canal": {"type": "string", "description": "Texto contenido en el canal, p. ej. booking, airbnb, trip, directo"},
         "habitacion": {"type": "string", "description": "Texto contenido en el nombre o tipo de habitación"},
         "huesped": {"type": "string", "description": "Texto contenido en el nombre del huésped"},
         "momento": {"type": "string", "enum": ["todas", "pasadas", "en_curso", "futuras"]},
         "orden": {"type": "string", "enum": ["recientes", "antiguas"]},
         "limite": {"type": "integer", "description": "Máximo 25, por defecto 10"}},
        ["propiedad"]),
    _herramienta(
        "resumen",
        "Cifras de un periodo: reservas, noches, estancia media, antelación media de la reserva (días), ocupación %, noches libres, ingresos brutos/comisiones/netos, precio medio por noche y RevPAR. "
        "Agrupadas por canal, habitación, tipo de habitación, mes, país de procedencia del huésped o en total.",
        {"propiedad": _PROP, "desde": _FECHA, "hasta": _FECHA, "agrupar_por": _AGRUPAR},
        ["propiedad", "desde", "hasta", "agrupar_por"]),
    _herramienta(
        "grafico",
        "Dibuja un gráfico en la app calculando él mismo los datos (no pases números). tarta = proporciones (solo métricas que suman: reservas, noches, ingresos, comisiones); barras = comparar grupos; linea = evolución por mes.",
        {"tipo": {"type": "string", "enum": ["tarta", "barras", "linea"]}, "titulo": {"type": "string", "description": "Título corto del gráfico"},
         "propiedad": _PROP, "desde": _FECHA, "hasta": _FECHA,
         "agrupar_por": {"type": "string", "enum": ["canal", "habitacion", "tipo_habitacion", "mes", "pais"]},
         "metrica": {"type": "string", "enum": list(METRICAS)}},
        ["tipo", "titulo", "propiedad", "desde", "hasta", "agrupar_por", "metrica"]),
    _herramienta(
        "comparar",
        "Compara dos periodos (p. ej. este mes con el mismo mes del año pasado, o dos canales entre sí por periodo) y calcula las diferencias y variaciones %. Usa esto en vez de restar tú.",
        {"propiedad": _PROP, "desde_a": _FECHA, "hasta_a": _FECHA, "desde_b": _FECHA, "hasta_b": _FECHA,
         "agrupar_por": {"type": "string", "enum": ["total", "canal", "habitacion", "tipo_habitacion", "pais"]}},
        ["propiedad", "desde_a", "hasta_a", "desde_b", "hasta_b"]),
    _herramienta(
        "cancelaciones",
        "Reservas canceladas de un periodo (por fecha de entrada): por canal, tasa de cancelación e importe cancelado.",
        {"propiedad": _PROP, "desde": _FECHA, "hasta": _FECHA}, ["propiedad", "desde", "hasta"]),
    _herramienta(
        "agenda",
        "Agenda de un día: llegadas (con el estado de su parte de viajeros), salidas, huéspedes que se quedan y habitaciones ocupadas esa noche.",
        {"propiedad": _PROP_OPC, "fecha": {"type": "string", "description": "YYYY-MM-DD; por defecto hoy"}}, []),
    _herramienta(
        "partes",
        "Estado del parte de viajeros (registro oficial en RPV) de las llegadas de hoy a dentro de 30 días: recibido, incompleto, pendiente, etc. RPV no informa de fechas pasadas.",
        {"propiedad": _PROP_OPC, "desde": _FECHA, "hasta": _FECHA, "solo_pendientes": {"type": "boolean", "description": "Solo las llegadas cuyo parte aún no está recibido"}}, []),
    _herramienta(
        "disponibilidad",
        "Noches libres, ocupadas y bloqueadas de cada habitación en un rango (máx. 120 días), con tramos libres y precio de calendario de esas noches.",
        {"propiedad": _PROP, "desde": _FECHA, "hasta": _FECHA, "habitacion": {"type": "string", "description": "Texto del nombre o tipo de habitación (opcional)"}},
        ["propiedad", "desde", "hasta"]),
    _herramienta(
        "finanzas_mes",
        "Informe financiero de un mes (el de la pestaña Finanzas): ingresos, comisiones, ocupación y rentabilidad estimada (beneficio tras costes e impuestos).",
        {"propiedad": _PROP, "mes": {"type": "string", "description": "YYYY-MM"}}, ["propiedad", "mes"]),
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
4. Ingresos brutos = precio de las reservas; netos = brutos menos la comisión del canal. Las noches y los ingresos de una estancia que cruza varios meses se reparten por noches. La rentabilidad de finanzas_mes usa costes estimados: avísalo. Para comparar periodos usa comparar (no restes tú) y avisa si tienen distinta duración.
5. El «parte de viajeros» es el registro oficial de huéspedes en RPV. Que una reserva de menos de 24 h aún no conste en RPV es normal; si lleva más, avísalo como algo a revisar. Los precios de disponibilidad son los del calendario de venta, no lo que pagó cada huésped.
6. Responde en español, breve y directo, en texto plano (sin markdown ni tablas; para listas usa «•»). Importes en euros, con separador de miles y sin decimales salvo que importen.
7. Solo lees datos: no puedes crear, modificar ni cancelar reservas, ni cambiar precios ni bloqueos; si te lo piden, indica que lo hagan desde las pantallas de la app.
8. No tienes teléfonos ni correos de huéspedes.
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

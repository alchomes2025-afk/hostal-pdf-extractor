"""
services/asesor.py — Costes fijos reales y perfil contable de cada propiedad («Asesor Contable»).

El objetivo es que la rentabilidad use costes lo más reales posible:
  1. Facturas subidas: cada factura de gasto cubre `cobertura_meses` (un seguro anual = 12, el agua = 2, la luz = 1…) y se reparte entre esos meses a partir del mes
     de la factura («devengo»). Así un IBI o un seguro no cae entero en un solo mes.
  2. Lo que el usuario declara al Asesor Contable (importe anual o mensual por concepto) cuando no hay factura que cubra ese mes.
  3. Lo que falta se avisa («faltan: IBI, seguro…») y el asesor lo pregunta.
Cada concepto de costes fijos tiene su estado: «facturas», «declarado», «no_aplica» o «falta». Perfil guardado en Firestore (`contabilidad_perfil`).
"""
import logging
from datetime import datetime

from services import facturas as fac

logger = logging.getLogger(__name__)

COLECCION_PERFIL = "contabilidad_perfil"
COBERTURAS, COBERTURA_DEFECTO = fac.COBERTURAS, fac.COBERTURA_DEFECTO     # los define facturas.py (lo habitual por categoría; se cambia en cada factura)
MODOS = ("anual", "mensual", "no_aplica")
MODOS_LIMPIEZA = ("estancia", "hora", "fija", "externa", "no_aplica")   # externa = la hace una empresa y se sube su factura; fija = cuota mensual
# Compras «de obra o de equipamiento»: las que suelen ser de otro proyecto y no de la propiedad (se le pregunta al usuario)
CATEGORIAS_COMPRAS = ("Mobiliario y equipamiento", "Mantenimiento y reparaciones", "Consumibles y amenities", "Otros gastos", "Publicidad")

CONCEPTOS_FIJOS = [
    {"id": "ibi", "nombre": "IBI (impuesto de bienes inmuebles)", "categorias": ["Impuestos y tasas"], "modo": "anual",
     "pregunta": "¿Cuánto pagas de IBI al año?",
     "ayuda": "Es el recibo del Ayuntamiento, normalmente de pago anual. Si no lo pagas tú (el local o la vivienda es alquilado y lo paga el dueño), elige «No aplica». "
              "Si subes el recibo como factura (categoría «Impuestos y tasas», cubre 12 meses), lo usaré yo solo."},
    {"id": "seguro", "nombre": "Seguros (comercio, hogar, responsabilidad civil)", "categorias": ["Seguros"], "modo": "anual",
     "pregunta": "¿Cuánto pagas de seguros al año?",
     "ayuda": "Suma todas las pólizas de la propiedad (comercio o hogar, responsabilidad civil…). Si subes las facturas de la póliza, las reparto en 12 meses."},
    {"id": "comunidad", "nombre": "Comunidad de propietarios", "categorias": ["Comunidad de propietarios"], "modo": "mensual",
     "pregunta": "¿Cuánto pagas de comunidad de propietarios al mes?",
     "ayuda": "La cuota ordinaria (y, si la hay, la derrama repartida). Si no hay comunidad, elige «No aplica»."},
    {"id": "alquiler", "nombre": "Alquiler del local o de la vivienda", "categorias": ["Alquiler"], "modo": "mensual",
     "pregunta": "¿Pagas alquiler por la propiedad? ¿Cuánto al mes?",
     "ayuda": "Solo si el inmueble NO es tuyo. Si es en propiedad, elige «No aplica»."},
    {"id": "suministros", "nombre": "Luz, agua, gas e internet", "categorias": ["Suministros (luz, agua, gas, internet)"], "modo": "mensual",
     "pregunta": "¿Cuánto gastas de media al mes en luz, agua, gas e internet?",
     "ayuda": "Una media de los últimos meses. Cuando subas esas facturas calcularé la media real yo solo (el agua suele cubrir 2 meses)."},
    {"id": "software", "nombre": "Software y suscripciones (Beds24, RPV, programas…)", "categorias": ["Software y suscripciones"], "modo": "mensual",
     "pregunta": "¿Cuánto pagas al mes en programas y suscripciones del negocio?",
     "ayuda": "Beds24, RPV, canal manager, correo, almacenamiento… Si pagas por año, dímelo en anual."},
    {"id": "gestoria", "nombre": "Gestoría y asesoría", "categorias": ["Gestoría y asesoría"], "modo": "mensual",
     "pregunta": "¿Cuánto pagas de gestoría o asesoría?",
     "ayuda": "Cuota de la gestoría o asesoría fiscal y laboral. Puedes indicarla al mes o al año."},
    {"id": "personal", "nombre": "Personal (nóminas y Seguridad Social)", "categorias": [], "modo": "mensual",
     "pregunta": "¿Tienes personal contratado? ¿Cuánto cuesta al mes en total?",
     "ayuda": "Nómina + Seguridad Social de la empresa de las personas que trabajan en esta propiedad. Las nóminas no son facturas, así que solo las puedo saber por ti. "
              "Si no hay personal, elige «No aplica»."},
]
_POR_ID = {c["id"]: c for c in CONCEPTOS_FIJOS}


# ── Perfil (lo que declara el usuario) ──────────────────────────────────────────
PERFIL_VACIO = {"conceptos": {}, "limpieza": None, "excluidas": [], "completar": False, "compras_decididas": False}


def cuenta(f, perfil_):
    """¿Esta factura de gasto cuenta en la rentabilidad de la propiedad? No si está marcada «de otro proyecto» o su categoría está excluida en el perfil."""
    return not f.get("proyecto_ajeno") and f.get("categoria") not in (perfil_.get("excluidas") or [])


def _doc_perfil(cliente_id, propiedad_id):
    return f"{cliente_id}-{propiedad_id}"


def perfil(cliente_id, propiedad_id):
    """{conceptos, limpieza, excluidas (categorías de compras que NO son de esta propiedad), completar (rellenar lo que falte con la estimación de siempre), compras_decididas}."""
    col = fac.config.db.collection(COLECCION_PERFIL) if fac.config.db is not None else None
    if col is None:
        return dict(PERFIL_VACIO)
    try:
        doc = col.document(_doc_perfil(cliente_id, propiedad_id)).get()
        d = doc.to_dict() if doc.exists else {}
    except Exception as e:
        logger.error(f"[asesor] Error leyendo el perfil: {e}")
        return dict(PERFIL_VACIO)
    return {"conceptos": d.get("conceptos") or {}, "limpieza": d.get("limpieza"), "excluidas": d.get("excluidas") or [], "completar": bool(d.get("completar")),
            "compras_decididas": bool(d.get("compras_decididas"))}


def guardar_perfil(cliente_id, propiedad_id, conceptos, limpieza, usuario="", excluidas=None, completar=None):
    """Guarda las respuestas del Asesor (se mezclan con las anteriores). conceptos: {id: {modo, importe}}; limpieza: {modo, importe}."""
    col = fac.config.db.collection(COLECCION_PERFIL) if fac.config.db is not None else None
    if col is None:
        raise fac.FacturaError("no se puede guardar ahora mismo (sin base de datos)", 503)
    actual = perfil(cliente_id, propiedad_id)
    nuevos = dict(actual["conceptos"])
    for cid, r in (conceptos or {}).items():
        if cid not in _POR_ID:
            raise fac.FacturaError(f"concepto desconocido: {cid}")
        if not isinstance(r, dict) or r.get("modo") not in MODOS:
            raise fac.FacturaError(f"respuesta no válida para {cid}")
        if r["modo"] == "no_aplica":
            nuevos[cid] = {"modo": "no_aplica", "importe": 0.0}
            continue
        importe = fac.a_numero(r.get("importe"))
        if importe is None or importe < 0 or importe > 1_000_000:
            raise fac.FacturaError(f"importe no válido para {_POR_ID[cid]['nombre']}")
        nuevos[cid] = {"modo": r["modo"], "importe": importe}
    lim = actual["limpieza"]
    if limpieza:
        modo = limpieza.get("modo")
        if modo not in MODOS_LIMPIEZA:
            raise fac.FacturaError("respuesta de limpieza no válida")
        if modo in ("externa", "no_aplica"):
            lim = {"modo": modo, "importe": 0.0}
        else:
            imp = fac.a_numero(limpieza.get("importe"))
            if imp is None or not 0 <= imp <= 10_000:
                raise fac.FacturaError("importe de limpieza no válido")
            lim = {"modo": modo, "importe": imp}
            if modo == "hora":   # horas al día según la ocupación (hostal) o por estancia (casa); opcionales
                for k in ("horas_min", "horas_max"):
                    h = fac.a_numero(limpieza.get(k))
                    if h is not None:
                        if not 0 <= h <= 24:
                            raise fac.FacturaError("las horas de limpieza no son válidas")
                        lim[k] = h
    exc = actual["excluidas"]
    decididas = actual["compras_decididas"]
    if excluidas is not None:
        malas = [c for c in excluidas if c not in fac.CATEGORIAS["gasto"]]
        if malas:
            raise fac.FacturaError(f"categoría desconocida: {malas[0]}")
        exc, decididas = sorted(set(excluidas)), True
    comp = actual["completar"] if completar is None else bool(completar)
    col.document(_doc_perfil(cliente_id, propiedad_id)).set({"conceptos": nuevos, "limpieza": lim, "excluidas": exc, "completar": comp, "compras_decididas": decididas,
                                                          "actualizado": datetime.now().strftime("%Y-%m-%d %H:%M"), "por": str(usuario or "")[:40]})
    return {"conceptos": nuevos, "limpieza": lim, "excluidas": exc, "completar": comp, "compras_decididas": decididas}


def mensual_declarado(r):
    """Importe mensual de una respuesta del perfil: anual → /12; mensual → tal cual; no aplica → 0; sin respuesta → None."""
    if not r:
        return None
    if r.get("modo") == "no_aplica":
        return 0.0
    importe = float(r.get("importe") or 0)
    return round(importe / 12, 2) if r.get("modo") == "anual" else round(importe, 2)


# ── Devengo de las facturas ──────────────────────────────────────────────────────
def cobertura(f):
    """Meses que cubre una factura (de la propia factura o el habitual de su categoría)."""
    try:
        c = int(f.get("cobertura_meses") or 0)
    except (TypeError, ValueError):
        c = 0
    return c if c in COBERTURAS else COBERTURA_DEFECTO.get(f.get("categoria"), 1)


def _indice(mes):
    a, m = str(mes).split("-")
    return int(a) * 12 + int(m) - 1


def devengo(f, mes):
    """Parte de la factura (con IVA, un abono resta) que corresponde a ese mes: total / meses que cubre, desde el mes de la factura."""
    if not f.get("mes"):
        return 0.0
    ini, c = _indice(f["mes"]), cobertura(f)
    if not ini <= _indice(mes) < ini + c:
        return 0.0
    return (-1 if f.get("es_rectificativa") else 1) * (f.get("total") or 0) / c


def facturas_de_propiedad(cliente_id, propiedad_id):
    col = fac._coleccion()
    if col is None:
        return []
    try:
        return [d.to_dict() for d in col.where("cliente", "==", cliente_id).where("propiedad_id", "==", str(propiedad_id)).stream()]
    except Exception as e:
        logger.error(f"[asesor] Error cargando las facturas: {e}")
        return []


def costes_fijos_mes(cliente_id, propiedad_id, mes, facturas=None, perfil_=None):
    """Costes fijos de un mes por concepto: facturas (devengo) → declarado → falta.
    {conceptos: {id: {importe, origen, facturas}}, total, faltan: [ids], hay_datos}."""
    facs = facturas if facturas is not None else facturas_de_propiedad(cliente_id, propiedad_id)
    p = perfil_ if perfil_ is not None else perfil(cliente_id, propiedad_id)
    conceptos, faltan = {}, []
    for c in CONCEPTOS_FIJOS:
        propias = [x for x in facs if x.get("tipo") == "gasto" and x.get("categoria") in c["categorias"] and cuenta(x, p)]
        dev = round(sum(devengo(x, mes) for x in propias), 2)
        declarado = mensual_declarado((p.get("conceptos") or {}).get(c["id"]))
        if dev > 0:
            conceptos[c["id"]] = {"nombre": c["nombre"], "importe": dev, "origen": "facturas", "facturas": sum(1 for x in propias if devengo(x, mes))}
        elif declarado is not None:
            conceptos[c["id"]] = {"nombre": c["nombre"], "importe": declarado, "origen": "declarado" if declarado > 0 else "no_aplica", "facturas": 0}
        else:
            conceptos[c["id"]] = {"nombre": c["nombre"], "importe": 0.0, "origen": "falta", "facturas": 0}
            faltan.append(c["id"])
    return {"conceptos": conceptos, "total": round(sum(x["importe"] for x in conceptos.values()), 2), "faltan": faltan,
            "hay_datos": any(x["origen"] in ("facturas", "declarado") for x in conceptos.values())}


# ── Estado para el asistente ─────────────────────────────────────────────────────
def detectado(facs, categorias):
    """Lo que dicen las facturas subidas de un concepto: cuántas, qué meses cubren y a cuánto sale al mes cada una (para preguntar si es siempre lo mismo)."""
    mias = [x for x in facs if x.get("tipo") == "gasto" and x.get("categoria") in categorias and cuenta(x, {})]
    if not mias:
        return None
    mensuales = [round((-1 if x.get("es_rectificativa") else 1) * (x.get("total") or 0) / cobertura(x), 2) for x in mias]
    return {"facturas": len(mias), "meses": sorted({x.get("mes") for x in mias if x.get("mes")}), "cobertura": sorted({cobertura(x) for x in mias}),
            "mensual_min": min(mensuales), "mensual_max": max(mensuales), "mensual_medio": round(sum(mensuales) / len(mensuales), 2),
            "igual": max(mensuales) - min(mensuales) < 0.5}


def estado(cliente_id, propiedad_id, es_admin, mes_ref):
    """Lo que el asistente necesita: cada concepto con su origen y su importe mensual en el mes de referencia, y la configuración de limpieza."""
    c = fac.cliente(cliente_id)
    propiedad_id = fac.comprobar_propiedad(cliente_id, propiedad_id, es_admin)
    facs = facturas_de_propiedad(cliente_id, propiedad_id)
    p = perfil(cliente_id, propiedad_id)
    calculo = costes_fijos_mes(cliente_id, propiedad_id, mes_ref, facs, p)
    conceptos = []
    for def_ in CONCEPTOS_FIJOS:
        x = calculo["conceptos"][def_["id"]]
        decl = (p["conceptos"] or {}).get(def_["id"])
        conceptos.append({"id": def_["id"], "nombre": def_["nombre"], "pregunta": def_["pregunta"], "ayuda": def_["ayuda"], "modo_sugerido": def_["modo"],
                          "origen": x["origen"], "mensual": x["importe"], "facturas_en_el_mes": x["facturas"], "declarado": decl,
                          "tiene_facturas": any(f.get("tipo") == "gasto" and f.get("categoria") in def_["categorias"] for f in facs),
                          "detectado": detectado(facs, def_["categorias"])})
    es_hostal = propiedad_id == "339751"
    compras = {}
    for x in facs:
        if x.get("tipo") == "gasto" and x.get("categoria") in CATEGORIAS_COMPRAS:
            r = compras.setdefault(x["categoria"], {"categoria": x["categoria"], "facturas": 0, "total": 0.0})
            r["facturas"] += 1
            r["total"] += (-1 if x.get("es_rectificativa") else 1) * (x.get("total") or 0)
    lista = sorted(({**v, "total": round(v["total"], 2)} for v in compras.values()), key=lambda v: -v["total"])
    limpiezas = [x for x in facs if x.get("tipo") == "gasto" and x.get("categoria") == "Limpieza y lavandería"]
    ideas_hostal = ["Por horas: unas 1 a 3 horas al día según la ocupación (la estimación de siempre del hostal) × lo que valga una hora (9 a 15 € es lo habitual).",
                    "Cuota fija al mes: lo que pagarías a una persona o empresa por dejarlo todo en orden.",
                    "Coste de oportunidad: las horas reales que dedicáis × lo que cobraría una persona externa por hora."]
    ideas_casa = ["Por estancia: una limpieza profesional de un apartamento turístico suele costar de 40 a 80 € por reserva (la estimación de siempre es 69 €, con productos).",
                  "Por horas: horas que lleva limpiar y preparar la casa × lo que valga una hora (9 a 15 €).",
                  "Cuota fija al mes: lo que pagarías por un servicio recurrente.",
                  "Coste de oportunidad: las horas reales que dedicáis × lo que cobraría una persona externa."]
    return {"propiedad": c["propiedades"][propiedad_id], "propiedad_id": propiedad_id, "mes_ref": mes_ref, "conceptos": conceptos, "fijos_mensual": calculo["total"],
            "faltan": calculo["faltan"], "facturas_subidas": len(facs), "completar": p["completar"], "estimacion_anual_habitual": 5700.0 if es_hostal else 3100.0,
            "compras": {"facturas": sum(v["facturas"] for v in lista), "total": round(sum(v["total"] for v in lista), 2), "categorias": lista,
                        "excluidas": p["excluidas"], "decidido": p["compras_decididas"]},
            "limpieza": {"declarado": p["limpieza"], "facturas": len(limpiezas), "modo_habitual": "hora" if es_hostal else "estancia",
                         "importe_habitual": 10.0 if es_hostal else 69.0, "horas_habituales": [1.0, 3.0] if es_hostal else None,
                         "ideas": ideas_hostal if es_hostal else ideas_casa}}

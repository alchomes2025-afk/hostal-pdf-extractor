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
MODOS_LIMPIEZA = ("estancia", "hora")

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
def _doc_perfil(cliente_id, propiedad_id):
    return f"{cliente_id}-{propiedad_id}"


def perfil(cliente_id, propiedad_id):
    """{conceptos: {id: {modo, importe}}, limpieza: {modo, importe}|None}. Vacío si no hay."""
    col = fac.config.db.collection(COLECCION_PERFIL) if fac.config.db is not None else None
    if col is None:
        return {"conceptos": {}, "limpieza": None}
    try:
        doc = col.document(_doc_perfil(cliente_id, propiedad_id)).get()
        d = doc.to_dict() if doc.exists else {}
    except Exception as e:
        logger.error(f"[asesor] Error leyendo el perfil: {e}")
        return {"conceptos": {}, "limpieza": None}
    return {"conceptos": d.get("conceptos") or {}, "limpieza": d.get("limpieza")}


def guardar_perfil(cliente_id, propiedad_id, conceptos, limpieza, usuario=""):
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
        imp = fac.a_numero(limpieza.get("importe"))
        if limpieza.get("modo") not in MODOS_LIMPIEZA or imp is None or not 0 <= imp <= 10_000:
            raise fac.FacturaError("respuesta de limpieza no válida")
        lim = {"modo": limpieza["modo"], "importe": imp}
    col.document(_doc_perfil(cliente_id, propiedad_id)).set({"conceptos": nuevos, "limpieza": lim, "actualizado": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                                          "por": str(usuario or "")[:40]})
    return {"conceptos": nuevos, "limpieza": lim}


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
        propias = [x for x in facs if x.get("tipo") == "gasto" and x.get("categoria") in c["categorias"]]
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
                          "tiene_facturas": any(f.get("tipo") == "gasto" and f.get("categoria") in def_["categorias"] for f in facs)})
    es_hostal = propiedad_id == "339751"
    return {"propiedad": c["propiedades"][propiedad_id], "propiedad_id": propiedad_id, "mes_ref": mes_ref, "conceptos": conceptos, "fijos_mensual": calculo["total"],
            "faltan": calculo["faltan"], "limpieza": {"declarado": p["limpieza"], "modo_habitual": "hora" if es_hostal else "estancia",
                                                      "importe_habitual": 10.0 if es_hostal else 69.0},
            "facturas_subidas": len(facs)}

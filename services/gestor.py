"""
services/gestor.py — Resumen trimestral para el gestor: lo que necesita para el IVA y la contabilidad de un trimestre y una propiedad.

Se calcula con las facturas GUARDADAS del trimestre: gastos por categoría (base, IVA soportado, total), ingresos facturados, IVA soportado y repercutido y avisos de
cosas que el gestor tendrá que mirar. Opcionalmente suma los ingresos por reservas de Beds24 (el IVA de alojamiento repercutido se ESTIMA con IVA_ALOJAMIENTO_PCT:
es solo una ayuda, no una declaración). Se puede volcar a una hoja de Google en la carpeta de la propiedad (pestañas «Resumen» y «Detalle»), que el gestor ya puede abrir
porque la carpeta está compartida.
"""
import logging
from datetime import datetime

from services import asesor, puente_plataforma
from services import facturas as fac

logger = logging.getLogger(__name__)

IVA_ALOJAMIENTO_PCT = 10.0     # IVA reducido del alojamiento turístico en España; hay que confirmarlo con el gestor
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def meses_del_trimestre(anio, trimestre):
    if trimestre not in (1, 2, 3, 4) or not 2000 <= anio <= 2100:
        raise fac.FacturaError("trimestre no válido")
    return [f"{anio}-{m:02d}" for m in range(3 * (trimestre - 1) + 1, 3 * trimestre + 1)]


def _suma(items, campo):
    return round(sum((-1 if x.get("es_rectificativa") else 1) * (x.get(campo) or 0) for x in items), 2)


def _por_categoria(items):
    cats = {}
    for x in items:
        cats.setdefault(x.get("categoria") or "Otros gastos", []).append(x)
    return [{"categoria": c, "facturas": len(xs), "base": _suma(xs, "base_imponible"), "iva": _suma(xs, "cuota_iva"), "total": _suma(xs, "total")}
            for c, xs in sorted(cats.items(), key=lambda kv: -abs(_suma(kv[1], "total")))]


def resumen_trimestral(cliente_id, propiedad_id, anio, trimestre, es_admin, ingresos_reservas=None):
    """ingresos_reservas: {mes: {brutos, comisiones, netos}} opcional (lo calcula el que llama con el informe de Finanzas)."""
    propiedad_id = fac.comprobar_propiedad(cliente_id, propiedad_id, es_admin)
    meses = meses_del_trimestre(int(anio), int(trimestre))
    nombre = fac.cliente(cliente_id)["propiedades"][propiedad_id]
    facs = [x for x in asesor.facturas_de_propiedad(cliente_id, propiedad_id) if x.get("mes") in meses]
    gastos = [x for x in facs if x.get("tipo") != "ingreso"]
    ingresos = [x for x in facs if x.get("tipo") == "ingreso"]
    iva_soportado, iva_repercutido = _suma(gastos, "cuota_iva"), _suma(ingresos, "cuota_iva")
    reservas = None
    iva_estimado = None
    if ingresos_reservas:
        reservas = [{"mes": m, **{k: round(float(ingresos_reservas[m].get(k) or 0), 2) for k in ("brutos", "comisiones", "netos")}} for m in meses if m in ingresos_reservas]
        brutos = sum(r["brutos"] for r in reservas)
        iva_estimado = round(brutos - brutos / (1 + IVA_ALOJAMIENTO_PCT / 100), 2)

    avisos = []
    sin_nif = [x for x in gastos if not x.get("nif")]
    if sin_nif:
        avisos.append(f"{len(sin_nif)} facturas de gasto no tienen NIF del proveedor: el gestor los necesitará para deducir el IVA.")
    sin_numero = [x for x in facs if not x.get("numero")]
    if sin_numero:
        avisos.append(f"{len(sin_numero)} facturas no tienen número de factura.")
    sin_iva = [x for x in gastos if (x.get("base_imponible") or 0) > 0 and not x.get("cuota_iva")]
    if sin_iva:
        avisos.append(f"{len(sin_iva)} facturas de gasto no llevan IVA: revisa si son exentas o si falta el desglose.")
    extranjeras = [x for x in gastos if "b.v." in str(x.get("proveedor") or "").lower() or str(x.get("nif") or "").upper().startswith(("NL", "IE", "LU"))]
    if extranjeras:
        avisos.append(f"{len(extranjeras)} facturas parecen de proveedores de la UE (p. ej. Booking.com B.V.): suelen llevar inversión del sujeto pasivo y su IVA no se deduce como una factura "
                      "española normal. Consúltalo con el gestor.")
    rect = [x for x in facs if x.get("es_rectificativa")]
    if rect:
        avisos.append(f"{len(rect)} facturas rectificativas o abonos (restan en los totales).")
    if not facs:
        avisos.append("Este trimestre no tiene facturas guardadas.")
    avisos.append("Solo cuentan las facturas subidas y guardadas en la app: si faltan facturas, los totales saldrán más bajos que los reales.")

    detalle = [{"fecha": x.get("fecha"), "tipo": "Ingreso" if x.get("tipo") == "ingreso" else "Gasto", "proveedor": x.get("proveedor"), "nif": x.get("nif") or "", "numero": x.get("numero") or "",
                "concepto": x.get("concepto") or "", "categoria": x.get("categoria"), "base": x.get("base_imponible"), "iva_pct": x.get("iva_pct"), "cuota_iva": x.get("cuota_iva"),
                "retencion": x.get("retencion") or 0, "total": x.get("total"), "rectificativa": bool(x.get("es_rectificativa")), "cubre_meses": asesor.cobertura(x), "url": x.get("url") or ""}
               for x in sorted(facs, key=lambda y: (y.get("fecha") or "", y.get("proveedor") or ""))]
    return {"propiedad": nombre, "propiedad_id": propiedad_id, "anio": int(anio), "trimestre": int(trimestre), "meses": meses,
            "gastos": {"facturas": len(gastos), "por_categoria": _por_categoria(gastos), "base": _suma(gastos, "base_imponible"), "iva": iva_soportado, "total": _suma(gastos, "total")},
            "ingresos": {"facturas": len(ingresos), "por_categoria": _por_categoria(ingresos), "base": _suma(ingresos, "base_imponible"), "iva": iva_repercutido, "total": _suma(ingresos, "total")},
            "iva": {"soportado": iva_soportado, "repercutido_facturas": iva_repercutido, "repercutido_estimado_reservas": iva_estimado,
                    "saldo_estimado": round((iva_repercutido + (iva_estimado or 0)) - iva_soportado, 2), "pct_alojamiento": IVA_ALOJAMIENTO_PCT},
            "reservas": reservas, "avisos": avisos, "detalle": detalle}


def filas_hoja(r):
    """Pestañas «Resumen» y «Detalle» de la hoja del gestor: {nombre: [[celdas]]}."""
    t = f"{r['trimestre']}º trimestre {r['anio']}"
    res = [[f"Resumen trimestral para el gestor — {r['propiedad']} — {t}"], [f"Generado el {datetime.now():%d/%m/%Y %H:%M} desde la app de gestión (solo facturas guardadas)"], [],
           ["GASTOS (facturas recibidas)"], ["Categoría", "Nº facturas", "Base imponible", "IVA soportado", "Total"]]
    res += [[c["categoria"], c["facturas"], c["base"], c["iva"], c["total"]] for c in r["gastos"]["por_categoria"]]
    res += [["TOTAL GASTOS", r["gastos"]["facturas"], r["gastos"]["base"], r["gastos"]["iva"], r["gastos"]["total"]], []]
    if r["ingresos"]["facturas"]:
        res += [["INGRESOS (facturas emitidas)"], ["Categoría", "Nº facturas", "Base imponible", "IVA repercutido", "Total"]]
        res += [[c["categoria"], c["facturas"], c["base"], c["iva"], c["total"]] for c in r["ingresos"]["por_categoria"]]
        res += [["TOTAL INGRESOS FACTURADOS", r["ingresos"]["facturas"], r["ingresos"]["base"], r["ingresos"]["iva"], r["ingresos"]["total"]], []]
    res += [["IVA"], ["IVA soportado (facturas de gasto)", r["iva"]["soportado"]], ["IVA repercutido (facturas emitidas)", r["iva"]["repercutido_facturas"]]]
    if r["iva"]["repercutido_estimado_reservas"] is not None:
        res += [[f"IVA repercutido ESTIMADO de las reservas ({r['iva']['pct_alojamiento']:g} % incluido en el precio de Beds24; confirmar con el gestor)", r["iva"]["repercutido_estimado_reservas"]]]
    res += [["Saldo estimado (repercutido − soportado)", r["iva"]["saldo_estimado"]], []]
    if r["reservas"]:
        res += [["INGRESOS POR RESERVAS (Beds24)"], ["Mes", "Ingresos brutos", "Comisiones de portales", "Ingresos netos"]]
        res += [[x["mes"], x["brutos"], x["comisiones"], x["netos"]] for x in r["reservas"]] + [[]]
    res += [["AVISOS PARA EL GESTOR"]] + [[a] for a in r["avisos"]]
    det = [["Fecha", "Tipo", "Proveedor / Cliente", "NIF", "Nº factura", "Concepto", "Categoría", "Base imponible", "IVA %", "Cuota IVA", "Retención", "Total", "Abono", "Cubre (meses)", "Enlace"]]
    det += [[d["fecha"], d["tipo"], d["proveedor"], d["nif"], d["numero"], d["concepto"], d["categoria"], d["base"], d["iva_pct"], d["cuota_iva"], d["retencion"], d["total"],
             "Sí" if d["rectificativa"] else "", d["cubre_meses"], d["url"]] for d in r["detalle"]]
    return {"Resumen": res, "Detalle": det}


def generar_hoja(cliente_id, propiedad_id, anio, trimestre, es_admin, ingresos_reservas=None):
    """Crea (o reescribe) la hoja de Google del trimestre en la carpeta de la propiedad y devuelve su enlace."""
    r = resumen_trimestral(cliente_id, propiedad_id, anio, trimestre, es_admin, ingresos_reservas)
    if not fac.habilitado(cliente_id):
        raise fac.FacturaError("la generación de hojas no está configurada", 503)
    titulo = f"Resumen {r['anio']}-{r['trimestre']}T para el gestor"
    try:
        out = puente_plataforma.resumen(fac.raiz_drive(cliente_id), [fac.CARPETA_FACTURAS, r["propiedad"], str(r["anio"])], titulo, filas_hoja(r))
    except puente_plataforma.PuenteError as e:
        logger.error(f"[gestor] Generar la hoja: {e}")
        raise fac.FacturaError("no se ha podido crear la hoja en el Drive; vuelve a intentarlo en un momento", 502)
    return {"url": out.get("url"), "titulo": titulo}

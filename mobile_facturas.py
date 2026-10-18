"""
mobile_facturas.py — Facturas desde la app de gestión (pestaña Finanzas): leer, revisar y guardar en el Drive del
cliente, y listar las del mes. La lógica vive en services/facturas.py; aquí solo el PIN y el formato HTTP.

Mismas reglas de PIN que /mobile/finance: con el PIN normal solo las propiedades que no son solo_admin (La Casa de la
Primavera); con el de administración, todas. La app de gestión actual es de un único cliente: ALC Homes.

  GET  /mobile/facturas/config                         → propiedades visibles, categorías y si está habilitado
  POST /mobile/facturas/leer     {propiedad_id, archivo:{nombre,mime,base64}}            → propuesta para revisar
  POST /mobile/facturas/guardar  {propiedad_id, archivo, datos, usuario}                 → guardada en el Drive
  POST /mobile/facturas/conocidas {huellas:[sha256…]}                                    → las que ya están guardadas (volver a subir un ZIP salta lo ya hecho)
  GET  /mobile/facturas/meses?propiedad_id=349341                                      → meses con facturas (cuántas y totales)
  GET  /mobile/facturas?propiedad_id=349341&mes=2026-10                                  → facturas y totales del mes
  GET  /mobile/facturas/asesor/estado?propiedad_id=…&mes=AAAA-MM                          → Asesor Contable: costes fijos por concepto (facturas / declarado / falta)
  POST /mobile/facturas/asesor/respuestas {propiedad_id, conceptos, limpieza}             → guarda lo que contesta el usuario al Asesor
  GET  /mobile/facturas/resumen-trimestral?propiedad_id&anio&trimestre[&reservas=1]       → resumen del trimestre para el gestor
  POST /mobile/facturas/resumen-trimestral/hoja {propiedad_id, anio, trimestre}           → hoja de Google en el Drive con ese resumen
  POST /mobile/facturas/editar   {id, propiedad_id, datos, usuario}                       → corrige una factura guardada (la renombra/mueve en el Drive y su fila)
  POST /mobile/facturas/eliminar {id, usuario}                                            → borra una factura guardada (archivo a la papelera, fila fuera)
  POST /mobile/facturas/extracto {propiedad_id, archivo:{nombre,mime,base64}, usuario}    → extracto bancario guardado en el Drive (para el cotejo)
  POST /mobile/facturas/cotejar {propiedad_id, archivo, usuario}                          → cotejo del extracto (PDF BBVA o CSV) con las facturas guardadas
  POST /mobile/facturas/conectar  (PIN de administrador)                                  → {url} del conector para dar permiso al Drive
  POST /mobile/facturas/conectado (lo llama el conector, firmado con PUENTE_SECRETO)      → crea las subcarpetas y guarda la conexión
"""
import logging

from flask import Blueprint, jsonify, request

from config import CONECTOR_URL
from mobile_routes import _es_pin_admin, check_pin
import re
from datetime import datetime

from services import asesor, drive_cliente, facturas, gestor, puente_plataforma

logger = logging.getLogger(__name__)
facturas_bp = Blueprint("facturas", __name__, url_prefix="/mobile/facturas")

CLIENTE_APP = "alc-homes"


def _error(e):
    return jsonify({"ok": False, "error": str(e)}), getattr(e, "status", 400)


@facturas_bp.route("/config", methods=["GET"])
def config_facturas():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    props = facturas.propiedades_visibles(CLIENTE_APP, _es_pin_admin())
    return jsonify({"ok": True, "habilitado": facturas.habilitado(CLIENTE_APP),
                    "conector": bool(CONECTOR_URL and puente_plataforma.configurado()), "puede_conectar": _es_pin_admin(),
                    "propiedades": [{"id": k, "nombre": v} for k, v in props.items()], "categorias": facturas.CATEGORIAS})


@facturas_bp.route("/conectar", methods=["POST"])
def conectar_drive():
    """Primer uso: devuelve la dirección del conector (con un token de un solo uso) para que el cliente dé permiso a su Drive."""
    if not _es_pin_admin():
        return jsonify({"ok": False, "error": "Solo el administrador puede conectar el Drive"}), 403
    if not (CONECTOR_URL and puente_plataforma.configurado()):
        return jsonify({"ok": False, "error": "El conector de Drive aún no está configurado en el servidor"}), 503
    body = request.get_json(silent=True) or {}
    try:
        token = drive_cliente.nuevo_token(CLIENTE_APP, body.get("usuario"))
    except drive_cliente.ConexionError as e:
        return _error(e)
    return jsonify({"ok": True, "url": f"{CONECTOR_URL}{'&' if '?' in CONECTOR_URL else '?'}t={token}"})


@facturas_bp.route("/conectado", methods=["POST"])
def drive_conectado():
    """Aviso del conector (no lleva PIN: va firmado con el secreto compartido y un token de un solo uso)."""
    body = request.get_json(silent=True) or {}
    try:
        r = drive_cliente.completar(body.get("token"), body.get("folder_id"), body.get("email"), body.get("secret"),
                                    facturas.estructura_inicial(CLIENTE_APP))
    except drive_cliente.ConexionError as e:
        return _error(e)
    return jsonify({"ok": True, **r})


@facturas_bp.route("/leer", methods=["POST"])
def leer_factura():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **facturas.leer(CLIENTE_APP, body.get("propiedad_id"), body.get("archivo"), _es_pin_admin())})
    except facturas.FacturaError as e:
        return _error(e)


@facturas_bp.route("/guardar", methods=["POST"])
def guardar_factura():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    body = request.get_json(silent=True) or {}
    try:
        factura = facturas.guardar(CLIENTE_APP, body.get("propiedad_id"), body.get("archivo"), body.get("datos"),
                                   _es_pin_admin(), usuario=body.get("usuario"))
        logger.info(f"[facturas] Guardada {factura['id']} ({factura['tipo']}, {factura['fecha']}, {factura['total']}) por {factura['usuario'] or '?'}")
        return jsonify({"ok": True, "factura": factura})
    except facturas.FacturaError as e:
        return _error(e)


def _mes_actual():
    return datetime.now().strftime("%Y-%m")


@facturas_bp.route("/asesor/estado", methods=["GET"])
def asesor_estado():
    """Asesor Contable: cada concepto de costes fijos de la propiedad con su origen (facturas / declarado / no aplica / falta) y su importe mensual."""
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    mes = request.args.get("mes") or _mes_actual()
    if not re.fullmatch(r"\d{4}-\d{2}", mes):
        return jsonify({"ok": False, "error": "mes debe tener formato AAAA-MM"}), 400
    try:
        return jsonify({"ok": True, **asesor.estado(CLIENTE_APP, request.args.get("propiedad_id"), _es_pin_admin(), mes)})
    except facturas.FacturaError as e:
        return _error(e)


@facturas_bp.route("/asesor/respuestas", methods=["POST"])
def asesor_respuestas():
    """Guarda lo que el usuario contesta al Asesor: {propiedad_id, conceptos: {id: {modo: anual|mensual|no_aplica, importe}}, limpieza: {modo, importe, horas_min, horas_max},
    excluidas: [categorías de compras que son de OTRO proyecto], completar: bool (rellenar lo que falte con la estimación anual de siempre)}."""
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    body = request.get_json(silent=True) or {}
    try:
        propiedad = facturas.comprobar_propiedad(CLIENTE_APP, body.get("propiedad_id"), _es_pin_admin())
        exc = body.get("excluidas")
        asesor.guardar_perfil(CLIENTE_APP, propiedad, body.get("conceptos"), body.get("limpieza"), usuario=body.get("usuario"),
                              excluidas=exc if isinstance(exc, list) else None, completar=body.get("completar") if "completar" in body else None)
        return jsonify({"ok": True, **asesor.estado(CLIENTE_APP, propiedad, _es_pin_admin(), body.get("mes") if re.fullmatch(r"\d{4}-\d{2}", str(body.get("mes") or "")) else _mes_actual())})
    except facturas.FacturaError as e:
        return _error(e)


def _ingresos_reservas(propiedad_id, meses):
    """Ingresos de reservas por mes (informe de Finanzas): {mes: {brutos, comisiones, netos}}. Un mes que Beds24 no responda se omite."""
    from mobile_routes import InformeError, _informe_financiero
    salida = {}
    for m in meses:
        try:
            r = _informe_financiero(propiedad_id, m, "estimados")["resumen"]
            salida[m] = {"brutos": r["ingresos_brutos"], "comisiones": r["comisiones"], "netos": r["ingresos_netos"]}
        except (InformeError, Exception) as e:
            logger.warning(f"[facturas] Resumen trimestral sin ingresos de {m}: {e}")
    return salida


def _periodo(args):
    try:
        return int(args.get("anio")), int(args.get("trimestre"))
    except (TypeError, ValueError):
        raise facturas.FacturaError("indica el año y el trimestre (1 a 4)")


@facturas_bp.route("/resumen-trimestral", methods=["GET"])
def resumen_trimestral():
    """Resumen del trimestre para el gestor: gastos por categoría, IVA soportado y repercutido, avisos y detalle. ?reservas=1 suma los ingresos de Beds24."""
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    try:
        anio, t = _periodo(request.args)
        propiedad = facturas.comprobar_propiedad(CLIENTE_APP, request.args.get("propiedad_id"), _es_pin_admin())
        reservas = _ingresos_reservas(propiedad, gestor.meses_del_trimestre(anio, t)) if request.args.get("reservas") == "1" else None
        return jsonify({"ok": True, **gestor.resumen_trimestral(CLIENTE_APP, propiedad, anio, t, _es_pin_admin(), reservas)})
    except facturas.FacturaError as e:
        return _error(e)


@facturas_bp.route("/resumen-trimestral/hoja", methods=["POST"])
def resumen_trimestral_hoja():
    """Genera (o reescribe) la hoja de Google del trimestre en el Drive, lista para el gestor."""
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    body = request.get_json(silent=True) or {}
    try:
        anio, t = _periodo(body)
        propiedad = facturas.comprobar_propiedad(CLIENTE_APP, body.get("propiedad_id"), _es_pin_admin())
        reservas = _ingresos_reservas(propiedad, gestor.meses_del_trimestre(anio, t)) if body.get("reservas") else None
        return jsonify({"ok": True, **gestor.generar_hoja(CLIENTE_APP, propiedad, anio, t, _es_pin_admin(), reservas)})
    except facturas.FacturaError as e:
        return _error(e)


@facturas_bp.route("/editar", methods=["POST"])
def editar_factura():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "factura": facturas.editar(CLIENTE_APP, body.get("id"), body.get("datos"), body.get("propiedad_id"), _es_pin_admin(),
                                                              usuario=body.get("usuario"))})
    except facturas.FacturaError as e:
        return _error(e)


@facturas_bp.route("/eliminar", methods=["POST"])
def eliminar_factura():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **facturas.eliminar(CLIENTE_APP, body.get("id"), _es_pin_admin(), usuario=body.get("usuario"))})
    except facturas.FacturaError as e:
        return _error(e)


@facturas_bp.route("/extracto", methods=["POST"])
def subir_extracto():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **facturas.guardar_extracto(CLIENTE_APP, body.get("propiedad_id"), body.get("archivo"), _es_pin_admin(),
                                                               usuario=body.get("usuario"))})
    except facturas.FacturaError as e:
        return _error(e)


@facturas_bp.route("/cotejar", methods=["POST"])
def cotejar_facturas():
    """Cotejo de un extracto bancario (PDF de BBVA o CSV) con las facturas guardadas. El extracto también se guarda en el Drive."""
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **facturas.cotejar_extracto(CLIENTE_APP, body.get("propiedad_id"), body.get("archivo"), _es_pin_admin(),
                                                               usuario=body.get("usuario"))})
    except facturas.FacturaError as e:
        return _error(e)


@facturas_bp.route("/diag-ia", methods=["GET"])
def diag_ia():
    """Qué modelos usa la lectura de facturas (solo administrador; no devuelve claves)."""
    if not _es_pin_admin():
        return jsonify({"ok": False, "error": "Solo el administrador"}), 403
    from config import GROQ_MODEL_FALL, GROQ_MODEL_PRI
    return jsonify({"ok": True, "texto": [GROQ_MODEL_PRI, GROQ_MODEL_FALL], "vision": facturas._modelo_vision() or None,
                    "vision_fijado_por_variable": bool(facturas.GROQ_MODEL_VISION)})


@facturas_bp.route("/conocidas", methods=["POST"])
def facturas_conocidas():
    """Qué archivos de una tanda ya están guardados (por su huella): la app los salta antes de gastar IA en leerlos."""
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    body = request.get_json(silent=True) or {}
    return jsonify({"ok": True, "conocidas": facturas.conocidas(CLIENTE_APP, body.get("huellas"))})


@facturas_bp.route("/meses", methods=["GET"])
def meses_facturas():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    try:
        return jsonify({"ok": True, "meses": facturas.meses_con_facturas(CLIENTE_APP, request.args.get("propiedad_id"), _es_pin_admin())})
    except facturas.FacturaError as e:
        return _error(e)


@facturas_bp.route("", methods=["GET"])
def listar_facturas():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    try:
        return jsonify({"ok": True, **facturas.listar(CLIENTE_APP, request.args.get("propiedad_id"), request.args.get("mes"), _es_pin_admin())})
    except facturas.FacturaError as e:
        return _error(e)

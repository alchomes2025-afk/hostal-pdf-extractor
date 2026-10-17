"""
mobile_facturas.py — Facturas desde la app de gestión (pestaña Finanzas): leer, revisar y guardar en el Drive del
cliente, y listar las del mes. La lógica vive en services/facturas.py; aquí solo el PIN y el formato HTTP.

Mismas reglas de PIN que /mobile/finance: con el PIN normal solo las propiedades que no son solo_admin (La Casa de la
Primavera); con el de administración, todas. La app de gestión actual es de un único cliente: ALC Homes.

  GET  /mobile/facturas/config                         → propiedades visibles, categorías y si está habilitado
  POST /mobile/facturas/leer     {propiedad_id, archivo:{nombre,mime,base64}}            → propuesta para revisar
  POST /mobile/facturas/guardar  {propiedad_id, archivo, datos, usuario}                 → guardada en el Drive
  GET  /mobile/facturas?propiedad_id=349341&mes=2026-10                                  → facturas y totales del mes
"""
import logging

from flask import Blueprint, jsonify, request

from mobile_routes import _es_pin_admin, check_pin
from services import facturas

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
                    "propiedades": [{"id": k, "nombre": v} for k, v in props.items()], "categorias": facturas.CATEGORIAS})


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


@facturas_bp.route("", methods=["GET"])
def listar_facturas():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    try:
        return jsonify({"ok": True, **facturas.listar(CLIENTE_APP, request.args.get("propiedad_id"), request.args.get("mes"), _es_pin_admin())})
    except facturas.FacturaError as e:
        return _error(e)

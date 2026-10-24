"""
mobile_historial.py — «Historial de sustitución» (solo el propietario, 1379): todo lo que ha hecho quien sustituye a Ione, con fecha y hora. Ver services/historial_sustitucion.py.

  GET  /mobile/historial-sustitucion?sustituto=&dia=AAAA-MM-DD&tipo=&limite=   → eventos (del más reciente al más antiguo), los sustitutos conocidos y los tipos
  POST /mobile/historial-sustitucion/preguntar   {pregunta}                    → respuesta del asistente del historial («¿qué hizo ayer?»)
"""
import logging
import re

from flask import Blueprint, jsonify, request

from mobile_routes import exigir_permiso
from services import entrevista, historial_ia
from services import historial_sustitucion as historial
from services.limites import ip_cliente, permitir

logger = logging.getLogger(__name__)
sustitucion_bp = Blueprint("historial_sustitucion", __name__, url_prefix="/mobile/historial-sustitucion")
sustitucion_bp.before_request(exigir_permiso)
_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@sustitucion_bp.route("", methods=["GET"])
def eventos():
    dia = request.args.get("dia", "")
    tipo = request.args.get("tipo", "")
    try:
        limite = max(1, min(int(request.args.get("limite", 300)), 1000))
    except ValueError:
        limite = 300
    lista = historial.listar(sustituto=request.args.get("sustituto") or None, dia=dia if _FECHA.match(dia) else None, tipo=tipo if tipo in historial.TIPOS else None, limite=limite)
    return jsonify({"ok": True, "eventos": lista, "sustitutos": historial.sustitutos(), "tipos": historial.TIPOS})


@sustitucion_bp.route("/preguntar", methods=["POST"])
def preguntar():
    if not permitir([(f"historial-ia:{ip_cliente()}", 60, 3600)]):
        return jsonify({"ok": False, "error": "Demasiadas preguntas seguidas. Espera un poco."}), 429
    try:
        return jsonify({"ok": True, **historial_ia.preguntar((request.get_json(silent=True) or {}).get("pregunta"))})
    except entrevista.ErrorEntrevista as e:
        return jsonify({"ok": False, "error": str(e)}), e.status

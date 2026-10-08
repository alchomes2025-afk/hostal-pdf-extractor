"""
routes/rebotes.py — Entrada de los correos devueltos (rebotes) que detecta el script de Apps Script del
buzón de huéspedes, y consulta de los registrados. Ver services/rebotes.py.
"""
import hmac
import logging

from flask import Blueprint, jsonify, request

from config import API_TOKEN, APPS_SCRIPT_EMAIL_SECRET, TEST_TOKEN
from services.rebotes import procesar_rebotes, ultimos_rebotes

logger = logging.getLogger(__name__)
rebotes_bp = Blueprint("rebotes", __name__)


def _iguales(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


@rebotes_bp.route("/rebote", methods=["POST"])
def recibir_rebotes():
    """
    Lo llama el script de Apps Script del buzón de huéspedes con los rebotes nuevos. Se autentica con el mismo
    secreto compartido que usa el backend para pedirle enviar emails (APPS_SCRIPT_EMAIL_SECRET).

    POST { "secret": "...", "mensajes": [{"id", "fecha", "de", "asunto", "cuerpo"}, ...] }
    → { "ok": true, "nuevos": n, "ignorados_booking": n, "repetidos": n }
    """
    data = request.get_json(silent=True) or {}
    if not _iguales(data.get("secret"), APPS_SCRIPT_EMAIL_SECRET):
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    mensajes = data.get("mensajes")
    if not isinstance(mensajes, list):
        return jsonify({"ok": False, "error": "Falta la lista «mensajes»"}), 400
    try:
        return jsonify({"ok": True, **procesar_rebotes(mensajes)})
    except Exception as e:
        logger.error(f"[rebotes] Error procesando rebotes: {e}")
        return jsonify({"ok": False, "error": "No se pudieron procesar los rebotes"}), 500


@rebotes_bp.route("/rebotes", methods=["GET"])
def listar_rebotes():
    """Constancia: los últimos rebotes registrados (no de Booking). GET /rebotes?token=<TOKEN>[&limite=30]"""
    token = request.args.get("token", "")
    if not any(_iguales(token, t) for t in (API_TOKEN, TEST_TOKEN)):
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    try:
        limite = max(1, min(int(request.args.get("limite", 30)), 100))
    except ValueError:
        limite = 30
    try:
        return jsonify({"ok": True, "rebotes": ultimos_rebotes(limite)})
    except Exception as e:
        logger.error(f"[rebotes] Error leyendo los rebotes: {e}")
        return jsonify({"ok": False, "error": "No se pudieron leer los rebotes"}), 500

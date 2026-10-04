"""
routes/misc.py — Health check.
"""
import os

from flask import Blueprint, jsonify

from config import CALLMEBOT_PHONE, CALLMEBOT_API_KEY

misc_bp = Blueprint("misc", __name__)


@misc_bp.route("/", methods=["GET"])
def health():
    callmebot_ok = bool(CALLMEBOT_PHONE and CALLMEBOT_API_KEY)
    return jsonify({
        "status": "ok",
        "servicio": "Hostal PDF Extractor",
        "callmebot_configurado": callmebot_ok,
        # Commit desplegado (variable que pone Render): sirve para comprobar
        # que un despliegue ya está activo sin llamar a APIs externas.
        "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
    }), 200

"""
routes/rebotes.py — Entrada de los correos devueltos (rebotes) que detecta el script de Apps Script del
buzón de huéspedes, y consulta de los registrados. Ver services/rebotes.py.
"""
import hmac
import logging

from flask import Blueprint, jsonify, request

from config import API_TOKEN, APPS_SCRIPT_EMAIL_SECRET, TEST_TOKEN
from services import rpv_webhook
from services.rebotes import procesar_rebotes, ultimos_rebotes
from services.reservas_correo import recibir_avisos, ultimos_avisos

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


@rebotes_bp.route("/reserva-correo", methods=["POST"])
def recibir_avisos_reserva():
    """
    Lo llama el script de Apps Script de alchomes2025@gmail.com con los avisos de reserva nueva/modificada/cancelada que
    llegan por correo. Solo los registra; el watchdog los coteja con Beds24 (services/reservas_correo.py). Mismo secreto
    compartido que /rebote.

    POST { "secret": "...", "mensajes": [{"id", "fecha", "de", "asunto", "cuerpo"}, ...] }
    → { "ok": true, "nuevos": n, "sin_entender": n, "repetidos": n }
    """
    data = request.get_json(silent=True) or {}
    if not _iguales(data.get("secret"), APPS_SCRIPT_EMAIL_SECRET):
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    mensajes = data.get("mensajes")
    if not isinstance(mensajes, list):
        return jsonify({"ok": False, "error": "Falta la lista «mensajes»"}), 400
    try:
        return jsonify({"ok": True, **recibir_avisos(mensajes)})
    except Exception as e:
        logger.error(f"[reservas_correo] Error registrando avisos: {e}")
        return jsonify({"ok": False, "error": "No se pudieron registrar los avisos"}), 500


@rebotes_bp.route("/reservas-correo", methods=["GET"])
def listar_avisos_reserva():
    """Constancia: los últimos avisos de reserva recibidos por correo y su estado. GET /reservas-correo?token=<TOKEN>[&limite=40]"""
    token = request.args.get("token", "")
    if not any(_iguales(token, t) for t in (API_TOKEN, TEST_TOKEN)):
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    try:
        limite = max(1, min(int(request.args.get("limite", 40)), 100))
    except ValueError:
        limite = 40
    try:
        return jsonify({"ok": True, "avisos": ultimos_avisos(limite)})
    except Exception as e:
        logger.error(f"[reservas_correo] Error leyendo los avisos: {e}")
        return jsonify({"ok": False, "error": "No se pudieron leer los avisos"}), 500


@rebotes_bp.route("/webhook-rpv", methods=["POST"])
def webhook_rpv():
    """
    Aviso de RPV (vía Svix) de que el parte de una reserva está completo: guest_registration.completed. Va firmado
    (cabeceras svix-*); sin firma válida, 401. El aviso solo provoca una consulta fresca a RPV (services/rpv_webhook.py).
    """
    cuerpo = request.get_data()
    try:
        rpv_webhook.verificar_firma(cuerpo, request.headers)
    except rpv_webhook.FirmaInvalida as e:
        logger.warning(f"[webhook-rpv] Aviso rechazado: {e}")
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    evento = request.get_json(silent=True)
    if not isinstance(evento, dict):
        return jsonify({"ok": False, "error": "Cuerpo no válido"}), 400
    try:
        return jsonify({"ok": True, **rpv_webhook.procesar(evento)})
    except Exception as e:
        logger.error(f"[webhook-rpv] Error procesando el aviso: {e}")
        return jsonify({"ok": False, "error": "Error interno"}), 500


@rebotes_bp.route("/webhook-rpv/ultimos", methods=["GET"])
def webhook_rpv_ultimos():
    """Constancia de los últimos avisos de RPV (en memoria, sin datos personales). GET /webhook-rpv/ultimos?token=<TOKEN>"""
    token = request.args.get("token", "")
    if not any(_iguales(token, t) for t in (API_TOKEN, TEST_TOKEN)):
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    return jsonify({"ok": True, "secretos_configurados": sorted(rpv_webhook._secretos()), "avisos": rpv_webhook.ultimos()})

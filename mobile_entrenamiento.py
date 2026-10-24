"""
mobile_entrenamiento.py — «Entrenamiento» (solo el propietario, 1379): entrevista guiada a Ione para llenar la base de conocimiento. Ver services/entrevista.py.

  GET  /mobile/entrenamiento/temas             → los temas con lo recogido de cada uno (borradores y aprobadas)
  GET  /mobile/entrenamiento/sesion?tema=      → la última conversación de ese tema (para seguirla), si existe
  POST /mobile/entrenamiento/mensaje           {tema, texto, sesion_id?} → siguiente pregunta (+ lo que se haya guardado como borrador). `texto` vacío = empezar o retomar

Lo aprendido se guarda siempre como BORRADOR; se revisa y se aprueba en Operativa de Gestión → Base de conocimiento.
"""
import logging

from flask import Blueprint, jsonify, request

from mobile_routes import exigir_permiso, nombre_actual, perfil_actual
from services import entrevista
from services.limites import ip_cliente, permitir

logger = logging.getLogger(__name__)
entrenamiento_bp = Blueprint("entrenamiento", __name__, url_prefix="/mobile/entrenamiento")
entrenamiento_bp.before_request(exigir_permiso)


def _error(e):
    return jsonify({"ok": False, "error": str(e)}), getattr(e, "status", 400)


@entrenamiento_bp.route("/temas", methods=["GET"])
def temas():
    try:
        return jsonify({"ok": True, "temas": entrevista.resumen_temas()})
    except entrevista.ErrorEntrevista as e:
        return _error(e)


@entrenamiento_bp.route("/sesion", methods=["GET"])
def sesion():
    tema = request.args.get("tema", "")
    if tema not in entrevista.TEMAS_POR_ID:
        return jsonify({"ok": False, "error": "Tema desconocido"}), 404
    try:
        s = entrevista.ultima_del_tema(tema)
    except entrevista.ErrorEntrevista as e:
        return _error(e)
    if not s:
        return jsonify({"ok": True, "sesion": None})
    return jsonify({"ok": True, "sesion": {"id": s["id"], "tema": tema, "mensajes": s.get("mensajes") or [], "cubierto": bool(s.get("cubierto"))}})


@entrenamiento_bp.route("/mensaje", methods=["POST"])
def mensaje():
    if not permitir([(f"entrenamiento:{ip_cliente()}", 120, 3600)]):
        return jsonify({"ok": False, "error": "Demasiados mensajes seguidos. Espera un poco."}), 429
    datos = request.get_json(silent=True) or {}
    try:
        r = entrevista.turno(datos.get("tema"), datos.get("texto"), sesion_id=datos.get("sesion_id"), usuario=nombre_actual() or perfil_actual().nombre)
    except entrevista.ErrorEntrevista as e:
        return _error(e)
    return jsonify({"ok": True, **r})

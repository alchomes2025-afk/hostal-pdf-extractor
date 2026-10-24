"""
mobile_operativa.py — Pestaña «Operativa de Gestión» (la ven el propietario 1379 y el sustituto 2589) y edición de la base de conocimiento (solo el propietario).

  GET  /mobile/operativa/indice          (operativa.ver)  → lo aprobado agrupado por tipo: tareas diarias, teléfonos, preguntas frecuentes, escalado, otros procesos
  GET  /mobile/operativa/entrada?id=     (operativa.ver)  → una entrada aprobada completa (pasos, qué comprobar, a quién avisar…)
  GET  /mobile/operativa/kb              (operativa.entrenamiento) → TODAS las entradas, con su estado (borrador/aprobado)
  POST /mobile/operativa/kb              (operativa.entrenamiento) {id?, tipo, titulo, contenido, etiquetas, orden, estado} → crea o actualiza
  POST /mobile/operativa/kb/borrar       (operativa.entrenamiento) {id}
  POST /mobile/operativa/kb/importar-faq (operativa.entrenamiento) → carga como borradores las preguntas frecuentes de la web de check-in

El asistente solo usa las entradas APROBADAS (services/operativa_kb.py). El permiso de cada ruta está en rutas_permisos.py.
"""
import logging

from flask import Blueprint, jsonify, request

from mobile_routes import exigir_permiso, nombre_actual, perfil_actual, tiene_permiso
from services import accesos
from services import operativa_kb as kb

logger = logging.getLogger(__name__)
operativa_bp = Blueprint("operativa", __name__, url_prefix="/mobile/operativa")
operativa_bp.before_request(exigir_permiso)


def _error(e):
    return jsonify({"ok": False, "error": str(e)}), getattr(e, "status", 400)


@operativa_bp.route("/indice", methods=["GET"])
def indice():
    return jsonify({"ok": True, "grupos": kb.indice(primavera=tiene_permiso(accesos.PRIMAVERA))})


@operativa_bp.route("/entrada", methods=["GET"])
def entrada():
    e = kb.una(request.args.get("id"), solo_aprobada=True, primavera=tiene_permiso(accesos.PRIMAVERA))
    if not e:
        return jsonify({"ok": False, "error": "No se encuentra esa entrada"}), 404
    return jsonify({"ok": True, "entrada": {k: e.get(k) for k in ("id", "tipo", "titulo", "contenido", "etiquetas")}})


@operativa_bp.route("/kb", methods=["GET"])
def kb_listar():
    return jsonify({"ok": True, "entradas": kb.entradas(None, forzar=True), "tipos": list(kb.TIPOS)})


@operativa_bp.route("/kb", methods=["POST"])
def kb_guardar():
    datos = request.get_json(silent=True) or {}
    try:
        doc_id = kb.guardar(datos, usuario=nombre_actual() or perfil_actual().nombre)
    except kb.ErrorKB as e:
        return _error(e)
    logger.info(f"[operativa] Entrada {doc_id} guardada por {nombre_actual() or perfil_actual().id}")
    return jsonify({"ok": True, "id": doc_id})


@operativa_bp.route("/kb/importar-faq", methods=["POST"])
def kb_importar_faq():
    try:
        resultado = kb.importar_faq(usuario=nombre_actual() or perfil_actual().nombre)
    except kb.ErrorKB as e:
        return _error(e)
    return jsonify({"ok": True, **resultado})


@operativa_bp.route("/kb/borrar", methods=["POST"])
def kb_borrar():
    try:
        kb.borrar((request.get_json(silent=True) or {}).get("id"))
    except kb.ErrorKB as e:
        return _error(e)
    return jsonify({"ok": True})

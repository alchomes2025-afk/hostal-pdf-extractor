"""
routes/historial.py — Histórico de interacciones y conversaciones por
reserva (Firestore): /log-event, /historial, /historial/<ref>.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Blueprint, request, jsonify

import config
from config import API_TOKEN, TEST_TOKEN

logger = logging.getLogger(__name__)
historial_bp = Blueprint("historial", __name__)


@historial_bp.route("/log-event", methods=["POST"])
def log_event():
    """
    Registra un evento de interacción o mensaje de chat asociado a una reserva.
    Llamado desde la web de check-in (index.html) en cada paso relevante:
    apertura de la pantalla, cambio de estado detectado por el autopoll,
    y cada mensaje enviado o recibido en el asistente virtual.

    POST /log-event
    Body: {
      "ref": "9999000001",
      "room_id": "702398",
      "room_name": "Playa del Albir",
      "arrival": "2026-07-20",
      "departure": "2026-07-22",
      "event_type": "page_view" | "state_changed" | "chat_user" | "chat_bot" | "error",
      "content": "texto libre (mensaje de chat, estado, error, etc.)",
      "state": "staying" (estado del check-in en el momento del evento)
    }

    Endpoint público (sin token) — es llamado directamente desde el navegador
    del huésped, igual que /chat. Nunca debe bloquear ni ralentizar la
    experiencia del huésped: cualquier fallo de Firestore se traga en
    silencio y responde 200 igualmente.
    """
    if config.db is None:
        return jsonify({"ok": False, "error": "Firestore no configurado"}), 200

    data = request.get_json(force=True) or {}
    ref = str(data.get("ref", "")).strip()
    if not ref:
        return jsonify({"ok": False, "error": "ref requerido"}), 400

    event = {
        "type":      data.get("event_type", "unknown"),
        "content":   str(data.get("content", ""))[:4000],  # límite razonable
        "state":     data.get("state", ""),
        "timestamp": datetime.now(ZoneInfo('Europe/Madrid')).isoformat(),
    }

    try:
        doc_ref = config.db.collection("interactions").document(ref)
        doc_ref.set({
            "booking_ref":    ref,
            "room_id":        data.get("room_id", ""),
            "room_name":      data.get("room_name", ""),
            "arrival":        data.get("arrival", ""),
            "departure":      data.get("departure", ""),
            "last_activity":  config.firestore.SERVER_TIMESTAMP,
            "events":         config.firestore.ArrayUnion([event]),
        }, merge=True)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"[log-event] Error escribiendo en Firestore: {e}")
        return jsonify({"ok": False, "error": str(e)}), 200


@historial_bp.route("/historial", methods=["GET"])
def historial_lista():
    """
    Devuelve el listado de reservas con interacción registrada,
    ordenadas de la más reciente a la más antigua.

    GET /historial?token=Alchomes2025
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    if config.db is None:
        return jsonify({"ok": False, "error": "Firestore no configurado"}), 500

    try:
        docs = (
            config.db.collection("interactions")
            .order_by("last_activity", direction=config.firestore.Query.DESCENDING)
            .limit(300)
            .stream()
        )
        resultado = []
        for d in docs:
            item = d.to_dict()
            events = item.get("events", [])
            last_activity = item.get("last_activity")
            resultado.append({
                "ref":           item.get("booking_ref", d.id),
                "room_name":     item.get("room_name", ""),
                "arrival":       item.get("arrival", ""),
                "departure":     item.get("departure", ""),
                "last_activity": last_activity.isoformat() if last_activity else None,
                "n_eventos":     len(events),
                "n_mensajes":    sum(1 for e in events if e.get("type") in ("chat_user", "chat_bot")),
                "ultimo_estado": events[-1].get("state", "") if events else "",
            })
        return jsonify({"ok": True, "reservas": resultado})
    except Exception as e:
        logger.error(f"[historial] Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@historial_bp.route("/historial/<ref>", methods=["GET"])
def historial_detalle(ref):
    """
    Devuelve el histórico completo de eventos y mensajes de una reserva.

    GET /historial/9999000001?token=Alchomes2025
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    if config.db is None:
        return jsonify({"ok": False, "error": "Firestore no configurado"}), 500

    try:
        doc = config.db.collection("interactions").document(ref).get()
        if not doc.exists:
            return jsonify({"ok": False, "error": "Reserva no encontrada"}), 404
        item = doc.to_dict()
        events = sorted(item.get("events", []), key=lambda e: e.get("timestamp", ""))
        return jsonify({
            "ok":         True,
            "ref":        item.get("booking_ref", ref),
            "room_name":  item.get("room_name", ""),
            "arrival":    item.get("arrival", ""),
            "departure":  item.get("departure", ""),
            "events":     events,
        })
    except Exception as e:
        logger.error(f"[historial/{ref}] Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

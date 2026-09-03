"""
routes/chat.py — Proxy a Groq para el asistente virtual de la web de check-in.
"""
import logging
import requests
from flask import Blueprint, request, jsonify

from config import GROQ_API_KEY, GROQ_API_URL, GROQ_MODEL_PRI, GROQ_MODEL_FALL

logger = logging.getLogger(__name__)
chat_bp = Blueprint("chat", __name__)


@chat_bp.route("/chat", methods=["POST"])
def chat_proxy():
    """
    POST /chat
    Body: { "system": "...", "messages": [...] }

    Proxy a Groq para el asistente virtual de la web de check-in Firebase.
    La clave GROQ_API_KEY permanece en Render (nunca expuesta en el HTML ni en GitHub).
    Intenta primero con el modelo principal; si falla (429/503) cae al modelo de respaldo.
    """
    if not GROQ_API_KEY:
        return jsonify({"ok": False, "error": "GROQ_API_KEY no configurada en Render"}), 500

    data     = request.get_json(force=True) or {}
    messages = data.get("messages", [])
    system   = data.get("system", "")

    if system:
        messages = [{"role": "system", "content": system}] + messages

    def llamar_groq(model):
        return requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": messages,
                  "max_tokens": 600, "temperature": 0.3},
            timeout=30,
        )

    try:
        resp = llamar_groq(GROQ_MODEL_PRI)
        if resp.status_code in (429, 503):
            logger.warning(f"[chat] {GROQ_MODEL_PRI} → {resp.status_code}, probando fallback")
            resp = llamar_groq(GROQ_MODEL_FALL)
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"].strip()
        return jsonify({"ok": True, "reply": reply})
    except Exception as e:
        logger.error(f"[chat] Error Groq: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

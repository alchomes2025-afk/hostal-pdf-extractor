"""
app.py — Punto de entrada del backend Flask (hostal-pdf-extractor).

Este archivo solo crea la app, registra los blueprints y arranca el
check de startup. Toda la lógica vive en config.py, services/ y routes/
(ver claude.md para el mapa completo del proyecto).

Render (Procfile) apunta a `gunicorn app:app`, así que este módulo debe
seguir exponiendo una variable `app` a nivel de módulo.
"""
import os
import logging
from flask import Flask
from flask_cors import CORS

from mobile_routes import mobile_bp

from config import (
    BEDS24_REFRESH_TOKEN, RPV_API_KEY, CALLMEBOT_PHONE, CALLMEBOT_API_KEY, GROQ_API_KEY,
)
from services.whatsapp import enviar_whatsapp_callmebot

from routes.misc import misc_bp
from routes.chat import chat_bp
from routes.checkin import checkin_bp
from routes.oauth import oauth_bp
from routes.extraccion import extraccion_bp
from routes.resumen_routes import resumen_bp
from routes.historial import historial_bp
from routes.watchdog import watchdog_bp
from routes.debug_diag import debug_bp

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.register_blueprint(mobile_bp)
for _bp in (
    misc_bp, chat_bp, checkin_bp, oauth_bp, extraccion_bp,
    resumen_bp, historial_bp, watchdog_bp, debug_bp,
):
    app.register_blueprint(_bp)
CORS(app)


# ── Check de startup ──────────────────────────────────────────────────────
# Se ejecuta al cargar el módulo (cuando Render arranca el servidor).
# Verifica variables críticas y avisa por WhatsApp si falta algo.
def _startup_check():
    criticas = {
        "BEDS24_REFRESH_TOKEN": BEDS24_REFRESH_TOKEN,
        "RPV_API_KEY":          RPV_API_KEY,
        "CALLMEBOT_PHONE":      CALLMEBOT_PHONE,
        "CALLMEBOT_API_KEY":    CALLMEBOT_API_KEY,
        "GROQ_API_KEY":         GROQ_API_KEY,
    }
    faltantes = [k for k, v in criticas.items() if not v]
    if faltantes:
        logger.error(f"[STARTUP] ⚠️ Variables críticas no configuradas: {faltantes}")
        try:
            enviar_whatsapp_callmebot(
                f"🟡 ALCHOMES — Render reiniciado\n\n"
                f"⚠️ Variables críticas no configuradas:\n"
                + "\n".join(f"• {k}" for k in faltantes)
                + "\n\nAcceder a Render → Environment para añadirlas."
            )
        except Exception as e:
            logger.error(f"[STARTUP] No se pudo enviar alerta WhatsApp: {e}")
    else:
        logger.info("[STARTUP] ✅ Todas las variables críticas configuradas")


_startup_check()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

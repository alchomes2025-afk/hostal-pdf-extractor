"""
routes/oauth.py — Flujo OAuth de Gmail (legacy, se conserva para poder
renovar GOOGLE_REFRESH_TOKEN cuando /extraer o /resumen lo necesiten).
"""
import json
import logging
import requests
from urllib.parse import urlencode
from flask import Blueprint, request, redirect

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, REDIRECT_URI

logger = logging.getLogger(__name__)
oauth_bp = Blueprint("oauth", __name__)


@oauth_bp.route("/oauth/inicio", methods=["GET"])
def oauth_inicio():
    """Redirige a Google para autorizar acceso a Gmail."""
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         "https://www.googleapis.com/auth/gmail.readonly",
        "access_type":   "offline",
        "prompt":        "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return redirect(url)


@oauth_bp.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Google redirige aquí con el código de autorización."""
    code  = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"<h2>Error: {error}</h2>", 400
    if not code:
        return "<h2>No se recibió código de autorización</h2>", 400
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=15)
    tokens = resp.json()
    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        return f"<h2>Error obteniendo refresh_token</h2><pre>{json.dumps(tokens, indent=2)}</pre>", 400
    return f"""<h2>✅ Autorización completada</h2>
    <p>Copia este valor y añádelo en Render como variable de entorno:</p>
    <p><strong>GOOGLE_REFRESH_TOKEN</strong></p>
    <pre style="background:#f0f0f0;padding:1rem;border-radius:8px;word-break:break-all">{refresh_token}</pre>
    <p>Una vez guardado en Render, el servidor podrá acceder a Gmail automáticamente.</p>"""

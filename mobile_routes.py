"""
mobile_routes.py
-----------------
Blueprint de Flask con las rutas para la app móvil de gestión de
precios/disponibilidad. Totalmente aislado del resto de app.py:
solo hace falta importarlo y registrarlo (ver instrucciones al final).

Reutiliza las variables de entorno que YA tienes configuradas en Render:
  - BEDS24_REFRESH_TOKEN
  - BEDS24_PROPERTY_ID

Añade UNA variable de entorno nueva en Render:
  - MOBILE_APP_PIN   (ej. "4821", el PIN de 4 dígitos que tú elijas)
"""

import os
import time
import requests
from flask import Blueprint, request, jsonify

mobile_bp = Blueprint("mobile", __name__, url_prefix="/mobile")

BEDS24_API = "https://beds24.com/api/v2"
BEDS24_REFRESH_TOKEN = os.environ.get("MOBILE_BEDS24_TOKEN") or os.environ.get("BEDS24_REFRESH_TOKEN")
MOBILE_APP_PIN = os.environ.get("MOBILE_APP_PIN")

ROOMS = [
    {"id": 702395, "name": "Deluxe Room"},
    {"id": 702396, "name": "Double Room"},
    {"id": 702397, "name": "Standard Queen Room"},
    {"id": 702398, "name": "Superior Queen Room"},
    {"id": 702399, "name": "Queen Room"},
]

# --- Caché simple del access token en memoria del proceso -----------------
_token_cache = {"token": None, "expires_at": 0}


def get_access_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    resp = requests.get(
        f"{BEDS24_API}/authentication/token",
        headers={"accept": "application/json", "refreshToken": BEDS24_REFRESH_TOKEN},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["token"]
    _token_cache["expires_at"] = now + data.get("expiresIn", 3600) - 60
    return _token_cache["token"]


def check_pin():
    pin = request.headers.get("x-app-pin") or (request.json or {}).get("pin") if request.is_json else None
    if pin is None:
        pin = request.args.get("pin")
    if not MOBILE_APP_PIN:
        return False
    return str(pin) == str(MOBILE_APP_PIN)


# --- Endpoints --------------------------------------------------------------

@mobile_bp.route("/login", methods=["POST"])
def mobile_login():
    if check_pin():
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "PIN incorrecto"}), 401


@mobile_bp.route("/rooms", methods=["GET"])
def mobile_rooms():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    return jsonify({"ok": True, "rooms": ROOMS})


@mobile_bp.route("/calendar", methods=["GET"])
def mobile_calendar():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401

    room_id = request.args.get("roomId")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    if not (room_id and date_from and date_to):
        return jsonify({"ok": False, "error": "Faltan parámetros roomId, from o to"}), 400

    try:
        token = get_access_token()
        resp = requests.get(
            f"{BEDS24_API}/inventory/rooms/calendar",
            params={"roomId": room_id, "startDate": date_from, "endDate": date_to, "includeAllDates": "true"},
            headers={"accept": "application/json", "token": token},
            timeout=20,
        )
        data = resp.json()
        if not resp.ok:
            return jsonify({"ok": False, "error": data}), resp.status_code
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@mobile_bp.route("/update", methods=["POST"])
def mobile_update():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401

    body = request.get_json(force=True, silent=True) or {}
    room_id = body.get("roomId")
    date_from = body.get("from")
    date_to = body.get("to")
    price1 = body.get("price1")
    num_avail = body.get("numAvail")

    if not (room_id and date_from and date_to):
        return jsonify({"ok": False, "error": "Faltan parámetros roomId, from o to"}), 400
    if price1 is None and num_avail is None:
        return jsonify({"ok": False, "error": "Debes enviar price1 y/o numAvail"}), 400

    calendar_entry = {"from": date_from, "to": date_to}
    if price1 is not None:
        calendar_entry["price1"] = float(price1)
    if num_avail is not None:
        calendar_entry["numAvail"] = int(num_avail)

    try:
        token = get_access_token()
        resp = requests.post(
            f"{BEDS24_API}/inventory/rooms/calendar",
            json=[{"roomId": int(room_id), "calendar": [calendar_entry]}],
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "token": token,
            },
            timeout=20,
        )
        data = resp.json()
        if not resp.ok:
            return jsonify({"ok": False, "error": data}), resp.status_code
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@mobile_bp.route("/setup-token", methods=["GET", "POST"])
def setup_token():
    """Endpoint temporal para canjear un invite code de Beds24 por un refresh token real."""
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        try:
            resp = requests.get(
                f"{BEDS24_API}/authentication/setup",
                headers={"accept": "application/json", "code": code},
                timeout=15,
            )
            return jsonify(resp.json())
        except Exception as e:
            return jsonify({"error": str(e)})
    return """<!DOCTYPE html>
<html><body style="font-family:sans-serif;padding:20px">
<h2>Canjear Invite Code de Beds24</h2>
<form method="POST">
  <label>Pega el invite code aquí:</label><br><br>
  <textarea name="code" rows="5" cols="60" style="font-size:13px"></textarea><br><br>
  <button type="submit" style="padding:10px 20px;font-size:16px">Canjear → obtener Refresh Token</button>
</form>
</body></html>"""

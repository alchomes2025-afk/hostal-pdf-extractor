"""
mobile_routes.py  v2
--------------------
Blueprint Flask para la app móvil de gestión de Beds24.
Endpoints:
  GET  /mobile/login          - valida PIN
  GET  /mobile/rooms          - lista habitaciones
  GET  /mobile/calendar       - reservas + overrides de precio del mes
  POST /mobile/update         - actualiza precio/disponibilidad de un día
  GET|POST /mobile/setup-token - canjea invite code por refresh token (setup)

Variables de entorno necesarias en Render:
  MOBILE_BEDS24_TOKEN  -> refresh token con scopes read/write inventory + bookings
  MOBILE_APP_PIN       -> PIN numérico de 4 dígitos para proteger la app
"""

import os
import time
import requests
from datetime import datetime, date, timedelta
from flask import Blueprint, request, jsonify

mobile_bp = Blueprint("mobile", __name__, url_prefix="/mobile")

BEDS24_API = "https://beds24.com/api/v2"
BEDS24_REFRESH_TOKEN = os.environ.get("MOBILE_BEDS24_TOKEN") or os.environ.get("BEDS24_REFRESH_TOKEN")
MOBILE_APP_PIN = os.environ.get("MOBILE_APP_PIN")

ROOMS = [
    {"id": 702395, "name": "Deluxe"},
    {"id": 702396, "name": "Doble"},
    {"id": 702397, "name": "Std Queen"},
    {"id": 702398, "name": "Sup Queen"},
    {"id": 702399, "name": "Queen"},
]
PROPERTY_ID = int(os.environ.get("BEDS24_PROPERTY_ID", "339751"))

# --- Caché de access token ------------------------------------------------
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
    pin = (
        request.headers.get("x-app-pin")
        or request.args.get("pin")
        or (request.get_json(silent=True) or {}).get("pin")
    )
    if not MOBILE_APP_PIN:
        return False
    return str(pin) == str(MOBILE_APP_PIN)


# --- Endpoints ------------------------------------------------------------

@mobile_bp.route("/login", methods=["POST", "GET"])
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
    """
    Devuelve para un roomId y mes (YYYY-MM):
      - bookings: lista de reservas con arrival, departure, guestName
      - prices: dict { "2026-08-01": { price1, price2, price3, numAvail } }
    """
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401

    room_id = request.args.get("roomId")
    month = request.args.get("month")  # YYYY-MM
    if not room_id or not month:
        return jsonify({"ok": False, "error": "Faltan roomId o month"}), 400

    try:
        year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    except Exception:
        return jsonify({"ok": False, "error": "month debe ser YYYY-MM"}), 400

    date_from = f"{year}-{mon:02d}-01"
    last_day = (date(year, mon, 1).replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    date_to = last_day.strftime("%Y-%m-%d")

    try:
        token = get_access_token()

        # 1. Reservas del mes para esta habitación
        bookings_resp = requests.get(
            f"{BEDS24_API}/bookings",
            params={
                "roomId": room_id,
                "arrivalFrom": date_from,
                "arrivalTo": date_to,
                "status": "confirmed",
            },
            headers={"accept": "application/json", "token": token},
            timeout=20,
        )
        bookings_data = bookings_resp.json()
        bookings = []
        for b in (bookings_data.get("data") or []):
            bookings.append({
                "arrival": b.get("arrival"),
                "departure": b.get("departure"),
                "guestName": b.get("guestFirstName", "") + " " + b.get("guestName", ""),
                "status": b.get("status"),
            })

        # 2. Overrides de precio/disponibilidad del mes
        cal_resp = requests.get(
            f"{BEDS24_API}/inventory/rooms/calendar",
            params={"roomId": room_id, "startDate": date_from, "endDate": date_to},
            headers={"accept": "application/json", "token": token},
            timeout=20,
        )
        cal_data = cal_resp.json()
        prices = {}
        for room_entry in (cal_data.get("data") or []):
            for day in (room_entry.get("calendar") or []):
                d = day.get("date")
                if d:
                    prices[d] = {
                        "price1": day.get("price1"),
                        "price2": day.get("price2"),
                        "price3": day.get("price3"),
                        "numAvail": day.get("numAvail"),
                    }

        return jsonify({"ok": True, "bookings": bookings, "prices": prices})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@mobile_bp.route("/update", methods=["POST"])
def mobile_update():
    """
    Body JSON:
    {
      "roomId": 702395,
      "from": "2026-08-10",
      "to": "2026-08-10",
      "price1": 140,       (opcional)
      "price2": 120,       (opcional)
      "price3": 100,       (opcional)
      "numAvail": 0        (opcional: 0=bloqueado, 1=disponible)
    }
    """
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401

    body = request.get_json(force=True, silent=True) or {}
    room_id = body.get("roomId")
    date_from = body.get("from")
    date_to = body.get("to")

    if not (room_id and date_from and date_to):
        return jsonify({"ok": False, "error": "Faltan roomId, from o to"}), 400

    entry = {"from": date_from, "to": date_to}
    for field in ["price1", "price2", "price3", "numAvail"]:
        if body.get(field) is not None:
            entry[field] = float(body[field]) if field.startswith("price") else int(body[field])

    if len(entry) == 2:
        return jsonify({"ok": False, "error": "Debes enviar al menos un campo a actualizar"}), 400

    try:
        token = get_access_token()
        resp = requests.post(
            f"{BEDS24_API}/inventory/rooms/calendar",
            json=[{"roomId": int(room_id), "calendar": [entry]}],
            headers={"content-type": "application/json", "accept": "application/json", "token": token},
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
    """Canjea un invite code de Beds24 por un refresh token permanente."""
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
  <label>Pega el invite code:</label><br><br>
  <textarea name="code" rows="5" cols="60"></textarea><br><br>
  <button type="submit" style="padding:10px 20px">Canjear</button>
</form></body></html>"""

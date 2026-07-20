"""
mobile_routes.py  v5
--------------------
Blueprint Flask para la app móvil de gestión de Beds24.

Novedades v5:
- Solo muestra price1 (Standard Rate) — price2/price3 las calcula Booking.com
- Precios reales: lee las price rules de GET /properties?includePriceRules=true
  y las aplica día a día. Sobrepone los overrides manuales del calendario.
- Reservas: busca 60 días antes del mes para capturar estancias en curso

Variables de entorno en Render:
  MOBILE_BEDS24_TOKEN  -> refresh token con scopes read/write inventory + bookings
  MOBILE_APP_PIN       -> PIN numérico de 4 dígitos
"""

import os
import time
import requests
from datetime import datetime, date, timedelta
from flask import Blueprint, request, jsonify

mobile_bp = Blueprint("mobile", __name__, url_prefix="/mobile")

BEDS24_API = "https://beds24.com/api/v2"
BEDS24_REFRESH_TOKEN = (
    os.environ.get("MOBILE_BEDS24_TOKEN") or os.environ.get("BEDS24_REFRESH_TOKEN")
)
MOBILE_APP_PIN = os.environ.get("MOBILE_APP_PIN")
PROPERTY_ID = os.environ.get("BEDS24_PROPERTY_ID", "339751")

ROOMS = [
    {"id": 702395, "name": "Deluxe"},
    {"id": 702396, "name": "Doble"},
    {"id": 702397, "name": "Std Queen"},
    {"id": 702398, "name": "Sup Queen"},
    {"id": 702399, "name": "Queen"},
]

# Caché del access token
_token_cache = {"token": None, "expires_at": 0}
# Caché de price rules (se invalida cada 6h)
_price_rules_cache = {"data": None, "expires_at": 0}


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


# ─── Price rules helpers ──────────────────────────────────────────────────────

def get_price_rules(token):
    """
    Obtiene las price rules de todas las habitaciones via GET /properties.
    Devuelve dict: { room_id_str: [ {from, to, days, price1}, ... ] }
    Se cachea durante 6 horas.
    """
    now = time.time()
    if _price_rules_cache["data"] and now < _price_rules_cache["expires_at"]:
        return _price_rules_cache["data"]

    try:
        resp = requests.get(
            f"{BEDS24_API}/properties",
            params={
                "propertyId": PROPERTY_ID,
                "includeAllRooms": "true",
                "includePriceRules": "true",
            },
            headers={"accept": "application/json", "token": token},
            timeout=20,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return {}

    rules_by_room = {}
    props = raw.get("data") or []
    if isinstance(props, dict):
        props = [props]

    for prop in props:
        rooms = prop.get("rooms") or []
        for room in rooms:
            rid = str(room.get("roomId", ""))
            if not rid:
                continue
            price_rules = room.get("priceRules") or room.get("pricerules") or []
            rules_by_room[rid] = price_rules

    _price_rules_cache["data"] = rules_by_room
    _price_rules_cache["expires_at"] = now + 6 * 3600
    return rules_by_room


def apply_price_rules(rules, date_str):
    """
    Aplica las price rules de un room a una fecha concreta.
    Devuelve el price1 calculado, o None si no aplica ninguna regla.
    Las reglas se evalúan en orden; gana la última que aplique (igual que Beds24).
    """
    if not rules:
        return None

    target = date.fromisoformat(date_str)
    dow = target.isoweekday()  # 1=lun … 7=dom
    # Mapeo Beds24 días: algunos usan "mon","tue"... otros 0-6
    DOW_MAP = {1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}
    dow_key = DOW_MAP[dow]

    result = None
    for rule in rules:
        # Rango de fechas (opcional)
        r_from = rule.get("from") or rule.get("dateFrom")
        r_to   = rule.get("to")   or rule.get("dateTo")
        if r_from and date_str < r_from:
            continue
        if r_to and date_str > r_to:
            continue

        # Días de la semana (opcional)
        days = rule.get("days") or rule.get("weekdays") or []
        if days:
            # puede ser lista ["mon","tue"...] o dict {mon:true, tue:false...}
            if isinstance(days, dict):
                if not days.get(dow_key, False):
                    continue
            elif isinstance(days, list):
                if dow_key not in [d.lower() for d in days]:
                    continue

        p = rule.get("price1") or rule.get("price") or rule.get("amount")
        if p is not None:
            try:
                result = float(p)
            except (ValueError, TypeError):
                pass

    return result


# ─── Endpoints ───────────────────────────────────────────────────────────────

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
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401

    room_id = request.args.get("roomId")
    month = request.args.get("month")
    if not room_id or not month:
        return jsonify({"ok": False, "error": "Faltan roomId o month"}), 400

    try:
        year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    except Exception:
        return jsonify({"ok": False, "error": "month debe ser YYYY-MM"}), 400

    month_start = date(year, mon, 1)
    last_day = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    date_from = month_start.strftime("%Y-%m-%d")
    date_to = last_day.strftime("%Y-%m-%d")
    search_from = (month_start - timedelta(days=60)).strftime("%Y-%m-%d")

    try:
        token = get_access_token()

        # ── 1. Reservas (desde 60 días antes para capturar estancias en curso) ──
        bookings_resp = requests.get(
            f"{BEDS24_API}/bookings",
            params={
                "roomId": room_id,
                "arrivalFrom": search_from,
                "arrivalTo": date_to,
                "includePersonalInfo": "true",
            },
            headers={"accept": "application/json", "token": token},
            timeout=20,
        )
        bookings_data = bookings_resp.json()
        bookings = []
        for b in (bookings_data.get("data") or []):
            if str(b.get("status", "")).lower() == "cancelled":
                continue
            arrival = b.get("arrival", "")
            departure = b.get("departure", "")
            if not arrival or not departure:
                continue
            if departure <= date_from or arrival > date_to:
                continue
            guest = b.get("guest") or {}
            first = (
                guest.get("firstName") or b.get("guestFirstName") or b.get("firstName") or ""
            )
            last = (
                guest.get("lastName") or b.get("guestLastName") or b.get("lastName") or ""
            )
            name = f"{first} {last}".strip() or "Huésped"
            bookings.append({
                "arrival": arrival,
                "departure": departure,
                "guestName": name,
                "status": b.get("status"),
            })

        # ── 2. Price rules del room (caché 6h) ──────────────────────────────
        all_rules = get_price_rules(token)
        room_rules = all_rules.get(str(room_id), [])

        # ── 3. Overrides manuales del calendario ────────────────────────────
        cal_resp = requests.get(
            f"{BEDS24_API}/inventory/rooms/calendar",
            params={"roomId": room_id, "startDate": date_from, "endDate": date_to},
            headers={"accept": "application/json", "token": token},
            timeout=20,
        )
        cal_raw = cal_resp.json()
        cal_rooms = cal_raw.get("data") or []
        if isinstance(cal_rooms, dict):
            cal_rooms = cal_rooms.get("data") or []

        overrides = {}
        for room_entry in cal_rooms:
            for day in (room_entry.get("calendar") or []):
                d = day.get("date")
                if d:
                    overrides[d] = {
                        "price1": day.get("price1"),
                        "numAvail": day.get("numAvail"),
                    }

        # ── 4. Construir precios diarios: rules + overrides ─────────────────
        prices = {}
        current = month_start
        while current <= last_day:
            ds = current.strftime("%Y-%m-%d")
            ov = overrides.get(ds, {})
            base_price = apply_price_rules(room_rules, ds)

            prices[ds] = {
                "price1": ov.get("price1") if ov.get("price1") is not None else base_price,
                "numAvail": ov.get("numAvail"),
                "isOverride": ov.get("price1") is not None,
            }
            current += timedelta(days=1)

        return jsonify({"ok": True, "bookings": bookings, "prices": prices})

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

    if not (room_id and date_from and date_to):
        return jsonify({"ok": False, "error": "Faltan roomId, from o to"}), 400

    entry = {"from": date_from, "to": date_to}
    if body.get("price1") is not None:
        entry["price1"] = float(body["price1"])
    if body.get("numAvail") is not None:
        entry["numAvail"] = int(body["numAvail"])

    if len(entry) == 2:
        return jsonify({"ok": False, "error": "Debes enviar price1 y/o numAvail"}), 400

    try:
        token = get_access_token()
        resp = requests.post(
            f"{BEDS24_API}/inventory/rooms/calendar",
            json=[{"roomId": int(room_id), "calendar": [entry]}],
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
        # Invalidar caché de price rules para que la siguiente carga sea fresca
        _price_rules_cache["expires_at"] = 0
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@mobile_bp.route("/setup-token", methods=["GET", "POST"])
def setup_token():
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

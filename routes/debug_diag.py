"""
routes/debug_diag.py — Endpoints de diagnóstico manual contra Beds24.
"""
import logging
import requests
from datetime import date, timedelta
from flask import Blueprint, request, jsonify

from config import API_TOKEN, TEST_TOKEN, BEDS24_API_BASE, BEDS24_PROPERTY_ID
from services.beds24 import get_beds24_access_token, _ref_en_booking

logger = logging.getLogger(__name__)
debug_bp = Blueprint("debug_diag", __name__)


@debug_bp.route("/ver-mensajes-beds24", methods=["GET"])
def ver_mensajes_beds24():
    """
    Diagnóstico: consulta directamente en Beds24 el historial de mensajes
    de una reserva concreta, para confirmar si un mensaje se registró/envió
    realmente (y ver el estado que reporta el canal).

    Uso:
        /ver-mensajes-beds24?token=Alchomes2025&book_id=89432182
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    book_id = request.args.get("book_id", "")
    if not book_id:
        return jsonify({"ok": False, "error": "Falta el parámetro book_id"}), 400

    try:
        access_token = get_beds24_access_token()
        resp = requests.get(
            f"{BEDS24_API_BASE}/bookings/messages",
            headers={"token": access_token, "accept": "application/json"},
            params={"bookingId": book_id},
            timeout=15,
        )
        resp.raise_for_status()
        return jsonify({"ok": True, "book_id": book_id, "mensajes": resp.json()}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@debug_bp.route("/ver-booking-completo", methods=["GET"])
def ver_booking_completo():
    """
    Diagnóstico: devuelve el JSON crudo COMPLETO de una reserva concreta
    (por su id de Beds24), tal cual lo entrega la API, sin resumir ni
    procesar ningún campo. Útil para ver exactamente qué contiene cada
    campo (apiReference, comments, etc.) carácter a carácter.

    Además, si se pasa el parámetro test_ref, ejecuta contra esa reserva
    la MISMA función de búsqueda recursiva que usa /check-in
    (_ref_en_booking) y dice en qué campo encontró coincidencia, o si no
    encontró ninguna — para depurar por qué un ref concreto falla o
    funciona sin tener que pasar por Make ni por la web.

    Uso:
        /ver-booking-completo?token=Alchomes2025&id=91615325
        /ver-booking-completo?token=Alchomes2025&id=91615325&test_ref=6166763556
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    booking_id = request.args.get("id", "")
    if not booking_id:
        return jsonify({"ok": False, "error": "Falta el parámetro id"}), 400

    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Beds24 auth: {e}"}), 500

    hoy = date.today()
    desde = (hoy - timedelta(days=5)).isoformat()
    hasta = (hoy + timedelta(days=180)).isoformat()

    try:
        resp = requests.get(
            f"{BEDS24_API_BASE}/bookings",
            headers={"token": access_token, "accept": "application/json"},
            params={"propertyId": BEDS24_PROPERTY_ID,
                    "arrivalFrom": desde, "arrivalTo": hasta},
            timeout=20,
        )
        resp.raise_for_status()
        bookings = resp.json().get("data", [])
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error consultando Beds24: {e}"}), 500

    booking = next((b for b in bookings if str(b.get("id")) == str(booking_id)), None)
    if not booking:
        return jsonify({"ok": False, "error": f"No se encontró ninguna reserva con id={booking_id} en el rango {desde}→{hasta}"}), 404

    resultado = {"ok": True, "booking_completo": booking}

    test_ref = request.args.get("test_ref", "")
    if test_ref:
        ref_norm = test_ref.strip().lower()
        campo = _ref_en_booking(booking, ref_norm)
        resultado["test_ref"] = test_ref
        resultado["encontrado_en_campo"] = campo
        resultado["match"] = campo is not None

    return jsonify(resultado), 200


@debug_bp.route("/ver-reservas-dia-beds24", methods=["GET"])
def ver_reservas_dia_beds24():
    """
    Diagnóstico: muestra la respuesta cruda de Beds24 al consultar reservas
    de un día concreto, para entender por qué el resumen devuelve resultados
    duplicados/excesivos (posible problema con el filtro de fechas o paginación).

    Uso:
        /ver-reservas-dia-beds24?token=Alchomes2025&fecha=2026-07-08&tipo=checkin
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    fecha = request.args.get("fecha", date.today().isoformat())
    tipo = request.args.get("tipo", "checkin")

    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    params = {"propertyId": BEDS24_PROPERTY_ID}
    if tipo == "checkin":
        params["checkInFrom"] = fecha
        params["checkInTo"] = fecha
    else:
        params["checkOutFrom"] = fecha
        params["checkOutTo"] = fecha

    try:
        resp = requests.get(
            f"{BEDS24_API_BASE}/bookings",
            headers={"token": access_token, "accept": "application/json"},
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    data = raw.get("data", [])
    resumen_items = [
        {
            "id": b.get("id"),
            "roomId": b.get("roomId"),
            "checkIn": b.get("arrival") or b.get("checkIn") or b.get("firstNight"),
            "checkOut": b.get("departure") or b.get("checkOut") or b.get("lastNight"),
            "status": b.get("status"),
        }
        for b in data
    ]

    return jsonify({
        "ok": True,
        "params_enviados": params,
        "total_items_data": len(data),
        "pages_info": raw.get("pages"),
        "primer_item_SIN_PROCESAR": data[0] if data else None,
        "primeros_10_items": resumen_items[:10],
        "todos_los_items_resumidos": resumen_items,
    }), 200

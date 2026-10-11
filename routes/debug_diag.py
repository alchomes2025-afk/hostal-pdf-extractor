"""
routes/debug_diag.py — Endpoints de diagnóstico manual contra Beds24.
"""
import logging
import requests
from datetime import timedelta
from flask import Blueprint, request, jsonify

from config import API_TOKEN, TEST_TOKEN, BEDS24_API_BASE, BEDS24_PROPERTY_ID
from services.acceso_enlace import crear_token, habilitado as enlaces_habilitados, verificar_token
from services.beds24 import buscar_booking_por_id, get_beds24_access_token, _ref_en_booking
from services.fechas import hoy_madrid
from services.rpv import diagnostico as diagnostico_rpv, diagnostico_pasado as diagnostico_rpv_pasado
from services import rpv_latencia
from services.rpv_sin_importar import candidatas_sin_importar

logger = logging.getLogger(__name__)
debug_bp = Blueprint("debug_diag", __name__)


@debug_bp.route("/ver-mensajes-beds24", methods=["GET"])
def ver_mensajes_beds24():
    """
    Diagnóstico: consulta directamente en Beds24 el historial de mensajes
    de una reserva concreta, para confirmar si un mensaje se registró/envió
    realmente (y ver el estado que reporta el canal).

    Uso:
        /ver-mensajes-beds24?token=<TOKEN>&book_id=89432182
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


@debug_bp.route("/diag-rpv", methods=["GET"])
def diag_rpv():
    """
    Diagnóstico de solo lectura del endpoint de partes de RPV: una consulta
    FRESCA a cada cuenta (2 llamadas) con un resumen por estado y la lista de
    partes (habitación, fechas, estado, huéspedes registrados/previstos). No
    contiene datos personales de los huéspedes.

    Uso:
        /diag-rpv?token=<TOKEN>               (producción)
        /diag-rpv?token=<TOKEN>&entorno=pre   (sandbox: datos ficticios)
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    entorno = "pre" if request.args.get("entorno") == "pre" else "real"
    return jsonify({"ok": True, "hoy": hoy_madrid().isoformat(), "entorno": entorno,
                    "cuentas": diagnostico_rpv(entorno)}), 200


@debug_bp.route("/diag-enlaces", methods=["GET"])
def diag_enlaces():
    """
    Diagnóstico de solo lectura de los enlaces personales de check-in: ¿está configurada la clave (CHECKIN_LINK_SECRET)
    y verifican bien los tokens? Con `id=<id de Beds24>` comprueba además que la búsqueda por id encuentra esa reserva
    (solo devuelve estado y fechas, sin datos personales). No expone la clave ni ningún token.

    Uso: /diag-enlaces?token=<TOKEN>[&id=94352000]
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    resultado = {"ok": True, "habilitado": enlaces_habilitados(),
                 "firma_verifica": verificar_token(crear_token("12345")) == "12345" if enlaces_habilitados() else None,
                 "manipulado_rechazado": (verificar_token((crear_token("12345") or "x.y")[:-2] + "AA") is None) if enlaces_habilitados() else None}
    book_id = request.args.get("id", "").strip()
    if book_id:
        b = buscar_booking_por_id(book_id)
        resultado["reserva"] = {"encontrada": bool(b), "estado": b.get("status") if b else None,
                                "llegada": b.get("arrival") if b else None, "salida": b.get("departure") if b else None}
    return jsonify(resultado), 200


@debug_bp.route("/diag-rpv-pasado", methods=["GET"])
def diag_rpv_pasado():
    """
    Diagnóstico de solo lectura: ¿admite el endpoint de partes de RPV fechas de entrada PASADAS? (hace falta para
    comprobar el parte de quien ya está alojado). Una consulta por cuenta; sin datos personales.

    Uso: /diag-rpv-pasado?token=<TOKEN>[&dias=3]
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    try:
        dias = max(1, min(int(request.args.get("dias", 3)), 30))
    except ValueError:
        dias = 3
    return jsonify({"ok": True, "hoy": hoy_madrid().isoformat(), "cuentas": diagnostico_rpv_pasado(dias)}), 200


@debug_bp.route("/diag-sin-importar", methods=["GET"])
def diag_sin_importar():
    """
    Diagnóstico de solo lectura: reservas que el aviso de "RPV no ha importado" avisaría
    ahora mismo (no consta en RPV pasadas HORAS_NORMALES_SIN_RPV h). No envía nada ni toca
    Firestore. Sin datos personales (habitación, canal, llegada, antigüedad).

    Uso: /diag-sin-importar?token=<TOKEN>
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    candidatas = [{k: v for k, v in c.items() if k != "huesped"} for c in candidatas_sin_importar()]
    return jsonify({"ok": True, "hoy": hoy_madrid().isoformat(), "candidatas": candidatas}), 200


@debug_bp.route("/diag-latencia-rpv", methods=["GET"])
def diag_latencia_rpv():
    """
    Diagnóstico de solo lectura: cuánto tarda RPV en recibir las reservas de Beds24 según las medidas del
    watchdog (services/rpv_latencia.py): estadísticas, espera típica y las últimas medidas. Sin datos personales.

    Uso: /diag-latencia-rpv?token=<TOKEN>
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    stats = rpv_latencia.estadisticas()
    tipica, fiable = rpv_latencia.espera_tipica_min(stats)
    return jsonify({"ok": True, "estadisticas": stats, "espera_tipica_min": tipica, "espera_tipica_fiable": fiable, **rpv_latencia.seguimiento()}), 200


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
    funciona sin tener que pasar por la web.

    Uso:
        /ver-booking-completo?token=<TOKEN>&id=91615325
        /ver-booking-completo?token=<TOKEN>&id=91615325&test_ref=6166763556
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

    hoy = hoy_madrid()
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
        /ver-reservas-dia-beds24?token=<TOKEN>&fecha=2026-07-08&tipo=checkin
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    fecha = request.args.get("fecha", hoy_madrid().isoformat())
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

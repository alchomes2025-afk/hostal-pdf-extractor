"""
mobile_limpieza.py — Incidencias de limpieza de La Casa de la Primavera (app de gestión).

La limpiadora entra con su propio PIN (config.PIN_LIMPIEZA, por defecto 4268): ese PIN NO vale para ningún otro endpoint de la app (todos usan
check_pin, que solo acepta el PIN normal y el de administrador), así que solo ve lo de aquí: el calendario de la casa con ocupado/libre y las
incidencias. Ver services/incidencias_limpieza.py.

  GET  /mobile/limpieza/calendario     (PIN de limpieza o de administrador) → {hoy, ocupados:[ISO], incidencias:[ISO]}
  POST /mobile/limpieza/incidencia     (idem) {fecha, accion: "marcar"|"quitar"} → marca o quita el día; al marcar avisa a Ione por WhatsApp/Telegram
  GET  /mobile/limpieza/incidencias    (PIN normal o de administrador) → {dias:[ISO], rangos:[{desde,hasta,dias}]} para el calendario de gestión
"""
import logging
from datetime import date

from flask import Blueprint, jsonify, request

from mobile_routes import _ensure_loaded, _es_pin_admin, _es_pin_limpieza, _state, check_pin
from services import incidencias_limpieza as inc
from services.fechas import hoy_madrid
from services.limites import ip_cliente, permitir
from services.whatsapp import alerta

logger = logging.getLogger(__name__)
limpieza_bp = Blueprint("limpieza", __name__, url_prefix="/mobile/limpieza")


def _autorizado():
    return _es_pin_limpieza() or _es_pin_admin()


def _error(e):
    return jsonify({"ok": False, "error": str(e)}), getattr(e, "status", 400)


def _fmt(iso):
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


def _texto_rango(r):
    return f"el {_fmt(r['desde'])}" if r["dias"] == 1 else f"del {_fmt(r['desde'])} al {_fmt(r['hasta'])} ({r['dias']} días)"


def _reservas():
    _ensure_loaded()
    return list(_state.get("bookings") or [])


def _contexto_del_dia(iso):
    """Qué hay ese día en la casa (para que Ione sepa si le afecta): salida y/o entrada de huéspedes, o una estancia en curso."""
    salida = entrada = False
    for b in _reservas():
        if str(b.get("roomId", "")) != inc.HABITACION or str(b.get("status", "")).lower() == "cancelled":
            continue
        if b.get("departure") == iso:
            salida = True
        if b.get("arrival") == iso:
            entrada = True
    return ("⚠️ Ese día hay SALIDA de huéspedes" if salida else "") + (" y ENTRADA de otros" if salida and entrada else ("⚠️ Ese día hay ENTRADA de huéspedes" if entrada else ""))


@limpieza_bp.route("/calendario", methods=["GET"])
def calendario():
    if not _autorizado():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    if not permitir([(f"limpieza-cal:{ip_cliente()}", 120, 3600)]):
        return jsonify({"ok": False, "error": "demasiadas_peticiones"}), 429
    hoy = hoy_madrid()
    return jsonify({"ok": True, "hoy": hoy.isoformat(), "ocupados": inc.noches_ocupadas(_reservas(), hoy), "incidencias": inc.dias(hoy, forzar=True)})


@limpieza_bp.route("/incidencia", methods=["POST"])
def incidencia():
    if not _autorizado():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    if not permitir([(f"limpieza-inc:{ip_cliente()}", 60, 3600)]):
        return jsonify({"ok": False, "error": "demasiadas_peticiones"}), 429
    data = request.get_json(silent=True) or {}
    accion = data.get("accion")
    if accion not in ("marcar", "quitar"):
        return jsonify({"ok": False, "error": "accion_no_valida"}), 400
    hoy = hoy_madrid()
    fecha = str(data.get("fecha") or "")
    try:
        if accion == "marcar":
            rango, ya = inc.marcar(fecha, hoy)
            if not ya:   # un segundo toque sobre un día ya marcado no vuelve a avisar
                contexto = _contexto_del_dia(fecha)
                alerta("Incidencia de limpieza — La Casa de la Primavera",
                       f"La limpiadora ha marcado que NO puede limpiar {_texto_rango({'desde': fecha, 'hasta': fecha, 'dias': 1})}."
                       + (f"\nIncidencia completa: {_texto_rango(rango)}." if rango["dias"] > 1 else "")
                       + (f"\n{contexto}" if contexto else ""), nivel="warning")
            return jsonify({"ok": True, "incidencias": inc.dias(hoy, forzar=True)})
        quitada = inc.quitar(fecha, hoy)
        if quitada:
            alerta("Incidencia de limpieza retirada — La Casa de la Primavera",
                   f"La limpiadora ha quitado la incidencia del {_fmt(fecha)}: ya puede limpiar ese día.", nivel="info")
        return jsonify({"ok": True, "incidencias": inc.dias(hoy, forzar=True)})
    except inc.ErrorIncidencias as e:
        return _error(e)


@limpieza_bp.route("/incidencias", methods=["GET"])
def incidencias():
    if not check_pin():
        return jsonify({"ok": False, "error": "PIN incorrecto"}), 401
    dias = inc.dias(hoy_madrid())
    return jsonify({"ok": True, "dias": dias, "rangos": inc.rangos(dias)})

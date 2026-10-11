"""
routes/acceso.py — Formulario «¿No te encuentra? Recibe tu enlace por correo» de la web de check-in.

POST /solicitar-acceso  { "ref": "<nombre o nº de reserva tal como lo recuerda>", "email": "...", "idioma": "es" }

Busca la reserva con la misma lógica que GET /check-in (número de confirmación y, si no, nombre con tolerancia a
erratas) y envía al correo indicado el enlace personal (services/acceso_enlace.py) o, si no la encuentra, un correo
que explica qué hacer. Sustituye al ciclo «escribir al buzón de huéspedes y esperar la respuesta de Apps Script».

La respuesta es SIEMPRE la misma ({"ok": true}) se haya encontrado la reserva o no, para que el formulario no sirva
para averiguar quién está alojado. Límites (services/limites.py): por IP, por correo y un tope diario global, porque
los correos salen de una cuenta de Gmail con unos 100 envíos al día.
"""
import logging
import re

from flask import Blueprint, jsonify, request

from services.acceso_correos import correo_enlace, correo_no_localizada
from services.acceso_enlace import IDIOMAS, habilitado, url_checkin
from services.beds24 import buscar_booking_por_nombre, buscar_booking_por_ref
from services.email_send import enviar_email, es_email_relay_ota
from services.limites import ip_cliente, permitir

logger = logging.getLogger(__name__)
acceso_bp = Blueprint("acceso", __name__)

MAX_POR_IP_HORA = 6
MAX_POR_CORREO_HORA = 3
MAX_POR_DIA = 40
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@acceso_bp.route("/solicitar-acceso", methods=["POST"])
def solicitar_acceso():
    if not habilitado():
        return jsonify({"ok": False, "error": "no_disponible"}), 503

    data = request.get_json(silent=True) or {}
    ref = str(data.get("ref") or "").strip()[:120]
    email = str(data.get("email") or "").strip()
    idioma = data.get("idioma") if data.get("idioma") in IDIOMAS else "es"

    if len(ref) < 2:
        return jsonify({"ok": False, "error": "falta_ref"}), 400
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        return jsonify({"ok": False, "error": "email_no_valido"}), 400
    if es_email_relay_ota(email):
        return jsonify({"ok": False, "error": "email_ota"}), 400
    if not permitir([(f"acceso-ip:{ip_cliente()}", MAX_POR_IP_HORA, 3600),
                     (f"acceso-correo:{email.lower()}", MAX_POR_CORREO_HORA, 3600),
                     ("acceso-dia", MAX_POR_DIA, 86400)]):
        return jsonify({"ok": False, "error": "demasiados_intentos"}), 429

    try:
        booking = buscar_booking_por_ref(ref)
        if not booking:
            booking, _ambiguo = buscar_booking_por_nombre(ref)   # una coincidencia dudosa cuenta como no encontrada
    except Exception as e:
        logger.error(f"[acceso] Error buscando la reserva: {e}")
        return jsonify({"ok": False, "error": "error_interno"}), 500

    asunto, cuerpo = correo_enlace(idioma, url_checkin(booking.get("id"), idioma)) if booking else correo_no_localizada(idioma)
    try:
        enviar_email(to=email, subject=asunto, body=cuerpo)
    except Exception as e:
        logger.error(f"[acceso] No se pudo enviar el correo: {e}")
        return jsonify({"ok": False, "error": "no_enviado"}), 502
    logger.info(f"[acceso] Solicitud atendida (reserva {'encontrada' if booking else 'no encontrada'}, idioma {idioma})")
    return jsonify({"ok": True})

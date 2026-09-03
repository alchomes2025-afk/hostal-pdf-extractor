"""
routes/watchdog.py — Punto de control completo del sistema: variables de
entorno, Beds24, RPV, Groq, Firebase, CallMeBot y Firestore.
"""
import logging
import requests
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Blueprint, request, jsonify

import config
from config import (
    API_TOKEN, TEST_TOKEN, ROOM_CONFIG, TEST_BOOKINGS,
    BEDS24_REFRESH_TOKEN, BEDS24_API_BASE, BEDS24_PROPERTY_ID,
    RPV_API_KEY, RPV_API_URL, RPV_PROPERTY_MAP, RPV_API_KEY_MAP,
    CALLMEBOT_PHONE, CALLMEBOT_API_KEY, CALLMEBOT_PHONE_2, CALLMEBOT_API_KEY_2,
    GOOGLE_REFRESH_TOKEN,
    GROQ_API_KEY, GROQ_API_URL, GROQ_MODEL_PRI,
)
from services.beds24 import get_beds24_access_token
from services.whatsapp import alerta

logger = logging.getLogger(__name__)
watchdog_bp = Blueprint("watchdog", __name__)


def _watchdog_debe_avisar(problemas):
    """
    Decide si hay que enviar un aviso de WhatsApp para este resultado del
    watchdog, comparando con el último estado guardado en Firestore.

    Evita el problema de repetir el MISMO aviso en cada ejecución (cada 15
    min) mientras el problema persiste sin cambios — solo avisa de nuevo
    si el conjunto de problemas cambia (aparece uno distinto, o se resuelve
    alguno), y envía un único aviso de "recuperado" cuando el sistema
    vuelve a estar OK tras haber tenido problemas.

    Devuelve (debe_avisar: bool, es_recuperacion: bool).
    Si Firestore no está configurado, no se puede recordar el estado
    anterior — en ese caso avisa siempre que haya problemas (comportamiento
    de antes, sin deduplicar) para no perder visibilidad por error.
    """
    firma_actual = "|".join(sorted(d for _, d, _ in problemas)) if problemas else ""

    if config.db is None:
        return (bool(problemas), False)

    doc_ref = config.db.collection("system_state").document("watchdog")
    try:
        doc = doc_ref.get()
        firma_anterior = doc.to_dict().get("firma", "") if doc.exists else ""
    except Exception as e:
        logger.error(f"[watchdog] Error leyendo estado anterior en Firestore: {e}")
        # Sin poder leer el estado anterior, avisa igualmente si hay problemas
        return (bool(problemas), False)

    es_recuperacion = bool(firma_anterior) and not problemas
    debe_avisar = (firma_actual != firma_anterior) if problemas else es_recuperacion

    try:
        doc_ref.set({
            "firma": firma_actual,
            "actualizado": datetime.now(ZoneInfo('Europe/Madrid')).isoformat(),
        })
    except Exception as e:
        logger.error(f"[watchdog] Error guardando estado en Firestore: {e}")

    return (debe_avisar, es_recuperacion)


@watchdog_bp.route("/watchdog", methods=["GET", "POST"])
def watchdog():
    """
    Verifica el estado de TODOS los servicios y variables del sistema.
    Envía alerta WhatsApp si detecta cualquier problema.

    Llamar desde Make.com una vez al día (ej. 08:00h) o desde UptimeRobot.

    GET /watchdog?token=Alchomes2025

    Puntos de control:
      ✓ Variables de entorno críticas (tokens, PINs, credenciales)
      ✓ Autenticación Beds24
      ✓ API de registroparteviajeros.com (las 5 habitaciones)
      ✓ API de Groq (chat del asistente)
      ✓ CallMeBot WhatsApp (los dos números)
      ✓ PINs de habitación configurados
      ✓ Accesibilidad de Firebase (web de check-in)
    """
    if request.method == "POST":
        token = (request.get_json(force=True) or {}).get("token", "")
    else:
        token = request.args.get("token", "")

    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    resultados = {}
    problemas  = []  # (nivel, descripcion, accion_sugerida)

    # ── 1. Variables de entorno ───────────────────────────────────────────
    env_criticas = {
        "BEDS24_REFRESH_TOKEN": BEDS24_REFRESH_TOKEN,
        "RPV_API_KEY":          RPV_API_KEY,
        "CALLMEBOT_PHONE":      CALLMEBOT_PHONE,
        "CALLMEBOT_API_KEY":    CALLMEBOT_API_KEY,
        "GROQ_API_KEY":         GROQ_API_KEY,
    }
    env_warning = {
        "CALLMEBOT_PHONE_2":    CALLMEBOT_PHONE_2,
        "CALLMEBOT_API_KEY_2":  CALLMEBOT_API_KEY_2,
        "GOOGLE_REFRESH_TOKEN": GOOGLE_REFRESH_TOKEN,  # para /extraer legacy
    }
    pins = {
        f"PIN hab. {ROOM_CONFIG[rid]['nombre']}": ROOM_CONFIG[rid]["pin"]
        for rid in ["702398", "702399", "702396", "702395"]  # Lanuza no tiene PIN
    }

    falt_criticas = [k for k, v in env_criticas.items() if not v]
    falt_warning  = [k for k, v in env_warning.items() if not v]
    falt_pins     = [k for k, v in pins.items() if not v]

    resultados["env_vars"] = {
        "criticas_ok": len(falt_criticas) == 0,
        "faltantes_criticas": falt_criticas,
        "faltantes_warning": falt_warning,
        "pins_faltantes": falt_pins,
    }
    if falt_criticas:
        problemas.append(("critico",
            f"Variables críticas no configuradas: {', '.join(falt_criticas)}",
            "Añadirlas en Render → Environment y hacer redeploy"))
    if falt_pins:
        problemas.append(("warning",
            f"PINs sin configurar: {', '.join(falt_pins)}",
            "Añadir en Render → Environment (PIN_HABITACION_2, PIN_HABITACION_3, PIN_DOBLE, PIN_DELUXE)"))

    # ── 2. Beds24 ─────────────────────────────────────────────────────────
    try:
        tok = get_beds24_access_token()
        # Prueba real: obtener 1 reserva
        r = requests.get(
            f"{BEDS24_API_BASE}/bookings",
            headers={"token": tok, "accept": "application/json"},
            params={"propertyId": BEDS24_PROPERTY_ID, "arrivalFrom": date.today().isoformat(),
                    "arrivalTo": (date.today() + timedelta(days=1)).isoformat()},
            timeout=10,
        )
        r.raise_for_status()
        resultados["beds24"] = {"ok": True, "reservas_hoy_mañana": len(r.json().get("data", []))}
    except Exception as e:
        resultados["beds24"] = {"ok": False, "error": str(e)}
        problemas.append(("critico",
            f"Beds24 no responde: {e}",
            "Verificar BEDS24_REFRESH_TOKEN en Render — puede haber caducado"))

    # ── 3. RPV API (probar cada habitación) ───────────────────────────────
    rpv_ok = []
    rpv_fail = []
    for room_id, prop_id in RPV_PROPERTY_MAP.items():
        nombre = ROOM_CONFIG.get(room_id, {}).get("nombre", room_id)
        # Usa la clave de RPV correcta para esta habitación (algunas propiedades
        # tienen cuenta de RPV propia, distinta de la del hostal) — mismo criterio
        # que ya usa parte_recibido_para().
        api_key_usar = RPV_API_KEY_MAP.get(room_id) or RPV_API_KEY
        try:
            resp = requests.get(
                RPV_API_URL,
                headers={"Authorization": f"Bearer {api_key_usar}", "accept": "application/json"},
                params={"propiedad": prop_id},
                timeout=8,
            )
            if resp.status_code == 401:
                raise Exception("API key inválida (401)")
            if resp.status_code == 403:
                raise Exception("Acceso denegado (403) — revisa que la API key usada tenga permiso sobre esta propiedad")
            if resp.status_code == 404:
                raise Exception(f"Propiedad no encontrada: {prop_id}")
            resp.raise_for_status()
            rpv_ok.append(nombre)
        except Exception as e:
            rpv_fail.append(f"{nombre}: {e}")

    resultados["rpv_api"] = {"ok": len(rpv_fail) == 0, "ok_list": rpv_ok, "fail_list": rpv_fail}
    if rpv_fail:
        problemas.append(("critico",
            f"RPV API falla en {len(rpv_fail)} habitación(es): {'; '.join(rpv_fail)}",
            "Verificar en Render que RPV_API_KEY (hostal) y RPV_API_KEY_CASA_PRIMAVERA "
            "(si aplica) están configuradas y son correctas — puede haber caducado, "
            "cambiado, o faltar la variable de la propiedad afectada"))

    # ── 4. Groq API ───────────────────────────────────────────────────────
    if GROQ_API_KEY:
        try:
            r = requests.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": GROQ_MODEL_PRI,
                      "messages": [{"role": "user", "content": "ping"}],
                      "max_tokens": 5},
                timeout=10,
            )
            if r.status_code == 401:
                raise Exception("API key inválida o revocada (401) — Groq la revoca si se publica en repos públicos")
            if r.status_code == 429:
                resultados["groq"] = {"ok": True, "nota": "Rate limit (429) — servicio activo pero saturado"}
            else:
                r.raise_for_status()
                resultados["groq"] = {"ok": True, "model": GROQ_MODEL_PRI}
        except Exception as e:
            resultados["groq"] = {"ok": False, "error": str(e)}
            problemas.append(("critico",
                f"Groq API error: {e}",
                "Generar nueva API key en console.groq.com y actualizar GROQ_API_KEY en Render"))
    else:
        resultados["groq"] = {"ok": False, "error": "GROQ_API_KEY no configurada"}
        problemas.append(("critico", "GROQ_API_KEY no configurada",
            "Añadir en Render → Environment"))

    # ── 5. Firebase (web de check-in) ─────────────────────────────────────
    try:
        r = requests.get("https://alc-homes-checkin.web.app", timeout=8)
        resultados["firebase_web"] = {"ok": r.ok, "status": r.status_code}
        if not r.ok:
            problemas.append(("critico",
                f"Web de check-in no accesible (HTTP {r.status_code})",
                "Revisar Firebase Hosting en console.firebase.google.com"))
    except Exception as e:
        resultados["firebase_web"] = {"ok": False, "error": str(e)}
        problemas.append(("critico", f"Web de check-in inaccesible: {e}",
            "Revisar Firebase Hosting"))

    # ── 6. CallMeBot ─────────────────────────────────────────────────────
    # Solo verificamos que los números y keys estén configurados
    # (no enviamos mensaje de prueba para no molestar)
    callmebot_ok = bool(CALLMEBOT_PHONE and CALLMEBOT_API_KEY)
    callmebot2_ok = bool(CALLMEBOT_PHONE_2 and CALLMEBOT_API_KEY_2)
    resultados["callmebot"] = {
        "numero_1": "configurado" if callmebot_ok else "⚠️ no configurado",
        "numero_2": "configurado" if callmebot2_ok else "no configurado (opcional)",
    }

    # ── 7. Reservas ficticias de prueba ───────────────────────────────────
    resultados["test_bookings"] = {
        "disponibles": list(TEST_BOOKINGS.keys()),
        "nota": "Usar en alc-homes-checkin.web.app para probar cada estado",
    }

    # ── 8. Firestore (histórico de interacciones) ─────────────────────────
    if config.db is None:
        resultados["firestore"] = {"ok": False, "error": "FIRESTORE_CREDENTIALS_JSON no configurada"}
        problemas.append(("warning",
            "Firestore no configurado — el histórico de conversaciones no se está guardando",
            "Añadir FIRESTORE_CREDENTIALS_JSON en Render → Environment"))
    else:
        try:
            list(config.db.collection("interactions").limit(1).stream())
            resultados["firestore"] = {"ok": True}
        except Exception as e:
            resultados["firestore"] = {"ok": False, "error": str(e)}
            problemas.append(("critico",
                f"Firestore no responde: {e}",
                "Verificar credenciales de servicio y permisos del proyecto en Firebase Console"))

    # ── Resumen y alerta WhatsApp (con deduplicación) ─────────────────────
    n_criticos = sum(1 for nivel, _, _ in problemas if nivel == "critico")
    n_warnings  = sum(1 for nivel, _, _ in problemas if nivel == "warning")
    todo_ok = len(problemas) == 0

    debe_avisar, es_recuperacion = _watchdog_debe_avisar(problemas)

    if problemas and debe_avisar:
        lineas = [f"{'🔴' if nivel=='critico' else '🟡'} {desc}\n   → {accion}"
                  for nivel, desc, accion in problemas]
        alerta(
            f"Watchdog — {n_criticos} error(es) crítico(s), {n_warnings} aviso(s)",
            "\n\n".join(lineas),
            nivel="critico" if n_criticos > 0 else "warning"
        )
    elif problemas and not debe_avisar:
        logger.info("[watchdog] Mismo(s) problema(s) que en el último aviso — no se repite la notificación por WhatsApp.")
    elif es_recuperacion:
        alerta("Watchdog — ✅ Recuperado, todos los sistemas vuelven a estar operativos", "", nivel="info")
    else:
        logger.info("[watchdog] ✅ Todos los sistemas operativos — sin aviso WhatsApp (solo se avisa si hay problemas)")

    return jsonify({
        "ok":          todo_ok,
        "timestamp":   datetime.now(ZoneInfo('Europe/Madrid')).strftime("%Y-%m-%d %H:%M:%S (Madrid)"),
        "resumen":     f"{n_criticos} crítico(s), {n_warnings} aviso(s)" if problemas else "✅ Todo OK",
        "problemas":   [{"nivel": n, "descripcion": d, "accion": a} for n, d, a in problemas],
        "servicios":   resultados,
    })

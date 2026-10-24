"""
routes/watchdog.py — Punto de control completo del sistema: variables de
entorno, Beds24, RPV, Groq, Firebase, CallMeBot y Firestore.
"""
import logging
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Blueprint, request, jsonify

import config
from config import (
    API_TOKEN, TEST_TOKEN, ROOM_CONFIG, TEST_BOOKINGS,
    BEDS24_REFRESH_TOKEN, BEDS24_API_BASE, BEDS24_PROPERTY_ID,
    RPV_API_KEY,
    CALLMEBOT_PHONE, CALLMEBOT_API_KEY, CALLMEBOT_PHONE_2, CALLMEBOT_API_KEY_2,
    GROQ_API_KEY, GROQ_API_URL, GROQ_MODEL_PRI,
)
from services.beds24 import get_beds24_access_token, _token_cache as _token_cache_beds24
from services.rpv import comprobar_cuentas, TTL_SEGUNDO_PLANO
from services.ical_beds24 import comprobar_feeds_ical
from services.whatsapp import alerta
from services.checkins_ultima_hora import comprobar_y_avisar_checkins_ultima_hora
from services.fechas import hoy_madrid
from services.hostelworld_avisos import enviar_avisos_checkin_hostelworld
from services.registro_completado_avisos import enviar_avisos_registro_completado
from services.sin_mensajeria_avisos import avisar_reservas_sin_mensajeria
from services.solapes import avisar_solapes
from services.rpv_sin_importar import avisar_reservas_sin_importar
from services.reservas_correo import verificar_pendientes as verificar_reservas_correo, avisar_errores_canal
from services.resumen_programado import enviar_resumenes_programados

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


INTENTOS_BEDS24 = 2          # el watchdog corre dentro de un worker con timeout de 60 s: pocos intentos y cortos
PAUSA_BEDS24 = 2             # segundos entre intentos
TIMEOUT_BEDS24 = (5, 10)     # (conexión, lectura)

# Textos FIJOS (sin el error de la excepción): la firma de deduplicación del watchdog depende de la descripción, y un
# texto como «Connection to beds24.com timed out … at 0x77ec52…» cambia en cada fallo y repetiría el WhatsApp.
_BEDS24_CREDENCIAL = ("critico", "Beds24 rechaza las credenciales (BEDS24_REFRESH_TOKEN)",
                      "Verificar BEDS24_REFRESH_TOKEN en Render — puede haber caducado o haberse revocado")
_BEDS24_LIMITADO = ("warning", "Beds24 está limitando peticiones (429)",
                    "No hay que tocar nada: el backend reintenta solo. Si dura más de una hora, revisar el volumen de llamadas")
_BEDS24_SIN_RESPUESTA = ("warning", "Beds24 no responde (no contesta a tiempo o devuelve error del servidor)",
                         "Suele ser un fallo puntual de Beds24 o de la red: no hay que tocar nada y se avisa cuando se recupere. "
                         "Si dura más de una hora, mirar el estado de Beds24 y, solo entonces, BEDS24_REFRESH_TOKEN en Render")


def _clasificar_fallo_beds24(error):
    """Problema del watchdog para un error al hablar con Beds24: credenciales (401/403), límite (429) o sin respuesta."""
    codigo = getattr(getattr(error, "response", None), "status_code", None)
    if codigo in (400, 401, 403):
        return _BEDS24_CREDENCIAL
    if codigo == 429:
        return _BEDS24_LIMITADO
    return _BEDS24_SIN_RESPUESTA


def _comprobar_beds24():
    """
    Prueba real de Beds24: token y 1 consulta de reservas. Un fallo de red puntual (timeout, 5xx) se reintenta antes de
    darlo por malo, y solo un rechazo de credenciales es «crítico» con la acción de revisar el refresh token.
    Devuelve (resultado_para_el_informe, problema_o_None).
    """
    ultimo_error = None
    for intento in range(INTENTOS_BEDS24):
        try:
            tok = get_beds24_access_token()
            r = requests.get(
                f"{BEDS24_API_BASE}/bookings",
                headers={"token": tok, "accept": "application/json"},
                params={"propertyId": BEDS24_PROPERTY_ID, "arrivalFrom": hoy_madrid().isoformat(),
                        "arrivalTo": (hoy_madrid() + timedelta(days=1)).isoformat()},
                timeout=TIMEOUT_BEDS24,
            )
            r.raise_for_status()
            return {"ok": True, "reservas_hoy_mañana": len(r.json().get("data", [])), "intentos": intento + 1}, None
        except Exception as e:
            ultimo_error = e
            if _clasificar_fallo_beds24(e) is _BEDS24_CREDENCIAL:
                # puede ser solo el token guardado en memoria, que Beds24 haya anulado: se descarta y se pide otro
                _token_cache_beds24.update({"token": None, "expira": 0.0})
                continue
            if intento < INTENTOS_BEDS24 - 1:
                time.sleep(PAUSA_BEDS24)
    logger.warning(f"[watchdog] Beds24 falla tras {INTENTOS_BEDS24} intento(s): {ultimo_error}")
    return {"ok": False, "error": str(ultimo_error)}, _clasificar_fallo_beds24(ultimo_error)


@watchdog_bp.route("/watchdog", methods=["GET", "POST"])
def watchdog():
    """
    Verifica el estado de TODOS los servicios y variables del sistema.
    Envía alerta WhatsApp si detecta cualquier problema.

    Lo llama el Apps Script "Orquestador" cada 15 min. Además de los
    chequeos de salud, envía los resúmenes diarios de las 08:00 y las 23:00
    y los avisos de negocio (última hora, reservas sin importar en RPV, Hostelworld, registro completado).

    GET /watchdog?token=<TOKEN>

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
    }
    pins = {
        f"PIN hab. {ROOM_CONFIG[rid]['nombre']}": ROOM_CONFIG[rid]["pin"]
        for rid in ["702398", "702399", "702395"]  # Lanuza y Fossá (Doble) no tienen PIN a propósito (cerraduras averiadas/inexistentes)
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
    resultados["beds24"], problema_beds24 = _comprobar_beds24()
    if problema_beds24:
        problemas.append(problema_beds24)

    # ── 3. RPV API (una consulta por cuenta de RPV) ───────────────────────
    # Va por services/rpv.comprobar_cuentas, con la misma copia en memoria que
    # usan los avisos y resúmenes: son 2 llamadas (hostal y Primavera) como
    # mucho cada 10 min (antes, 12 en ráfaga cada 15 min provocaron un 429).
    rpv_ok = []
    rpv_fail = []
    rpv_limitado = []
    for etiqueta, error in comprobar_cuentas(TTL_SEGUNDO_PLANO):
        if error is None:
            rpv_ok.append(etiqueta)
        elif error.startswith("429"):
            rpv_limitado.append(etiqueta)
        else:
            rpv_fail.append(f"{etiqueta}: {error}")

    resultados["rpv_api"] = {"ok": not rpv_fail and not rpv_limitado, "ok_list": rpv_ok,
                             "fail_list": rpv_fail, "limitado_429": rpv_limitado}
    if rpv_fail:
        problemas.append(("critico",
            f"RPV API falla en {len(rpv_fail)} cuenta(s): {'; '.join(rpv_fail)}",
            "Verificar en Render que RPV_API_KEY (hostal) y RPV_API_KEY_CASA_PRIMAVERA "
            "(si aplica) están configuradas y son correctas — puede haber caducado, "
            "cambiado, o faltar la variable de la propiedad afectada"))
    if rpv_limitado:
        # Texto fijo (sin lista de habitaciones): la firma de dedupe del
        # watchdog depende de él, y si variara se repetiría el WhatsApp.
        problemas.append(("warning",
            "RPV API está limitando peticiones (429)",
            "No hay que tocar nada: el backend deja de llamar unos minutos y "
            "reintenta solo; mientras, la web de check-in usa el último dato "
            "conocido. Si dura más de una hora, revisar el volumen de llamadas a RPV"))

    # ── 3b. Calendarios iCal de Beds24 (los que importa RPV) ──────────────
    # RPV no avisa si deja de poder importarlos (sigue poniendo "Sincronizado"):
    # sin esto, las reservas nuevas no llegan a RPV y nadie se entera.
    ical = comprobar_feeds_ical()
    if ical is None:
        resultados["ical_beds24"] = {"configurado": False}
    else:
        resultados["ical_beds24"] = {"ok": not ical["fail"], "ok_list": ical["ok"], "fail_list": ical["fail"]}
        if ical["fail"]:
            problemas.append(("critico",
                f"Calendario iCal de Beds24 falla en {len(ical['fail'])} habitación(es): {'; '.join(ical['fail'])}",
                "RPV no importa reservas nuevas mientras falle (y sigue poniendo 'Sincronizado'). "
                "En Beds24: SETTINGS → CHANNEL MANAGER → ICAL EXPORT, comprobar que 'Export' de esa "
                "habitación no está en Disable (debe ser solo reservas) y que el enlace abre en el "
                "navegador un texto que empieza por BEGIN:VCALENDAR"))

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

    # ── 9. Resúmenes diarios por WhatsApp (08:00 hoy / 23:00 mañana) ──────
    # Va ANTES del paso 10: cuando sale un resumen, marca sus entradas como
    # anunciadas y el chequeo de última hora de esta misma pasada ya no las
    # repite. Dedupe propio en Firestore (system_state/resumenes_programados).
    try:
        enviar_resumenes_programados()
    except Exception as e:
        logger.error(f"[watchdog] Error enviando resúmenes programados: {e}")

    # ── 10. Check-ins de última hora (hostal + La Casa de la Primavera) ────
    # No es un chequeo de salud del sistema (no entra en `problemas`/dedupe
    # de watchdog): es un aviso de negocio aparte, con su propio dedupe por
    # book_id en Firestore. Se aprovecha esta ejecución cada 15 min para
    # detectar rápido, a cualquier hora, una reserva que no estaba en el
    # resumen que cubría el día.
    try:
        comprobar_y_avisar_checkins_ultima_hora()
    except Exception as e:
        logger.error(f"[watchdog] Error comprobando check-ins de última hora: {e}")

    # ── 10b. Reservas que RPV no ha importado a tiempo ─────────────────────
    # Aviso de negocio aparte (dedupe propio por book_id en Firestore): una
    # reserva de Beds24 que lleva más de HORAS_NORMALES_SIN_RPV sin constar en
    # RPV indica que algo falla en la importación, aunque RPV responda bien.
    try:
        avisar_reservas_sin_importar()
    except Exception as e:
        logger.error(f"[watchdog] Error comprobando reservas sin importar en RPV: {e}")

    # ── 10c. Avisos de reserva del correo que no cuadran con Beds24 ────────
    # Aviso de negocio aparte (dedupe propio en Firestore, colección reservas_correo): una reserva nueva, modificada o
    # cancelada que la plataforma notificó por correo y que Beds24 no refleja pasados ESPERA_MINUTOS. Sin avisos pendientes
    # no consulta Beds24; si Beds24 no responde, no avisa de nada.
    try:
        verificar_reservas_correo()
    except Exception as e:
        logger.error(f"[watchdog] Error cotejando los avisos de reserva del correo: {e}")
    try:
        avisar_errores_canal()
    except Exception as e:
        logger.error(f"[watchdog] Error comprobando los errores de canal de Beds24: {e}")

    # ── 11. Email de check-in para huéspedes de Hostelworld ────────────────
    # Hostelworld no permite plantillas de mensaje preprogramadas en Beds24,
    # así que sin esto esos huéspedes no reciben nunca el enlace de check-in.
    # Se envía un día antes de la llegada; dedupe por book_id en Firestore.
    try:
        enviar_avisos_checkin_hostelworld()
    except Exception as e:
        logger.error(f"[watchdog] Error enviando avisos de check-in a Hostelworld: {e}")

    # ── 12. Email de "registro completado" para TODOS los huéspedes ────────
    # En cuanto el parte de viajero queda registrado en RPV para una reserva
    # próxima (cualquier plataforma), se avisa de que ya puede volver a la
    # web a por los códigos de acceso. Dedupe por book_id en Firestore.
    try:
        enviar_avisos_registro_completado()
    except Exception as e:
        logger.error(f"[watchdog] Error enviando avisos de registro completado: {e}")

    # ── 13. Huéspedes que no pueden recibir mensajes por la plataforma ─────
    # (p. ej. Expedia vía red de afiliados): aviso con sus datos de contacto.
    try:
        avisar_reservas_sin_mensajeria()
    except Exception as e:
        logger.error(f"[watchdog] Error avisando de reservas sin mensajería: {e}")

    # ── 14. Reservas que se pisan (posible sobreventa) ─────────────────────
    # Dos reservas activas de la misma habitación con noches en común: una sobreventa que Beds24 sí ha registrado.
    # Se comprueba como mucho cada hora (ver services/solapes.py); dedupe por par de reservas en Firestore.
    try:
        avisar_solapes()
    except Exception as e:
        logger.error(f"[watchdog] Error comprobando solapes de reservas: {e}")

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

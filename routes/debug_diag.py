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


@debug_bp.route("/diag-bloqueos", methods=["GET"])
def diag_bloqueos():
    """
    Diagnóstico de solo lectura: cómo se ven en Beds24 los bloqueos (reservas ficticias de la app, importaciones por
    iCal como «BLOCKED by Holidu») frente a las reservas reales, y si constan en RPV. Para cada reserva: habitación,
    fechas, estado, canal, origen, si tiene precio, si el email es el de bloqueos de la app y si consta en RPV. El
    nombre solo se muestra cuando es el de un bloqueo («Fecha bloqueada», «BLOCKED by …»); los de huéspedes reales
    salen como «(huésped)». Sin emails ni datos personales.

    Con `q=<nombre>` añade a cada reserva si su nombre coincide con ese texto según la regla de la web de check-in
    (`coincide_con_q`) y cuántas palabras tiene, para depurar «no me reconoce» sin mostrar nombres.

    Uso: /diag-bloqueos?token=<TOKEN>[&desde=2026-10-01&hasta=2026-12-31][&habitacion=720841][&q=Nombre Apellido]
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    from config import BEDS24_PROPERTY_IDS
    from services.rpv import TTL_SEGUNDO_PLANO, obtener_estado_partes
    hoy = hoy_madrid()
    desde = request.args.get("desde") or (hoy - timedelta(days=10)).isoformat()
    hasta = request.args.get("hasta") or (hoy + timedelta(days=120)).isoformat()
    habitacion = request.args.get("habitacion", "").strip()
    q = request.args.get("q", "").strip()
    from services.guest_match import normalizar_nombre, _match_exacto
    q_tokens = normalizar_nombre(q) if q else None
    try:
        access_token = get_beds24_access_token()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Beds24 auth: {e}"}), 500
    estados, _sin_verificar = obtener_estado_partes(max_age=TTL_SEGUNDO_PLANO)
    filas, campos = [], set()
    for property_id in BEDS24_PROPERTY_IDS:
        for pagina in range(1, 11):
            try:
                resp = requests.get(f"{BEDS24_API_BASE}/bookings", headers={"token": access_token, "accept": "application/json"},
                                    params={"propertyId": property_id, "arrivalFrom": desde, "arrivalTo": hasta,
                                            "includePersonalInfo": "true", "page": pagina}, timeout=20)
                resp.raise_for_status()
                cuerpo = resp.json()
            except Exception as e:
                return jsonify({"ok": False, "error": f"Error consultando Beds24 (propiedad {property_id}): {e}"}), 500
            for b in cuerpo.get("data", []):
                if not b.get("arrival") or not (desde <= b["arrival"] <= hasta):
                    continue
                if habitacion and str(b.get("roomId")) != habitacion:
                    continue
                guest = b.get("guest") or {}
                nombre = f"{guest.get('firstName') or b.get('firstName') or ''} {guest.get('lastName') or b.get('lastName') or ''}".strip()
                es_nombre_bloqueo = nombre.lower().startswith(("blocked", "fecha bloqueada"))
                email = (guest.get("email") or b.get("email") or "").strip().lower()
                room_id = str(b.get("roomId", ""))
                rpv = estados.get((room_id, b.get("arrival")))
                campos.update(b.keys())
                clave_grupo = str(b.get("masterId") or "") or str(b.get("apiReference") or "") or str(b.get("reference") or "")
                filas.append({"id": b.get("id"), "habitacion": room_id, "llegada": b.get("arrival"), "salida": b.get("departure"),
                              "estado": b.get("status"), "canal": b.get("channel"), "origen_api": b.get("apiSource"),
                              "referer": b.get("referer"), "precio": b.get("price"), "nombre": nombre if es_nombre_bloqueo else ("(huésped)" if nombre else "(sin nombre)"),
                              "email_de_bloqueo_app": email == "bloqueo@bloqueo.com", "tiene_email": bool(email),
                              "master_id": b.get("masterId"), "_g": clave_grupo, "_n": f"{nombre.lower()}|{b.get('arrival')}",
                              "en_rpv": rpv["estado"] if rpv else None, "creada": b.get("bookingTime"), "modificada": b.get("modifiedTime"),
                              **({"coincide_con_q": bool(_match_exacto(q_tokens, [normalizar_nombre(nombre)])), "palabras_nombre": len(normalizar_nombre(nombre))} if q_tokens else {})})
            if not (cuerpo.get("pages") or {}).get("nextPageExists"):
                break
    filas.sort(key=lambda f: (f["habitacion"], f["llegada"] or ""))
    from collections import Counter
    por_clave, por_nombre = Counter(f["_g"] for f in filas if f["_g"]), Counter(f["_n"] for f in filas if f["nombre"] != "(sin nombre)")
    numeros = {}
    for f in filas:
        f["mismo_grupo_o_ref"] = numeros.setdefault(("g", f["_g"]), len(numeros) + 1) if f["_g"] and por_clave[f["_g"]] > 1 else None
        f["mismo_nombre_y_llegada"] = numeros.setdefault(("n", f["_n"]), len(numeros) + 1) if por_nombre[f["_n"]] > 1 else None
        del f["_g"], f["_n"]
    return jsonify({"ok": True, "hoy": hoy.isoformat(), "total": len(filas), "campos_disponibles": sorted(campos), "reservas": filas}), 200


@debug_bp.route("/diag-grupo", methods=["GET"])
def diag_grupo():
    """
    Diagnóstico de solo lectura: las habitaciones de la misma reserva de VARIAS HABITACIONES que una reserva de Beds24
    (la misma consulta que usa /check-in). Solo habitación, fechas y estado; sin nombres ni datos personales.

    Uso: /diag-grupo?token=<TOKEN>&id=94617075
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    from services.beds24 import reservas_del_grupo
    booking = buscar_booking_por_id(request.args.get("id", ""))
    if not booking:
        return jsonify({"ok": False, "error": "reserva no encontrada"}), 404
    miembros = reservas_del_grupo(booking)
    return jsonify({"ok": True, "habitaciones": [{"id": b.get("id"), "master_id": b.get("masterId"), "habitacion": str(b.get("roomId")), "llegada": b.get("arrival"),
                                                  "salida": b.get("departure"), "estado": b.get("status")} for b in miembros]}), 200


@debug_bp.route("/diag-estado", methods=["GET"])
def diag_estado():
    """
    Diagnóstico de solo lectura: cuándo se escribió por última vez cada documento de estado del watchdog en Firestore
    (system_state/*). Sirve para saber si el watchdog sigue ejecutándose y cuándo salió el último resumen. Sin datos
    personales: solo fechas de escritura y, del de resúmenes programados, la fecha de último envío de cada uno.

    Uso: /diag-estado?token=<TOKEN>
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    import config
    from services.fechas import ahora_madrid
    if config.db is None:
        return jsonify({"ok": False, "error": "Firestore no configurado"}), 500
    docs = {}
    for nombre in ("watchdog", "resumenes_programados", "primavera_avisos", "rpv_sin_importar", "rpv_latencia",
                   "registro_completado_emails", "hostelworld_checkin_emails", "token_health"):
        try:
            d = config.db.collection("system_state").document(nombre).get()
            info = {"existe": d.exists, "escrito": d.update_time.isoformat() if d.exists and d.update_time else None}
            if nombre == "resumenes_programados" and d.exists:
                info["ultimo_envio"] = {k: v for k, v in (d.to_dict() or {}).items() if k in ("manana", "noche")}
            docs[nombre] = info
        except Exception as e:
            docs[nombre] = {"error": str(e)[:120]}
    return jsonify({"ok": True, "ahora_madrid": ahora_madrid().isoformat(timespec="minutes"), "documentos": docs}), 200


@debug_bp.route("/diag-whatsapp", methods=["GET"])
def diag_whatsapp():
    """
    Diagnóstico: manda UN mensaje de prueba corto a cada número de CallMeBot configurado y devuelve la respuesta CRUDA de
    CallMeBot (código HTTP y texto) de cada uno, con el teléfono enmascarado. Sirve para ver si los envíos que el servidor
    da por buenos (HTTP 200) llegan de verdad o CallMeBot responde con un error dentro del texto. ENVÍA UN WHATSAPP REAL.

    Uso: /diag-whatsapp?token=<TOKEN>&enviar=1[&resumen=hoy|manana]   (sin enviar=1 solo dice qué números hay configurados;
    con resumen= manda el resumen real en lugar de la prueba y da su longitud)
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    from urllib.parse import quote
    from config import CALLMEBOT_API_KEY, CALLMEBOT_API_KEY_2, CALLMEBOT_PHONE, CALLMEBOT_PHONE_2
    destinos = [(f"nº1 …{str(CALLMEBOT_PHONE)[-3:]}", CALLMEBOT_PHONE, CALLMEBOT_API_KEY)] if CALLMEBOT_PHONE and CALLMEBOT_API_KEY else []
    if CALLMEBOT_PHONE_2 and CALLMEBOT_API_KEY_2:
        destinos.append((f"nº2 …{str(CALLMEBOT_PHONE_2)[-3:]}", CALLMEBOT_PHONE_2, CALLMEBOT_API_KEY_2))
    if request.args.get("enviar") != "1":
        return jsonify({"ok": True, "numeros_configurados": [d[0] for d in destinos], "enviado": False}), 200
    texto_envio, extra = "Prueba de diagnostico ALCHOMES", {}
    if request.args.get("resumen") in ("hoy", "manana"):
        # El resumen REAL (el que el watchdog manda a las 08:00 / 23:00), sin marcarlo como enviado en Firestore.
        from services.resumen import generar_mensaje_resumen
        manana = request.args.get("resumen") == "manana"
        texto_envio, _ = generar_mensaje_resumen("23" if manana else "08", dia=(hoy_madrid() + timedelta(days=1)) if manana else hoy_madrid())
        extra = {"caracteres": len(texto_envio), "caracteres_url": len(quote(texto_envio))}
    resultados = []
    for etiqueta, telefono, apikey in destinos:
        try:
            resp = requests.get(f"https://api.callmebot.com/whatsapp.php?phone={telefono}&text={quote(texto_envio)}&apikey={apikey}", timeout=25)
            texto = resp.text.replace(str(telefono), "<tel>").replace(str(apikey), "<clave>")
            resultados.append({"destino": etiqueta, "http": resp.status_code, "respuesta": " ".join(texto.split())[:350]})
        except Exception as e:
            resultados.append({"destino": etiqueta, "error": str(e)[:200]})
    return jsonify({"ok": True, "enviado": True, **extra, "resultados": resultados}), 200


@debug_bp.route("/diag-avisos", methods=["GET"])
def diag_avisos():
    """
    Diagnóstico y ayuda de configuración de los canales de respaldo de los avisos (Telegram y correo):
      - sin parámetros: qué canales están configurados (sin mostrar claves) y, si hay bot de Telegram, los chats que le han
        escrito (id, tipo y nombre) para saber qué poner en TELEGRAM_CHAT_ID: basta escribirle «hola» al bot y abrir esto;
      - con enviar=1: manda UNA prueba por cada canal de respaldo configurado y dice si llegó (ENVÍA MENSAJES REALES).

    Uso: /diag-avisos?token=<TOKEN>[&enviar=1]
    """
    token = request.args.get("token", "")
    tokens_validos = [t for t in [API_TOKEN, TEST_TOKEN] if t]
    if token not in tokens_validos:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    from config import AVISOS_EMAIL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    from services import whatsapp as wa
    resultado = {"ok": True, "telegram": {"bot_configurado": bool(TELEGRAM_BOT_TOKEN), "chats_configurados": len(wa._lista(TELEGRAM_CHAT_ID))},
                 "correo": {"direcciones_configuradas": len(wa._lista(AVISOS_EMAIL))}}
    if TELEGRAM_BOT_TOKEN:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=15).json()
            chats = {}
            for u in r.get("result", []):
                chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat") or {}
                if chat.get("id") is not None:
                    chats[chat["id"]] = {"id": chat["id"], "tipo": chat.get("type"), "nombre": chat.get("first_name") or chat.get("title")}
            resultado["telegram"]["chats_que_han_escrito_al_bot"] = list(chats.values())
            if not r.get("ok"):
                resultado["telegram"]["error"] = str(r.get("description"))[:120]
        except Exception as e:
            resultado["telegram"]["error"] = str(e)[:120]
    if request.args.get("enviar") == "1":
        resultado["enviado"] = [{"canal": canal, "llego": error is None, "error": error} for canal, error in wa._enviar_respaldos("Prueba de aviso de respaldo ALCHOMES")]
    return jsonify(resultado), 200


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

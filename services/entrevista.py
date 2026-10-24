"""
services/entrevista.py — Entrenamiento: una entrevista guiada a Ione (con Adrián al lado) para recoger lo que sabe del puesto de gestora y pasarlo a la base de conocimiento.

Funciona por TEMAS (los del encargo). Cada turno: el modelo recibe el tema, su lista de cosas por cubrir, lo ya recogido y la conversación reciente, y devuelve UNA pregunta
siguiente y, si lo último que se ha contestado da para ello, entradas nuevas de la base de conocimiento. Esas entradas se guardan SIEMPRE como BORRADOR: nada llega al
sustituto ni al asistente hasta que el propietario lo revisa y lo aprueba en la pestaña Operativa de Gestión → Base de conocimiento.

La conversación se guarda en Firestore (`entrenamiento_sesiones`) para poder seguirla otro día. Usa la cuenta de Groq de GESTIÓN (services/groq_claves.py).
Nada lanza excepción hacia arriba salvo ErrorEntrevista (con un mensaje legible y el HTTP que corresponde).
"""
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

import requests

import config
from config import GROQ_API_URL, GROQ_MODEL_FALL, GROQ_MODEL_PRI
from services import operativa_kb as kb
from services.groq_claves import clave_gestion

logger = logging.getLogger(__name__)

COLECCION = "entrenamiento_sesiones"
MAX_MENSAJES_GUARDADOS = 80
MENSAJES_AL_MODELO = 12
MAX_CHARS_MENSAJE = 900
MAX_ENTRADAS_POR_TURNO = 4
PRESUPUESTO_SEG = 45

# Qué debe cubrir cada tema (lo que pidió Adrián). `tipo` es el tipo de entrada que normalmente sale de él.
TEMAS = [
    {"id": "dia_a_dia", "titulo": "Tareas del día a día, en secuencia", "tipo": "tarea", "cubrir": [
        "qué hace Ione desde que empieza hasta que acaba el día, en orden y con la hora aproximada de cada tarea",
        "para cada tarea: pasos concretos, qué se mira o comprueba, dónde (app, Beds24, RPV, correo, WhatsApp…) y a quién se avisa si algo no cuadra",
        "qué tareas son diarias, cuáles semanales y cuáles solo a veces; qué se puede dejar para otro momento y qué no"]},
    {"id": "limpieza", "titulo": "Supervisión y control de la limpieza", "tipo": "tarea", "cubrir": [
        "cómo se organiza la limpieza (quién limpia, cuándo, en qué orden de habitaciones) y cómo sabe Ione que está hecha",
        "qué se revisa (lista de comprobación), cómo se registra y qué se hace si falta algo, si no se ha limpiado o hay una queja de un huésped",
        "qué hacer si la limpiadora no puede venir (incidencia de limpieza), a quién se llama y qué se hace con las reservas de ese día"]},
    {"id": "reservas", "titulo": "Supervisión de las reservas", "tipo": "tarea", "cubrir": [
        "qué revisa de las reservas y con qué frecuencia (nuevas, cancelaciones, modificaciones, llegadas y salidas del día, partes de viajeros, solapes)",
        "qué se considera una anomalía y qué se hace en cada caso (parte sin hacer, reserva que no aparece en RPV, posible sobreventa, huésped que no llega…)",
        "cómo se comprueba que lo que dice cada canal coincide con el calendario y qué se hace si no coincide"]},
    {"id": "llamadas", "titulo": "Llamadas telefónicas: qué responder y cómo", "tipo": "proceso", "cubrir": [
        "qué tipos de llamadas llegan (huéspedes que no pueden entrar, dudas, quejas, plataformas, proveedores…) y qué idioma se usa",
        "para cada tipo: qué se responde, qué datos se piden, qué se promete y qué no, y cuándo se escala",
        "qué hacer fuera de horario o si no se puede atender, y cómo se deja constancia de la llamada"]},
    {"id": "vending", "titulo": "Supervisión de la máquina de vending", "tipo": "proceso", "cubrir": [
        "cada cuánto se revisa y qué se comprueba (stock, funcionamiento, dinero, limpieza)",
        "qué se hace si falla o se vacía, a quién se avisa (empresa de vending o quien sea) y con qué datos",
        "qué es urgente y qué puede esperar"]},
    {"id": "plataformas", "titulo": "Incidencias con Booking y otras plataformas", "tipo": "proceso", "cubrir": [
        "cuál es el proceso para llamar o contactar con Booking (y con cada una de las otras: Airbnb, Hostelworld, Holidu, Expedia, Trip…): teléfono o extranet, horario, qué datos tener a mano",
        "cuáles son las incidencias habituales (huésped que no aparece, cancelaciones, cobros, quejas, reseñas, cambios de fechas…) y qué hacer en cada una, paso a paso",
        "las particularidades de cada plataforma que conviene saber (plazos, políticas, cómo se comunica con el huésped, qué no se debe hacer)"]},
    {"id": "faq", "titulo": "Preguntas frecuentes de huéspedes", "tipo": "faq", "cubrir": [
        "cuando la llaman, qué le suelen pedir o preguntar los huéspedes (parking, cuna, horarios, equipaje, wifi, ruido, taxis, dónde comer, cómo llegar…) y qué les responde exactamente",
        "las respuestas que NO están en la web de check-in o que cambian según el caso",
        "qué se les dice cuando la respuesta es «no» (p. ej. mascotas, fumar, early check-in) para que quede bien dicho"]},
    {"id": "telefonos", "titulo": "Teléfonos de interés", "tipo": "telefono", "cubrir": [
        "los gestores de la empresa (asesoría, administración): nombre, para qué se les llama, teléfono y horario",
        "las limpiadoras, el mantenimiento y los proveedores (fontanero, electricista, cerrajero, internet…): nombre, teléfono, horario y para qué casos",
        "cualquier otro contacto útil (vecinos, comunidad, vending, policía local…)"]},
    {"id": "escalado_admin", "titulo": "Escalado de problemas administrativos", "tipo": "escalado", "cubrir": [
        "las categorías de problema administrativo (pagos, facturas, reservas con errores, quejas graves, reclamaciones, documentación…)",
        "para CADA categoría: ejemplos reales, qué resuelve el sustituto y qué no, a quién avisar, por qué medio (llamada, WhatsApp, correo) y en qué horario, qué datos dar, qué hacer si no responde, y qué es urgente"]},
    {"id": "escalado_tecnico", "titulo": "Escalado de problemas técnicos (a Adrián)", "tipo": "escalado", "cubrir": [
        "las categorías de problema técnico (la app, Beds24, RPV, cerraduras y códigos, wifi, cámaras, sincronización de canales, correos automáticos…)",
        "para CADA categoría: ejemplos reales, qué puede probar el sustituto antes de avisar y qué no debe tocar, a Adrián cómo y cuándo avisarle, qué datos darle, qué hacer si no responde, y qué es urgente"]},
]
TEMAS_POR_ID = {t["id"]: t for t in TEMAS}

ESTRUCTURA_ESCALADO = ("Para las entradas de ESCALADO, escribe el contenido con estos apartados (los que se hayan contado): Ejemplos · Lo resuelve el sustituto · Lo que NO resuelve el sustituto · "
                       "Avisar a · Medio · Horario · Datos que hay que dar · Si no responde · Es urgente cuando…")


class ErrorEntrevista(Exception):
    def __init__(self, mensaje, status=400):
        super().__init__(mensaje)
        self.status = status


def _norm(texto):
    return kb._norm(texto).strip()


def _prompt_sistema(tema, titulos_existentes):
    cubrir = "\n".join(f"- {c}" for c in tema["cubrir"])
    existentes = "\n".join(f"- {t}" for t in titulos_existentes[:40]) or "(todavía nada)"
    extra = ("\n" + ESTRUCTURA_ESCALADO) if tema["tipo"] == "escalado" else ""
    return f"""Eres el entrevistador del sistema de gestión de ALC Homes (un hostal en Alicante y una casa de alquiler turístico). Entrevistas a IONE, que lleva el puesto de gestora del hostal, para recoger lo que sabe y que
una persona que la sustituya pueda hacer su trabajo con tus apuntes. Suele estar Adrián (el propietario) con ella. Hablas en español, cercano y claro, sin tecnicismos.

TEMA DE HOY: {tema['titulo']}.
Lo que hay que cubrir de este tema:
{cubrir}

Lo que ya está recogido (no lo repitas, a menos que haya algo nuevo que añadir):
{existentes}

REGLAS:
1. Haz UNA sola pregunta cada vez, concreta y fácil de contestar (quién, cuándo, cómo, qué pasa si…). Profundiza según lo que conteste: si menciona algo sin explicarlo (un paso, una persona, un sitio), pregunta por ello.
2. NO inventes nada ni rellenes huecos: solo apuntas lo que Ione o Adrián hayan dicho. Si algo es dudoso, pregúntalo.
3. Cuando lo último contestado dé para una entrada útil, devuélvela en «entradas». Cada entrada: tipo ({', '.join(kb.TIPOS)}), titulo corto, contenido (pasos numerados y claros, con qué comprobar, a quién avisar, datos a dar…), etiquetas (palabras que usaría un sustituto para buscarla) y propiedad («hostal», «primavera» o vacío si vale para todo). Si amplía una que ya existe, usa el mismo título.{extra}
4. Cuando creas que el tema está bien cubierto, pon «tema_cubierto»: true, haz un breve resumen y propón el siguiente tema.
5. Responde SIEMPRE con un único JSON: {{"pregunta": "...", "entradas": [], "tema_cubierto": false, "resumen": ""}}. «resumen» es una frase corta de lo aprendido en este turno (vacía si nada)."""


def _llamar_modelo(mensajes):
    clave = clave_gestion()
    if not clave:
        raise ErrorEntrevista("El asistente no está disponible (sin clave de IA).", 503)
    limite = time.time() + PRESUPUESTO_SEG
    saturada = False
    for modelo in (GROQ_MODEL_PRI, GROQ_MODEL_FALL):
        for modo_json in (True, False):
            restante = limite - time.time()
            if restante < 6:
                raise ErrorEntrevista("La entrevista tarda demasiado. Inténtalo de nuevo.", 504)
            cuerpo = {"model": modelo, "messages": mensajes, "temperature": 0.3, "max_tokens": 2200}
            if modo_json:
                cuerpo["response_format"] = {"type": "json_object"}
            try:
                resp = requests.post(GROQ_API_URL, headers={"Authorization": f"Bearer {clave}", "Content-Type": "application/json"}, json=cuerpo, timeout=min(35, restante - 2))
                if resp.status_code in (429, 503):
                    saturada = True
                    break
                if resp.status_code == 400 and modo_json:
                    continue
                resp.raise_for_status()
                texto = (resp.json()["choices"][0]["message"].get("content") or "").strip()
                texto = re.sub(r"^```(json)?|```$", "", texto, flags=re.M).strip()
                datos = json.loads(texto[texto.find("{"): texto.rfind("}") + 1])
                if isinstance(datos, dict):
                    return datos
            except Exception as e:
                logger.warning(f"[entrevista] El modelo {modelo} no devolvió un JSON válido: {e}")
                break
    if saturada:
        raise ErrorEntrevista("La IA está saturada ahora mismo (límite por minuto). Espera un minuto y vuelve a contestar.", 503)
    raise ErrorEntrevista("No he podido preparar la siguiente pregunta. Inténtalo de nuevo.", 502)


# ── Sesiones ───────────────────────────────────────────────────────────────────

def _col():
    if config.db is None:
        raise ErrorEntrevista("El Entrenamiento no está disponible ahora mismo", 503)
    return config.db.collection(COLECCION)


def _ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sesion_id(valor):
    valor = str(valor or "")
    return valor if re.fullmatch(r"[a-f0-9]{12}", valor) else ""


def cargar(sesion_id):
    sid = _sesion_id(sesion_id)
    if not sid:
        return None
    doc = _col().document(sid).get()
    if not doc.exists:
        return None
    d = doc.to_dict() or {}
    d["id"] = sid
    return d


def ultima_del_tema(tema_id):
    """La sesión más reciente de un tema (para continuarla otro día), o None."""
    try:
        todas = [dict(d.to_dict() or {}, id=d.id) for d in _col().stream()]
    except ErrorEntrevista:
        raise
    except Exception as e:
        logger.error(f"[entrevista] No se pudieron leer las sesiones: {e}")
        return None
    del_tema = [d for d in todas if d.get("tema") == tema_id]
    return max(del_tema, key=lambda d: d.get("actualizado", ""), default=None)


def resumen_temas():
    """Los temas con lo recogido de cada uno (para la pantalla de elegir tema)."""
    cuentas = {}
    for e in kb.entradas(None, forzar=True):
        c = cuentas.setdefault(e.get("tema") or "", {"borradores": 0, "aprobadas": 0})
        c["aprobadas" if e.get("estado") == "aprobado" else "borradores"] += 1
    return [{"id": t["id"], "titulo": t["titulo"], "tipo": t["tipo"], **cuentas.get(t["id"], {"borradores": 0, "aprobadas": 0})} for t in TEMAS]


# ── Guardar lo aprendido ─────────────────────────────────────────────────────────

def _guardar_borrador(entrada, tema, sesion_id, usuario):
    """Valida la entrada del modelo y la guarda como BORRADOR. Devuelve {id, titulo, tipo, accion} o None si no vale."""
    try:
        limpio = kb.validar({**entrada, "estado": "borrador", "orden": entrada.get("orden", 1000)})
    except kb.ErrorKB as e:
        logger.info(f"[entrevista] Entrada descartada ({e}): {str(entrada.get('titulo'))[:60]}")
        return None
    doc_id = "ent-" + hashlib.sha1(_norm(limpio["titulo"]).encode()).hexdigest()[:10]
    previo = next((e for e in kb.entradas(None, forzar=True) if e["id"] == doc_id), None)
    accion = "actualizado" if previo else "nuevo"
    if previo and previo.get("estado") == "aprobado":     # lo ya aprobado no se pisa: se propone una revisión aparte
        doc_id += "-rev"
        limpio["titulo"] = (limpio["titulo"] + " (revisión)")[:120]
        accion = "revisión"
    limpio.update(tema=tema["id"], origen="entrenamiento", sesion=sesion_id, actualizado=_ahora(), editado_por=str(usuario or "")[:40])
    config.db.collection(kb.COLECCION).document(doc_id).set(limpio)
    return {"id": doc_id, "titulo": limpio["titulo"], "tipo": limpio["tipo"], "accion": accion}


def turno(tema_id, texto, sesion_id=None, usuario=""):
    """Un turno de la entrevista. `texto` vacío = empezar (o retomar) el tema. Devuelve {sesion_id, pregunta, guardadas, tema_cubierto, resumen, mensajes}."""
    tema = TEMAS_POR_ID.get(tema_id)
    if not tema:
        raise ErrorEntrevista("Tema desconocido", 404)
    texto = str(texto or "").strip()[:MAX_CHARS_MENSAJE]
    sesion = cargar(sesion_id) if sesion_id else None
    if sesion is None:
        sesion = {"id": uuid.uuid4().hex[:12], "tema": tema_id, "mensajes": [], "creado": _ahora(), "por": str(usuario or "")[:40]}
    if sesion.get("tema") != tema_id:
        raise ErrorEntrevista("Esa conversación es de otro tema", 400)
    mensajes = list(sesion.get("mensajes") or [])
    if texto:
        mensajes.append({"rol": "ione", "texto": texto, "ts": _ahora(), "por": str(usuario or "")[:40]})
    elif mensajes and mensajes[-1]["rol"] == "entrevistador":
        return {"sesion_id": sesion["id"], "pregunta": mensajes[-1]["texto"], "guardadas": [], "tema_cubierto": bool(sesion.get("cubierto")), "resumen": "", "mensajes": mensajes}

    titulos = [e.get("titulo") for e in kb.entradas(None, forzar=True) if e.get("tema") == tema_id or e.get("tipo") == tema["tipo"]]
    modelo_msgs = [{"role": "system", "content": _prompt_sistema(tema, [t for t in titulos if t])}]
    for m in mensajes[-MENSAJES_AL_MODELO:]:
        modelo_msgs.append({"role": "assistant" if m["rol"] == "entrevistador" else "user", "content": m["texto"][:MAX_CHARS_MENSAJE]})
    if not any(m["role"] == "user" for m in modelo_msgs):
        modelo_msgs.append({"role": "user", "content": "Empecemos con este tema. Haz la primera pregunta."})

    respuesta = _llamar_modelo(modelo_msgs)
    pregunta = " ".join(str(respuesta.get("pregunta") or "").split())[:700]
    if not pregunta:
        raise ErrorEntrevista("No he podido preparar la siguiente pregunta. Inténtalo de nuevo.", 502)
    guardadas = []
    entradas = respuesta.get("entradas") if isinstance(respuesta.get("entradas"), list) else []
    for e in entradas[:MAX_ENTRADAS_POR_TURNO]:
        if isinstance(e, dict):
            g = _guardar_borrador(e, tema, sesion["id"], usuario)
            if g:
                guardadas.append(g)
    if guardadas:
        kb.olvidar_cache()
    cubierto = bool(respuesta.get("tema_cubierto"))
    mensajes.append({"rol": "entrevistador", "texto": pregunta, "ts": _ahora()})
    sesion.update(mensajes=mensajes[-MAX_MENSAJES_GUARDADOS:], actualizado=_ahora(), cubierto=cubierto or bool(sesion.get("cubierto")),
                  entradas=(sesion.get("entradas") or []) + [g["id"] for g in guardadas])
    _col().document(sesion["id"]).set({k: v for k, v in sesion.items() if k != "id"})
    return {"sesion_id": sesion["id"], "pregunta": pregunta, "guardadas": guardadas, "tema_cubierto": cubierto,
            "resumen": " ".join(str(respuesta.get("resumen") or "").split())[:300], "mensajes": mensajes[-MAX_MENSAJES_GUARDADOS:]}

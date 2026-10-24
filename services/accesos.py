"""
services/accesos.py — Perfiles de la app de gestión, sus PIN y sus permisos (fase 1: el modelo; todavía NO se aplica a las rutas).

Cada PIN identifica un PERFIL y cada perfil es un conjunto de PERMISOS. Las rutas (fase 2) pedirán un permiso concreto, nunca un nombre de perfil, y así
el modelo se puede portar a la plataforma multicliente (roles `normal`/`admin` de docs/DISENO.md = conjuntos de permisos distintos). Aquí solo se
define y se prueba la tabla; hasta la fase 2 la app sigue funcionando con los dos PIN de siempre (MOBILE_APP_PIN y MOBILE_APP_PIN_ADMIN).

Perfiles (confirmados por Adrián el 2026-10-24):
  propietario (1379, Ione y Adrián)  → todo
  gestor_primavera (9999)            → como gestor + precios + Finanzas de La Casa de la Primavera
  gestor (2486, resto de gestores)   → calendario, reservas, asistente acotado
  sustituto (2589, quien sustituye a Ione) → calendario, reservas, asistente acotado, Operativa de Gestión, envío de emergencia con el PIN 1432 (y el
                                       asistente le dice ese PIN); toda su actividad queda registrada (`auditado`)
  limpieza (4268)                    → solo el calendario de la casa (ocupado/libre) para marcar incidencias de limpieza

Los PIN vienen de variables de entorno con estos valores por defecto (en esta fase Adrián prefiere que estén «recordados»; más adelante se repensará
todo el acceso). En memoria se guarda solo su HASH (PBKDF2-SHA256 con sal por perfil y un secreto del servidor), y se comparan en tiempo constante.
Un PIN de 4 dígitos tiene 10.000 combinaciones: lo que lo protege de verdad es el límite de intentos (mobile_routes.pin_bloqueado).
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass

# ── Permisos ──────────────────────────────────────────────────────────────────
CALENDARIO = "calendario.ver"
RESERVAS = "reservas.escribir"                 # crear, editar, cancelar y bloquear
PRECIOS = "precios.escribir"                   # precios y disponibilidad
ASISTENTE = "asistente.usar"
ASISTENTE_COMPLETO = "asistente.alcance_completo"   # sin acotar el ámbito (el resto solo calendario, reservas, la app y el puesto de gestora)
FINANZAS_PRIMAVERA = "finanzas.primavera"
FINANZAS_HOSTAL = "finanzas.hostal"
FACTURAS = "finanzas.facturas"                 # facturas, Asesor Contable y Desglose de la comisión
EMERGENCIA = "emergencia.enviar"               # 🚨: correo de códigos y mensaje del enlace del parte
EMERGENCIA_SIN_PIN = "emergencia.sin_pin"      # sin teclear el PIN de seguridad (1432)
EMERGENCIA_VER_PIN = "emergencia.ver_pin"      # el asistente le dice el PIN de seguridad
OPERATIVA = "operativa.ver"
ENTRENAMIENTO = "operativa.entrenamiento"
HISTORIAL = "operativa.historial"
INCIDENCIAS_VER = "limpieza.ver_incidencias"   # ver las incidencias de limpieza en el calendario de gestión
LIMPIEZA_CALENDARIO = "limpieza.calendario"    # el calendario ocupado/libre de la limpiadora
LIMPIEZA_MARCAR = "limpieza.marcar"
PRIMAVERA = "propiedad.primavera"              # ver cualquier cosa de La Casa de la Primavera: calendario, reservas, asistente, incidencias, emergencias
SISTEMA = "sistema.diagnostico"                # rutas de diagnóstico y mantenimiento (/logs, /test-*, /debug-*, /fix-zero-prices…)

TODOS = (CALENDARIO, RESERVAS, PRECIOS, ASISTENTE, ASISTENTE_COMPLETO, FINANZAS_PRIMAVERA, FINANZAS_HOSTAL, FACTURAS, EMERGENCIA, EMERGENCIA_SIN_PIN,
         EMERGENCIA_VER_PIN, OPERATIVA, ENTRENAMIENTO, HISTORIAL, INCIDENCIAS_VER, LIMPIEZA_CALENDARIO, LIMPIEZA_MARCAR, SISTEMA, PRIMAVERA)


@dataclass(frozen=True)
class Perfil:
    id: str
    nombre: str
    permisos: frozenset
    auditado: bool = False       # toda su actividad se registra en el historial de sustitución (no se puede desactivar desde el perfil)
    pide_nombre: bool = False    # el PIN es compartido: al entrar se pide el nombre de quien lo usa

    def tiene(self, permiso):
        return permiso in self.permisos


_GESTOR = frozenset({CALENDARIO, RESERVAS, ASISTENTE})

PERFILES = {
    "propietario": Perfil("propietario", "Propietario (Ione y Adrián)", frozenset(p for p in TODOS if p not in (LIMPIEZA_CALENDARIO, LIMPIEZA_MARCAR))),
    "gestor_primavera": Perfil("gestor_primavera", "Gestor de la Casa de la Primavera", _GESTOR | {PRECIOS, FINANZAS_PRIMAVERA, PRIMAVERA, INCIDENCIAS_VER}),
    "gestor": Perfil("gestor", "Gestor", _GESTOR),
    "sustituto": Perfil("sustituto", "Sustituto de Ione", _GESTOR | {EMERGENCIA, EMERGENCIA_VER_PIN, OPERATIVA}, auditado=True, pide_nombre=True),
    "limpieza": Perfil("limpieza", "Limpieza", frozenset({LIMPIEZA_CALENDARIO, LIMPIEZA_MARCAR})),
}

_PIN_POR_PERFIL = {
    "propietario": os.environ.get("PIN_PROPIETARIO", "1379"),
    "gestor": os.environ.get("PIN_GESTOR", "2486"),
    "gestor_primavera": os.environ.get("PIN_GESTOR_PRIMAVERA", "9999"),
    "sustituto": os.environ.get("PIN_SUSTITUTO", "2589"),
    "limpieza": os.environ.get("PIN_LIMPIEZA", "4268"),
}
# El PIN de seguridad del envío de emergencia de códigos (1432) sigue siendo config.PIN_ENVIO_CODIGOS: no identifica a nadie, solo se exige a quien no tiene
# EMERGENCIA_SIN_PIN.

_PEPPER = os.environ.get("ACCESO_PEPPER", "alchomes-app-gestion").encode()
_ITERACIONES = 100_000


def _hash(perfil_id, pin):
    return hashlib.pbkdf2_hmac("sha256", str(pin).encode(), _PEPPER + b":" + perfil_id.encode(), _ITERACIONES)


# Solo se guardan hashes: los PIN en claro no se conservan en memoria tras el arranque.
_HASHES = {pid: _hash(pid, pin) for pid, pin in _PIN_POR_PERFIL.items() if pin}
del _PIN_POR_PERFIL


_VERIFICADOS = {}   # sha256(PIN) → id de perfil, solo de PIN que ya acertaron (para no repetir el PBKDF2 en cada petición de una app que manda el PIN cada vez)


def perfil_por_pin(pin):
    """El Perfil al que pertenece ese PIN, o None. Comparación en tiempo constante contra todos los hashes (no se corta al primer acierto)."""
    pin = str(pin or "").strip()
    if not pin or len(pin) > 12:
        return None
    clave = hashlib.sha256(pin.encode()).hexdigest()
    if clave in _VERIFICADOS:
        return PERFILES[_VERIFICADOS[clave]]
    encontrado = None
    for pid, esperado in _HASHES.items():
        if hmac.compare_digest(_hash(pid, pin), esperado):
            encontrado = PERFILES[pid]
    if encontrado:
        _VERIFICADOS[clave] = encontrado.id
    return encontrado


# ── Sesión firmada ──────────────────────────────────────────────────────────────
# Al entrar con el PIN el servidor emite un token con el perfil y el nombre, firmado con un secreto que solo conoce él. El navegador no puede cambiar
# ni el perfil ni el nombre (es lo que hace fiable el historial del sustituto). Sin ACCESO_SESION_SECRETO se genera uno al arrancar: al reiniciar el
# servidor las sesiones caducan y se vuelve a entrar con el PIN.
_SECRETO = (os.environ.get("ACCESO_SESION_SECRETO") or "").encode() or secrets.token_bytes(32)
SESION_HORAS = 12


def _b64(datos):
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def _firma_sesion(cuerpo):
    return _b64(hmac.new(_SECRETO, cuerpo.encode(), hashlib.sha256).digest())


def crear_sesion(perfil_id, nombre=None, ahora=None):
    ahora = time.time() if ahora is None else ahora
    carga = {"p": perfil_id, "n": nombre or "", "e": int(ahora + SESION_HORAS * 3600)}
    cuerpo = _b64(json.dumps(carga, separators=(",", ":"), ensure_ascii=False).encode())
    return f"{cuerpo}.{_firma_sesion(cuerpo)}"


def verificar_sesion(token, ahora=None):
    """(Perfil, nombre) si el token es auténtico y no ha caducado; None si no."""
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 600:
        return None
    cuerpo, firma = token.split(".")
    if not hmac.compare_digest(firma, _firma_sesion(cuerpo)):
        return None
    try:
        carga = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)).decode())
        perfil = PERFILES.get(carga["p"])
        vigente = float(carga["e"]) > (time.time() if ahora is None else ahora)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None
    if not perfil or not vigente:
        return None
    return perfil, (carga.get("n") or None)


_NOMBRE = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñÇç][A-Za-zÁÉÍÓÚÜÑáéíóúüñÇç .'\-]{1,39}$")


def normalizar_nombre(nombre):
    """Nombre de quien usa un PIN compartido, limpio y con mayúscula inicial en cada palabra; None si no es un nombre aceptable (2-40 letras)."""
    limpio = " ".join(str(nombre or "").split())
    if not _NOMBRE.match(limpio):
        return None
    return " ".join(w[:1].upper() + w[1:] for w in limpio.split(" "))


def tiene(perfil, permiso):
    """True si el perfil (un Perfil, un id o None) tiene ese permiso."""
    if isinstance(perfil, str):
        perfil = PERFILES.get(perfil)
    return bool(perfil) and perfil.tiene(permiso)

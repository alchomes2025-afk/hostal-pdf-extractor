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
import hashlib
import hmac
import os
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
SISTEMA = "sistema.diagnostico"                # rutas de diagnóstico y mantenimiento (/logs, /test-*, /debug-*, /fix-zero-prices…)

TODOS = (CALENDARIO, RESERVAS, PRECIOS, ASISTENTE, ASISTENTE_COMPLETO, FINANZAS_PRIMAVERA, FINANZAS_HOSTAL, FACTURAS, EMERGENCIA, EMERGENCIA_SIN_PIN,
         EMERGENCIA_VER_PIN, OPERATIVA, ENTRENAMIENTO, HISTORIAL, INCIDENCIAS_VER, LIMPIEZA_CALENDARIO, LIMPIEZA_MARCAR, SISTEMA)


@dataclass(frozen=True)
class Perfil:
    id: str
    nombre: str
    permisos: frozenset
    auditado: bool = False       # toda su actividad se registra en el historial de sustitución (no se puede desactivar desde el perfil)
    pide_nombre: bool = False    # el PIN es compartido: al entrar se pide el nombre de quien lo usa

    def tiene(self, permiso):
        return permiso in self.permisos


_GESTOR = frozenset({CALENDARIO, RESERVAS, ASISTENTE, INCIDENCIAS_VER})

PERFILES = {
    "propietario": Perfil("propietario", "Propietario (Ione y Adrián)", frozenset(p for p in TODOS if p not in (LIMPIEZA_CALENDARIO, LIMPIEZA_MARCAR))),
    "gestor_primavera": Perfil("gestor_primavera", "Gestor de la Casa de la Primavera", _GESTOR | {PRECIOS, FINANZAS_PRIMAVERA}),
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


def perfil_por_pin(pin):
    """El Perfil al que pertenece ese PIN, o None. Comparación en tiempo constante contra todos los hashes (no se corta al primer acierto)."""
    pin = str(pin or "").strip()
    if not pin or len(pin) > 12:
        return None
    encontrado = None
    for pid, esperado in _HASHES.items():
        if hmac.compare_digest(_hash(pid, pin), esperado):
            encontrado = PERFILES[pid]
    return encontrado


def tiene(perfil, permiso):
    """True si el perfil (un Perfil, un id o None) tiene ese permiso."""
    if isinstance(perfil, str):
        perfil = PERFILES.get(perfil)
    return bool(perfil) and perfil.tiene(permiso)

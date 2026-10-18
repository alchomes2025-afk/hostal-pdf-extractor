"""
services/limites.py — Límites de uso en memoria para los endpoints públicos (web de check-in).

El backend corre con un solo worker de gunicorn, así que un contador en memoria vale; se reinicia al reiniciar el
servicio. Sirve para frenar abusos (probar nombres a ritmo de máquina, o usar el formulario para gastar los
correos diarios de Gmail), no es un control de seguridad por sí solo.
"""
import time
from collections import deque

from flask import request

_eventos = {}   # clave -> deque de instantes (epoch) de los eventos aceptados


def permitir(reglas, ahora=None):
    """
    reglas: lista de (clave, máximo, ventana_en_segundos). Si NINGUNA ha llegado a su máximo, anota el evento en
    todas y devuelve True; si alguna sí, devuelve False sin anotar nada (un rechazo no gasta cupo de las otras).
    """
    ahora = time.time() if ahora is None else ahora
    colas = []
    for clave, maximo, ventana in reglas:
        cola = _eventos.setdefault(clave, deque())
        while cola and ahora - cola[0] > ventana:
            cola.popleft()
        if len(cola) >= maximo:
            return False
        colas.append(cola)
    for cola in colas:
        cola.append(ahora)
    if len(_eventos) > 5000:   # limpieza ocasional de claves sin eventos recientes
        for clave in [k for k, v in _eventos.items() if not v]:
            del _eventos[clave]
    return True


def cantidad(clave, ventana, ahora=None):
    """Eventos anotados con esa clave en los últimos `ventana` segundos (no anota nada)."""
    ahora = time.time() if ahora is None else ahora
    cola = _eventos.get(clave)
    if not cola:
        return 0
    while cola and ahora - cola[0] > ventana:
        cola.popleft()
    return len(cola)


def anotar(clave, ahora=None):
    """Anota un evento con esa clave (p. ej. un intento fallido) sin comprobar ningún máximo."""
    _eventos.setdefault(clave, deque()).append(time.time() if ahora is None else ahora)


def ip_cliente():
    """
    IP del visitante para los límites. Revisión de seguridad 2026-10-18: el proxy de Render AÑADE a X-Forwarded-For en
    vez de reemplazarlo, así que el PRIMER valor lo pone quien llama y cambiándolo en cada petición se saltaba el límite.
    Se prefiere la cabecera que fija Cloudflare (CF-Connecting-IP / True-Client-IP); si no viene, la ÚLTIMA de
    X-Forwarded-For (la añadida por el proxy).
    """
    for cabecera in ("CF-Connecting-IP", "True-Client-IP"):
        valor = request.headers.get(cabecera, "").strip()
        if valor:
            return valor
    adelantada = [x.strip() for x in request.headers.get("X-Forwarded-For", "").split(",") if x.strip()]
    return adelantada[-1] if adelantada else (request.remote_addr or "desconocida")

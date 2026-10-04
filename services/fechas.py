"""
services/fechas.py — Fecha y hora "de negocio" en zona Europe/Madrid.

El servidor (Render) corre en UTC, así que `date.today()` / `datetime.now()`
sin zona devuelven el día UTC: entre las 00:00 y las 02:00 de Madrid (01:00 en
invierno) el servidor todavía está en el día anterior, y el cambio de día le
llega a las 02:00 de Madrid. Todo lo que dependa de "hoy" para el hostal
(resumen diario, avisos, consultas a Beds24) debe usar estas funciones.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

MADRID_TZ = ZoneInfo("Europe/Madrid")


def ahora_madrid() -> datetime:
    return datetime.now(MADRID_TZ)


def hoy_madrid() -> date:
    return ahora_madrid().date()

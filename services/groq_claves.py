"""
services/groq_claves.py — Qué cuenta de Groq usa cada parte del sistema (el cupo por minuto es por cuenta).

  - huéspedes del cliente (chat de la web de check-in): GROQ_API_KEY, en routes/chat.py.
  - gestión del cliente (asistente de la app de gestión y lectura de facturas): `clave_gestion()`.
  - trabajo interno de HostalFácil (desarrollo y pruebas): una cuenta aparte que el backend de un cliente NUNCA usa.

`clave_gestion()` usa GROQ_API_KEY_GESTION. Solo si falta esa variable cae a GROQ_API_KEY (la de los huéspedes), para que el asistente
no se quede sin servicio; en ese caso el trabajo de gestión vuelve a competir con el de los huéspedes por el mismo cupo.
"""
from config import GROQ_API_KEY, GROQ_API_KEY_GESTION


def clave_gestion():
    return GROQ_API_KEY_GESTION or GROQ_API_KEY

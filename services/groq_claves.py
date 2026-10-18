"""
services/groq_claves.py — Qué cuenta de Groq usa cada parte del sistema (el cupo por minuto es por cuenta).

  - huéspedes del cliente (chat de la web de check-in): GROQ_API_KEY, en routes/chat.py.
  - gestión del cliente (asistente de la app de gestión y lectura de facturas): `clave_gestion()`.
  - trabajo interno de HostalFácil (desarrollo y pruebas): GROQ_API_KEY_INTERNA, que NO se usa para atender a un cliente.

`clave_gestion()` usa GROQ_API_KEY_GESTION. TEMPORAL: mientras esa variable no exista, tira de GROQ_API_KEY_INTERNA (la que el asistente
ya usa en producción) y, en último caso, de GROQ_API_KEY; en cuanto GESTION esté en Render, se retira ese respaldo.
"""
from config import GROQ_API_KEY, GROQ_API_KEY_GESTION, GROQ_API_KEY_INTERNA


def clave_gestion():
    return GROQ_API_KEY_GESTION or GROQ_API_KEY_INTERNA or GROQ_API_KEY

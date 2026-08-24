# CLAUDE.md — hostal-pdf-extractor

## Qué es este proyecto
Backend del sistema de self check-in automatizado para el hostal ALC Homes San Blas (5 habitaciones). También aloja la app móvil de gestión de reservas ("Alchomes Manager").

## Repos relacionados (bajo la organización alchomes2025-afk)
- Este repo: `hostal-pdf-extractor` — backend Flask, desplegado en Render: https://hostal-pdf-extractor.onrender.com
- Frontend check-in de huéspedes: `alc-homes-checkin` — Firebase Hosting: https://alc-homes-checkin.web.app
- Historial/admin: `alc-homes-historial` — https://alc-homes-historial.web.app

## Flujo del check-in automático (8 servicios integrados)
Booking.com → Beds24 (property ID 339751, owner 170906) → registroparteviajeros.com/RPV (room IDs 702395–702399) → este backend (Render) → Firebase Hosting (frontend) → notificación WhatsApp vía CallMeBot. También intervienen Groq API y Make.com.

## Infraestructura y cuentas
- Cuenta de la app de check-in: `juanantloz@gmail.com` — proyecto "Alchomes PDF Extractor" en Google Cloud, credenciales OAuth de Gmail.
- Gmail OAuth para lectura de correos fue **sustituido** por polling directo a la API de RPV (endpoint `/procesar-partes-hoy`) — no reintroducir el flujo OAuth antiguo sin motivo.
- La clave de Groq API se movió a un proxy en el backend después de que GitHub auto-revocara una clave expuesta en el repo público — **nunca** hardcodear claves API en el código, aunque el repo sea privado.
- Gmail `alchomes2025@gmail.com`: recibe notificaciones de reservas por canal vía plus-addressing (`alchomes2025+agoda@gmail.com`, etc.) para el parsing de Make.com.
- Gmail `alchomes2025guest@gmail.com`: cuenta dedicada para el correo de solicitud de check-in al huésped (antes era un alias `+guest`, se separó a petición del propietario).

## App móvil (Alchomes Manager) — vive en este mismo repo
- Blueprint Flask: `mobile_routes.py`
- Frontend: GitHub Pages
- **Sincronización con Beds24**: peticiones fragmentadas en bloques de 60 días (resuelve un bug de truncamiento silencioso a 100 reservas si se pide todo de golpe). Consulta explícita adicional para reservas con estado "cancelled".
- **Precios**: se guardan en Firestore como overrides, porque `GET /inventory/rooms/calendar` de Beds24 devuelve arrays vacíos.
- **Sync delta**: usa el ID de reserva como clave.
- **Bloqueo de fechas**: se hace creando reservas reales en Beds24 con email `bloqueo@bloqueo.com` (no hay endpoint nativo de bloqueo).
- **Monitorización de tokens**: estado en Firestore (`system_state/token_health`), alertas por WhatsApp vía CallMeBot a dos números.
- **Logging**: actividad registrada en Firestore. Página de diagnóstico en `/test-sync`.
- **IMPORTANTE**: `MOBILE_BEDS24_TOKEN` y `BEDS24_REFRESH_TOKEN` están separados a propósito, tras un incidente en el que intercambiarlos rompió el sistema de check-in principal. Nunca unificarlos ni reutilizar uno para el otro.

## Frontend de check-in (por qué Firebase y no Vercel)
Se eligió Firebase Hosting sobre Vercel por compatibilidad con el filtro de seguridad de URLs de Booking.com. No migrar a otro hosting sin verificar ese requisito primero.
- Multilenguaje: ES/EN/FR/DE/VAL
- Solo revela habitación/PIN cuando el estado es "staying" (huésped alojado), no antes.
- Autopoll cada 45s durante el estado "pre_checkin".
- Usa `todayISOMadrid()` para evitar bugs de zona horaria (no usar `toISOString()` a secas — ya causó bugs de fecha equivocada).
- La fecha de llegada se resuelve en JavaScript antes de inyectarla, no en el backend.

## Historial/admin (alc-homes-historial)
Registros de interacción respaldados por Firestore. Login con Google Sign-In estaba planeado — verificar si ya se implementó antes de asumir su estado.

## Estilo de trabajo de Adrián
- Sin entorno local hasta ahora — viene de trabajar 100% desde GitHub web UI, con commits directos a `main` que disparan auto-deploy en Render. Verificar en local con Claude Code antes de hacer push.
- Antes de cualquier cambio con impacto en producción (sobre todo tokens de Beds24 o el flujo de check-in en vivo), avisar explícitamente y confirmar antes de hacer commit/push.
- Ya hubo un incidente grave por intercambiar tokens de Beds24 — tratar cualquier cambio relacionado con `MOBILE_BEDS24_TOKEN` / `BEDS24_REFRESH_TOKEN` con máxima precaución y confirmación explícita.

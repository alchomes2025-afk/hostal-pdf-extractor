# CLAUDE.md — hostal-pdf-extractor

## Qué es este proyecto
Backend del sistema de self check-in automatizado para **dos propiedades**: el hostal ALC Homes San Blas (5 habitaciones) y La Casa de la Primavera (vivienda completa en Gran Alacant, añadida agosto 2026). También aloja la app móvil de gestión de reservas ("Alchomes Manager").

## Repos relacionados (bajo la organización alchomes2025-afk)
- Este repo: `hostal-pdf-extractor` — backend Flask, desplegado en Render: https://hostal-pdf-extractor.onrender.com
- Frontend check-in de huéspedes: `alc-homes-checkin` — Firebase Hosting: https://alc-homes-checkin.web.app
- Historial/admin: `alc-homes-historial` — https://alc-homes-historial.web.app

## Estructura del backend (modularizado, septiembre 2026)
`app.py` es solo el punto de entrada (crea la app Flask y registra blueprints). Toda la lógica vive en:
- `config.py` — env vars, `ROOM_CONFIG`, `BEDS24_PROPERTY_IDS`, RPV, Firestore, reservas de prueba (`TEST_BOOKINGS`)
- `services/` — `whatsapp.py`, `beds24.py`, `rpv.py`, `resumen.py` (lógica de negocio, sin rutas)
- `routes/` — un Blueprint por grupo de endpoints (`checkin.py`, `chat.py`, `resumen_routes.py`, `historial.py`, `watchdog.py`, `debug_diag.py`, `misc.py`)

## Dos propiedades en Beds24
- **ALC Homes San Blas** (hostal): `BEDS24_PROPERTY_ID=339751`, 5 habitaciones (`702395`–`702399`), check-in 15:00 / check-out 12:00.
- **La Casa de la Primavera** (Gran Alacant, Santa Pola): `BEDS24_PROPERTY_ID_CASA_PRIMAVERA=349341`, 1 unidad (`room_id 720841`), vivienda completa. Acceso: código fijo de la urbanización (2308, no secreto) + cajetín de llaves físico (código dinámico, `PIN_CASA_PRIMAVERA`) — NO es una cerradura electrónica como en el hostal.
- `BEDS24_PROPERTY_IDS` (config.py) recorre ambas propiedades donde hace falta buscar/sincronizar reservas de cualquiera de las dos (`buscar_booking_por_ref`, `obtener_bookings_dia_beds24`, `mobile_routes.py`).
- RPV (registroparteviajeros.com): **dos cuentas separadas**, `RPV_API_KEY` (hostal) y `RPV_API_KEY_CASA_PRIMAVERA`, resueltas por habitación vía `RPV_API_KEY_MAP`.
- `RPV_LINKS["720841"]` ya configurado (septiembre 2026).

## Identificación de reserva sin número fiable (La Casa de la Primavera)
La Casa de la Primavera recibe reservas de Booking, Airbnb y Holidu — cada plataforma da su propio número al huésped, y no hay un campo único en Beds24 donde buscarlo de forma fiable (a diferencia del hostal, solo Booking.com).

- **`GET /check-in?ref=...`** (routes/checkin.py) prueba primero `buscar_booking_por_ref()` (número/referencia, cualquier campo de Beds24, recursivo). Si no encuentra nada, reintenta automáticamente `buscar_booking_por_nombre()` (services/beds24.py) interpretando `ref` como el nombre del huésped — normaliza mayúsculas/acentos/orden de palabras y restringe la búsqueda a una ventana estrecha de días de llegada (hoy-2 a hoy+4) para evitar falsos positivos entre huéspedes con nombres parecidos. Si hay más de una coincidencia en esa ventana, responde `409 {"error": "nombre_ambiguo"}` en vez de adivinar.
- Esto NO usa Groq ni tolera erratas de escritura — es solo normalización determinista. La tolerancia a erratas vive en el pipeline de email (siguiente punto), que si hace falta puede evolucionar a llamar también a Groq para esto en el futuro.
- **Pipeline externo por email** (fuera de este repo, vive en Google Apps Script + Make.com): cuenta `alchomes2025guest@gmail.com` recibe correos de huéspedes que no pueden usar el enlace directo (algunas plataformas bloquean el link en el mensaje de bienvenida). Apps Script empuja cada correo nuevo a un Webhook de Make cada minuto; Make llama a Groq para extraer nombre/número/plataforma del asunto, consulta `GET /check-in?ref=...` (público, sin token) y responde al huésped con el enlace + el `book_id` de la respuesta. **`book_id` y el endpoint público `/check-in` son un contrato con ese pipeline — si se renombran o se protege el endpoint con token, avisar antes, se rompe silenciosamente sin que este repo lo note.**

## Infraestructura y cuentas
- La clave de Groq API se movió a un proxy en el backend después de que GitHub auto-revocara una clave expuesta en el repo público — **nunca** hardcodear claves API en el código, aunque el repo sea privado.
- Gmail `alchomes2025@gmail.com`: recibe notificaciones de reservas por canal vía plus-addressing (`alchomes2025+agoda@gmail.com`, etc.) para el parsing de Make.com.
- Gmail `alchomes2025guest@gmail.com`: cuenta dedicada para el correo de solicitud de check-in al huésped (antes era un alias `+guest`, se separó a petición del propietario).
- **El sistema legado de Gmail/PDF (OAuth, lectura de adjuntos, envío de código por Booking.com Messages vía `/extraer`, `/procesar-partes-hoy`, `/oauth/*`, `/test`, `/debug`) se ELIMINÓ por completo en septiembre 2026** — confirmado obsoleto, sustituido íntegramente por el polling directo a la API de RPV (`/check-in`, `/watchdog`). No reintroducirlo. Las variables `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_REFRESH_TOKEN`/`REDIRECT_URI`/`PDF_PASSWORD` en Render quedaron sin uso (no se han borrado del entorno por si acaso, pero el código ya no las lee).

## App móvil (Alchomes Manager) — vive en este mismo repo
- Blueprint Flask: `mobile_routes.py`
- Frontend: `mobile-app/index.html` (GitHub Pages)
- **Multi-propiedad** (añadido septiembre 2026): `PROPERTY_IDS` + `ROOM_PROPERTY_MAP` en `mobile_routes.py`, mismo patrón que el backend principal. La Casa de la Primavera aparece en el calendario con una fila separadora visual ("🌸 La Casa de la Primavera") para no confundirla con una habitación más del hostal.
- **Sincronización con Beds24**: peticiones fragmentadas en bloques de 60 días (resuelve un bug de truncamiento silencioso a 100 reservas si se pide todo de golpe), repetidas por cada propiedad de `PROPERTY_IDS`. Consulta explícita adicional para reservas con estado "cancelled".
- **Precios**: se guardan en Firestore como overrides, porque `GET /inventory/rooms/calendar` de Beds24 devuelve arrays vacíos. No hay overrides de precio cargados aún para La Casa de la Primavera.
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
- **Multi-propiedad** (septiembre 2026): `welcomeCard()` y `buildSystemPrompt()` (asistente virtual) detectan `booking.room_id === '720841'` y usan textos/`FAQ_DOCUMENT_CASA_PRIMAVERA` propios de La Casa de la Primavera en vez de los del hostal — nunca mezclar direcciones/WiFi/instrucciones de una propiedad con la otra.

## Historial/admin (alc-homes-historial)
Registros de interacción respaldados por Firestore. **Login con Google Sign-In implementado en septiembre 2026** (Firebase Auth, proyecto `alc-homes-checkin`), restringido por email a `alchomes2025@gmail.com`. Es solo un filtro visual en el frontend — el backend (`/historial`) sigue exigiendo el token compartido de siempre (`API_TOKEN`/`TEST_TOKEN`) por debajo; no se implementó verificación de token de Firebase en el backend. Si se quiere hacer más robusto, avisar antes de tocar `routes/historial.py`.

## Estilo de trabajo de Adrián
- Sin entorno local hasta ahora — viene de trabajar 100% desde GitHub web UI, con commits directos a `main` que disparan auto-deploy en Render. Verificar en local con Claude Code antes de hacer push.
- Antes de cualquier cambio con impacto en producción (sobre todo tokens de Beds24 o el flujo de check-in en vivo), avisar explícitamente y confirmar antes de hacer commit/push.
- Ya hubo un incidente grave por intercambiar tokens de Beds24 — tratar cualquier cambio relacionado con `MOBILE_BEDS24_TOKEN` / `BEDS24_REFRESH_TOKEN` con máxima precaución y confirmación explícita.

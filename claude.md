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
- `services/` — `whatsapp.py`, `beds24.py`, `rpv.py`, `resumen.py`, `resumen_programado.py`, `checkins_ultima_hora.py`, `hostelworld_avisos.py`, `registro_completado_avisos.py`, `ical_beds24.py`, `guest_match.py`, `fechas.py` (lógica de negocio, sin rutas)
- `routes/` — un Blueprint por grupo de endpoints (`checkin.py`, `chat.py`, `resumen_routes.py`, `historial.py`, `watchdog.py`, `debug_diag.py`, `misc.py`)

## Dos propiedades en Beds24
- **ALC Homes San Blas** (hostal): `BEDS24_PROPERTY_ID=339751`, 5 habitaciones (`702395`–`702399`), check-in 15:00 / check-out 12:00.
- **La Casa de la Primavera** (Gran Alacant, Santa Pola): `BEDS24_PROPERTY_ID_CASA_PRIMAVERA=349341`, 1 unidad (`room_id 720841`), vivienda completa. Acceso: código fijo de la urbanización (2308, no secreto) + cajetín de llaves físico (código dinámico, `PIN_CASA_PRIMAVERA`) — NO es una cerradura electrónica como en el hostal.
- `BEDS24_PROPERTY_IDS` (config.py) recorre ambas propiedades donde hace falta buscar/sincronizar reservas de cualquiera de las dos (`buscar_booking_por_ref`, `obtener_bookings_dia_beds24`, `mobile_routes.py`).
- RPV (registroparteviajeros.com): **dos cuentas separadas**, `RPV_API_KEY` (hostal) y `RPV_API_KEY_CASA_PRIMAVERA`, resueltas por habitación vía `RPV_API_KEY_MAP`.
- `RPV_LINKS["720841"]` ya configurado (septiembre 2026).

## Identificación de reserva: nombre completo, no número (septiembre 2026)
La Casa de la Primavera recibe reservas de Booking, Airbnb y Holidu — cada plataforma da su propio número al huésped, y no hay un campo único en Beds24 donde buscarlo de forma fiable. Por eso el campo principal de la web de check-in dejó de ser "número de reserva" y pasó a ser **nombre completo**, para las dos propiedades por igual (más simple de mantener que tener dos UX distintas).

- **`GET /check-in?ref=...`** (routes/checkin.py) prueba primero `buscar_booking_por_ref()` (número/referencia, cualquier campo de Beds24, recursivo — sigue funcionando si el huésped prefiere usar su número). Si no encuentra nada, reintenta con `buscar_booking_por_nombre()` (services/beds24.py) interpretando `ref` como nombre.
- `buscar_booking_por_nombre()` NO pide fecha al huésped — el sistema ya la sabe: solo considera candidatos con llegada **hoy o mañana** (el enlace se envía el día antes o el mismo día), o huéspedes **ya alojados** (llegada ≤ hoy ≤ salida). Evita comparar contra reservas de todo el año.
- El emparejamiento en sí vive en **`services/guest_match.py`** (nuevo módulo, sin dependencia de Beds24): primero normalización determinista (mayúsculas/acentos/orden de palabras) — si hay una única coincidencia, responde al instante sin gastar Groq. Si hay cero o varias, le pasa a Groq (mismo `GROQ_API_KEY` que `/chat`) la lista corta de candidatos de esa ventana (nunca toda la base de datos) para que tolere erratas de escritura. Si Groq tampoco resuelve con confianza, `/check-in` responde `409 {"error": "nombre_ambiguo"}` pidiendo al huésped que contacte con recepción — nunca se adivina.
- **Pipeline externo por email** (fuera de este repo, vive en Google Apps Script + Make.com): cuenta `alchomes2025guest@gmail.com` recibe correos de huéspedes que no pueden usar el enlace directo (algunas plataformas bloquean el link en el mensaje de bienvenida). Apps Script empuja cada correo nuevo a un Webhook de Make cada minuto; Make llama a Groq para extraer nombre/número/plataforma del asunto, consulta `GET /check-in?ref=...` (público, sin token) y responde al huésped con el enlace + el `book_id` de la respuesta. **`book_id` y el endpoint público `/check-in` son un contrato con ese pipeline — si se renombran o se protege el endpoint con token, avisar antes, se rompe silenciosamente sin que este repo lo note.**

## Fechas y zona horaria (octubre 2026)
Render corre en UTC. **Nunca usar `date.today()` ni `datetime.now()` sin zona para el "hoy" de negocio**: usar `hoy_madrid()` / `ahora_madrid()` de `services/fechas.py`. Con `date.today()` el día cambiaba a las 02:00 de Madrid (01:00 en invierno), y eso provocaba un falso aviso de "check-in de última hora" cada madrugada a las 02:13 (primer `/watchdog` tras el cambio de día UTC). Los `datetime.utcnow()` de `mobile_routes.py` son marcas de tiempo en UTC a propósito (logs, token health, `modifiedFrom` de Beds24): no cambiarlos.

## Vigilancia del iCal de Beds24 que importa RPV (octubre 2026)
- RPV crea las reservas de su listado importando una vez al día el iCal de exportación de cada habitación (`api.beds24.com/ical/bookings.ics?roomid=…&token=…`, pegado en el campo "Calendario de Booking" de cada ficha de RPV). Si Beds24 lo deja de servir, RPV no importa nada nuevo, sigue poniendo "Sincronizado" y **no avisa**. Pasó el 2026-10-04: en Beds24 (SETTINGS → CHANNEL MANAGER → ICAL EXPORT) el ajuste "Export" estaba en "Disable" en todas las habitaciones y el enlace daba `Error: room synchroniser not enabled`.
- `services/ical_beds24.py` abre los 6 enlaces desde `/watchdog` (paso 3b) y mete el fallo en `problemas` (WhatsApp con el dedupe habitual). Un fallo solo se confirma tras 2 pasadas seguidas (+1 reintento inmediato) y los OK se cachean 55 min. Los mensajes y logs **no incluyen nunca el enlace** (lleva el token).
- Los enlaces van en la variable de entorno **`BEDS24_ICAL_URLS`** de Render (6 URLs separadas por comas, puntos y coma, espacios o saltos de línea; son los mismos que están en las fichas de RPV, variante "Include Property and Room Description"). Si no está definida, el chequeo se omite sin avisar (`resultados.ical_beds24.configurado = false`). Si se regenera un token en Beds24, hay que cambiarlo en las 6 fichas de RPV **y** en esta variable.
- "Export" debe estar en **solo reservas**: con "Bookings + Unavailable Dates" los bloqueos salen como eventos y RPV los importaría como reservas pendientes falsas.

## Estado del parte en RPV: endpoint `/partes` (desde 2026-10-07)
- `services/rpv.py` consulta `GET /api/v1/partes` de RPV: estado del parte **por rango de fechas de entrada, incluidas las futuras**, sin datos personales. Una llamada por **cuenta** de RPV (hostal y Primavera tienen cada una su API key; sin parámetro `propiedad` → todas las propiedades de la cuenta) con ventana hoy..hoy+29 (la API admite 31 días). Por reserva devuelve `estado` (pendiente | parcial | programado | comunicado), `parte_completado`, `comunicado_autoridades`, `huespedes_registrados` (null si no está completado), `huespedes_previstos` y `completado_en`. **"Listo" = `parte_completado`**, no el estado: puede volver a `parcial` si el propietario aumenta los huéspedes previstos. Se cruza con Beds24 por (habitación, fecha de entrada).
- Por qué se cambió: el endpoint antiguo `GET /api/v1/usuarios` solo devuelve los huéspedes con entrada de HOY (hora de Canarias, 1 h menos que Madrid) y no veía los partes enviados con antelación (verificado el 2026-10-04: Deluxe 5 completado en el panel y 0 registros en la API). Sigue existiendo (con datos personales, solo entradas de hoy) pero el backend ya no lo usa.
- Copia en memoria por cuenta: TTL 10 min en segundo plano; la web de check-in da por bueno un "completado" en copia y, si no consta, vuelve a preguntar como mucho 1 vez/min; el resumen acepta ≤1 min. Si RPV falla se usa la última copia buena y las habitaciones de esa cuenta quedan "sin verificar" (nunca "pendiente"). Límite de RPV: **20 peticiones/min por API key**; `services/rpv.py` frena a 16/min por cuenta y respeta `Retry-After` en un 429. La API key nunca va a logs ni a mensajes de error.
- Consumidores: `/check-in` (`parte_recibido_para`, ahora con `pending_early` real para quien ya envió el parte), resumen 08:00/23:00 (recibido / recibido y comunicado / incompleto / pendiente / "no consta en RPV" / sin verificar), email de "registro completado" (en cuanto `parte_completado`, también con entrada futura; la primera pasada tras la migración envía de golpe a las reservas de los próximos 14 días que ya lo tuvieran completado) y el chequeo de salud del watchdog (2 llamadas).
- Diagnóstico sin datos personales: `/diag-rpv?token=<TOKEN>` (producción) y `&entorno=pre` (sandbox con datos ficticios, 60/min). Documentación oficial: panel de RPV → Integraciones → API de Huéspedes (solo con sesión).
- RPV también ha habilitado (pendiente de revisar) la importación nativa de reservas desde Beds24 y el indicador de calendario "no sincronizado". Si se migra, el aviso del iCal (`BEDS24_ICAL_URLS`) deja de ser necesario para RPV, pero el iCal de Primavera sigue haciendo falta para Holidu.

## Resúmenes diarios por WhatsApp (octubre 2026)
- Dos resúmenes, ambos con entradas y salidas de **las dos propiedades** y el canal de cada reserva: **08:00 → día de hoy**, **23:00 → día de mañana**.
- Los envía **el propio backend desde `/watchdog`** (`services/resumen_programado.py`), en la primera pasada a partir de cada hora (≤15 min de retraso) y con reintento en la siguiente si falla. Dedupe en Firestore `system_state/resumenes_programados` (`{"manana": fecha, "noche": fecha}`). Sin Firestore no se envía (sin dedupe saldría cada 15 min).
- Los triggers `ejecutarResumenDiario` del Apps Script "Orquestador" **ya no se usan** (se quitaron al pasar a este sistema; si siguieran, habría resúmenes duplicados). `GET /resumen` queda como herramienta manual: `?enviar=0` para verlo sin enviar, `&dia=manana` para el de las 23:00.

## Avisos de check-in de última hora
- `services/checkins_ultima_hora.py` (antes `primavera_avisos.py`) cubre **las dos propiedades**. Lo llama `/watchdog` cada 15 min (después del paso de resúmenes), **a cualquier hora del día**.
- "Última hora" = llegada de hoy que no estaba en ningún resumen que cubriera hoy (el de las 23:00 de ayer o el de las 08:00 de hoy). Cada resumen, tras enviarse, marca sus entradas para el día que cubre; el de las 23:00 deja preparado el día siguiente, así que desde las 00:00 ya se avisa.
- Mientras ningún resumen haya cubierto el día de hoy no se avisa de nada: sin esa referencia todas las llegadas parecerían de última hora. Se resuelve solo cuando sale el de las 08:00, que se reintenta en cada watchdog.
- El dedupe vive en Firestore en `system_state/primavera_avisos`, por fecha: `{"dias": {"YYYY-MM-DD": [book_id, ...]}}` (los días pasados se podan; se lee también el formato antiguo `{fecha, book_ids}`). El nombre del documento se mantiene a propósito aunque el módulo se renombrara (cambiarlo perdería el estado al desplegar).

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

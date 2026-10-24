# Perfiles por PIN y permisos — app de gestión

Estado: **fase 2** (el modelo en `services/accesos.py` y su aplicación a TODAS las rutas con `rutas_permisos.py` + el hook `mobile_routes.exigir_permiso`).
Encargo de Adrián del 2026-10-24; la tabla de abajo la confirmó él. Mientras `ACCESO_PINS_ANTIGUOS` no sea `0`, los dos PIN de siempre (`MOBILE_APP_PIN` →
«gestor de la Casa de la Primavera», `MOBILE_APP_PIN_ADMIN` → «propietario») siguen valiendo: es el día de corte (fase 8) el que los retira.

## Perfiles y PIN
| PIN | Perfil | Quién |
|---|---|---|
| 1379 | `propietario` | Ione y Adrián |
| 9999 | `gestor_primavera` | el gestor que además lleva la Casa de la Primavera |
| 2486 | `gestor` | el resto de gestores |
| 2589 | `sustituto` | quien sustituya a Ione (PIN compartido: pide el nombre; actividad auditada) |
| 4268 | `limpieza` | personal de limpieza |
| 1432 | — (PIN de seguridad) | no abre la app: lo teclea el sustituto para enviar códigos de emergencia; el propietario no lo necesita |

Los PIN salen de variables de entorno (`PIN_PROPIETARIO`, `PIN_GESTOR`, `PIN_GESTOR_PRIMAVERA`, `PIN_SUSTITUTO`, `PIN_LIMPIEZA`, `PIN_ENVIO_CODIGOS`) con
estos valores por defecto. En memoria solo se conserva su hash (PBKDF2-SHA256, sal por perfil + `ACCESO_PEPPER`).

## Permisos por perfil
| Permiso | propietario | gestor_primavera | gestor | sustituto | limpieza |
|---|---|---|---|---|---|
| `calendario.ver` | ✔ | ✔ | ✔ | ✔ | |
| `reservas.escribir` (crear, editar, cancelar, bloquear) | ✔ | ✔ | ✔ | ✔ | |
| `precios.escribir` | ✔ | ✔ | | | |
| `asistente.usar` | ✔ | ✔ | ✔ | ✔ | |
| `asistente.alcance_completo` | ✔ | | | | |
| `finanzas.primavera` | ✔ | ✔ | | | |
| `finanzas.hostal` | ✔ | | | | |
| `finanzas.facturas` (facturas, Asesor Contable, Desglose) | ✔ | | | | |
| `emergencia.enviar` (🚨, correo de códigos, enlace del parte) | ✔ | | | ✔ | |
| `emergencia.sin_pin` | ✔ | | | | |
| `emergencia.ver_pin` (el asistente le dice el 1432) | ✔ | | | ✔ | |
| `operativa.ver` | ✔ | | | ✔ | |
| `operativa.entrenamiento` / `operativa.historial` | ✔ | | | | |
| `propiedad.primavera` (**todo** lo de La Casa de la Primavera: calendario, reservas, precios, asistente, incidencias, emergencias) | ✔ | ✔ | | | |
| `limpieza.ver_incidencias` | ✔ | ✔ | | | |
| `limpieza.calendario` / `limpieza.marcar` | | | | | ✔ |
| `sistema.diagnostico` | ✔ | | | | |

## Quién es cada persona
- **1379 = Ione o Adrián, según el dispositivo.** La primera vez que un dispositivo entra con ese PIN, la app pregunta «¿Eres Ione o Adrián?» (solo esas dos
  opciones) y llama a `POST /mobile/dispositivo {deviceId, nombre}`; el servidor lo recuerda en Firestore (`dispositivos_acceso`, `services/dispositivos.py`) y a partir
  de entonces ese dispositivo entra ya identificado: `/login` (con `deviceId` en el cuerpo) devuelve `nombre` y `identidad: {necesita: false}`. Si el dispositivo no está
  registrado devuelve `identidad: {necesita: true, opciones: ["Ione", "Adrián"]}` y entra igualmente (la app antigua no se rompe).
- **9999 = Hermann.** La sesión lleva siempre ese nombre.
- **2589 = sustituto:** pide el nombre al entrar, y es auditado.
- **2486 y 4268:** como hasta ahora (la app pregunta el nombre por su cuenta).

## La Casa de la Primavera
Solo la ven el 1379 y el 9999 (permiso `propiedad.primavera`). Para el resto de perfiles el servidor: quita su habitación, reservas, precios y
disponibilidad de `/all-data`, `/rooms` y `/check-changes`; rechaza (403) crear, editar, cancelar o bloquear reservas suyas (por habitación o por el id
de la reserva); hace que el asistente no consulte ni nombre la propiedad y se niegue a preparar códigos o mensajes de sus reservas; y oculta sus incidencias de limpieza.
La limpiadora (4268) es la excepción: ve su calendario ocupado/libre para marcar incidencias.

## Cómo se identifica cada petición
`Authorization: Bearer <token>` (sesión firmada que emite `/mobile/login`, con el perfil y el nombre; 12 h) o, por compatibilidad con la app instalada, el PIN en
`x-app-pin`. El PIN compartido del sustituto (2589) **solo vale con sesión**: el login le pide el nombre y la sesión lo fija, así el historial no se puede falsear.
Tras 8 PIN equivocados desde una IP en 15 min se bloquea todo desde ella.

## Qué permiso pide cada ruta
Antes casi todas aceptaban cualquier PIN válido (`check_pin`), incluidas las de diagnóstico y el cambio de precios. Ahora cada una exige UN permiso, y **una ruta sin declarar se rechaza** (un test falla si falta alguna):

| Ruta | Hoy | Fase 2 |
|---|---|---|
| `/health`, `/login` | sin PIN | sin PIN (el login limita intentos) |
| `/rooms`, `/all-data`, `/reload`, `/check-changes` | cualquier PIN | `calendario.ver` |
| `/update` (precios y disponibilidad) | **cualquier PIN** | `precios.escribir` |
| `/create-booking`, `/block-dates`, `/cancel-booking`, `/edit-booking` | cualquier PIN | `reservas.escribir` |
| `/finance` | cualquier PIN; hostal solo admin | `finanzas.primavera` o `finanzas.hostal` según la propiedad |
| `/finance/desglose` | PIN normal + PIN 1379 | `finanzas.facturas` (+ PIN 1379) |
| `/assistant`, `/finance-chat` | cualquier PIN; el cliente manda `finanzas:true` | `asistente.usar`; herramientas y ámbito según permisos; el servidor ignora `finanzas` del cliente |
| `/assistant/preparar-codigos`, `/assistant/enviar-codigos` | admin o PIN normal + 1432 | `emergencia.enviar` (+ 1432 salvo `emergencia.sin_pin`) |
| `/facturas/*` (todas las de usuario) | cualquier PIN; hostal solo admin | `finanzas.facturas` |
| `/facturas/diag-ia`, `/token-status`, `/test-whatsapp`, `/test-email`, `/diag-registro-completado`, `/logs`, `/debug-overrides`, `/debug-bookings`, `/test-sync`, `/fix-zero-prices`, `/set-booking-price` | **cualquier PIN** (algunas envían WhatsApp o correo) | `sistema.diagnostico` |
| `/facturas/conectado` | secreto firmado del conector | igual (no es de usuario) |
| `/limpieza/calendario`, `/limpieza/incidencia` | PIN de limpieza (o admin) | `limpieza.calendario` / `limpieza.marcar` |
| `/limpieza/incidencias` | cualquier PIN | `limpieza.ver_incidencias` |

Las rutas nuevas de las fases 5-7 (Operativa, Entrenamiento, Historial) usarán `operativa.ver`, `operativa.entrenamiento` y `operativa.historial`; hay que
declararlas en `rutas_permisos.py`. `tests/test_acceso_fase2.py` recorre todas las rutas con los cinco perfiles.

## La app (fase 3)
`mobile-app/index.html` entra con el PIN, guarda la sesión firmada solo en memoria y la manda en `Authorization` en cada petición a la API. Según los permisos del
perfil oculta lo que no le toca: pestaña Finanzas (y la barra Hostal solo para el propietario), asistente flotante, 🚨 emergencia, botones de Facturas y Asesor,
«Editar precio» y el historial de cambios 📋 (rutas de diagnóstico: solo propietario). **Esto es comodidad: el servidor rechaza igualmente lo que no corresponda.**
El candado de Finanzas del navegador (con el PIN 9999 escrito en el código) ya no existe. Al entrar: el 1379 pregunta «¿Ione o Adrián?» si el dispositivo es nuevo; el 2589
pide el nombre y avisa de que su actividad queda registrada; el 9999 entra como Hermann; el 2486 y el 4268 siguen como antes. Si la sesión caduca (12 h) o el
servidor reinicia, la app avisa y pide volver a entrar.

## El asistente por perfil (fase 4)
- **Propietario (1379):** alcance completo.
- **2589 (sustituto):** calendario y reservas, cómo funciona la app y el puesto de gestora del hostal (tareas, llamadas, plataformas, limpieza, incidencias, escalado, con la base de conocimiento).
- **2486 y 9999:** solo calendario y reservas y cómo funciona la app. **Las tareas de gestión no son para ellos** (sin `buscar_operativa`): «Eso no está disponible para tu perfil: consúltalo con Ione».
  Fuera de eso declina con amabilidad (barrera blanda, por el prompt). **Finanzas es barrera dura:** sin acceso a datos económicos, una pregunta de dinero (ingresos, beneficio,
  comisiones, costes, rentabilidad, impuestos…) ni llega al modelo: el servidor responde «Dirígete a Ione.» (pendiente de confirmar a quién remitir las finanzas); y la herramienta `finanzas_mes` ni existe para ellos.
  El 9999 ve la Primavera pero no el hostal: el modelo y las herramientas lo limitan, y se le remite a Ione.
- **Base de conocimiento** (`services/operativa_kb.py`, colección `kb_operativa`): herramienta `buscar_operativa`, solo con entradas **aprobadas**. Si no hay nada, el asistente
  dice «Eso todavía no está recogido: consúltalo con Ione» y no inventa procedimientos. La edición, el alta y el Entrenamiento llegan en las fases 5 y 6.

**A quién remite el asistente** (constantes `CONTACTO_*` en `mobile_asistente.py`): reservas y operativa → **Ione**; fallos técnicos de la app y permisos o accesos (PIN, qué ve cada perfil,
«no tienes acceso a la Casa de la Primavera») → **Adrián**.

## Operativa de Gestión (fase 5)
Pestaña «📋 Operativa» (permiso `operativa.ver`: el sustituto 2589 y el propietario 1379): botón del asistente de operativa (con sugerencias propias), tareas diarias en botones que abren su detalle, y
las secciones de escalado de problemas, teléfonos, preguntas frecuentes y otros procedimientos. Todo sale de la base de conocimiento (`services/operativa_kb.py`, Firestore `kb_operativa`), **solo lo
aprobado**. El propietario (`operativa.entrenamiento`) tiene además «📚 Base de conocimiento»: lista con borradores y aprobadas, alta, edición, aprobación y borrado
(`mobile_operativa.py`). Rutas: `/mobile/operativa/indice`, `/entrada`, `/kb` (GET y POST) y `/kb/borrar`. Las fases 6 y 7 añaden «Entrenamiento» e «Historial de sustitución» (solo 1379).

## Entrenamiento (fase 6)
Solo el propietario (`operativa.entrenamiento`). Botón «🎙 Entrenamiento» en la pestaña Operativa: entrevista guiada a Ione (con Adrián al lado), **una pregunta cada vez**, sobre los 10 temas del encargo
(tareas del día a día, limpieza, reservas, llamadas, vending, plataformas, preguntas frecuentes, teléfonos, escalado administrativo y escalado técnico). Cada turno
(`services/entrevista.py`, `mobile_entrenamiento.py`) manda a Groq (cuenta de gestión) el tema, lo que hay que cubrir, lo ya recogido y los últimos mensajes; el modelo devuelve la siguiente pregunta y,
si procede, entradas nuevas. **Las entradas se guardan siempre como BORRADOR** (un tema ya aprobado no se pisa: se propone una «revisión» aparte) y se revisan en «📚 Base de conocimiento».
La conversación queda en Firestore (`entrenamiento_sesiones`) para seguirla otro día. Las entradas de escalado llevan los apartados: ejemplos, lo que resuelve y no resuelve el sustituto, a quién avisar,
medio, horario, datos a dar, qué hacer si no responde y cuándo es urgente.
Cada entrada tiene un campo `propiedad` (hostal, primavera o vacío): lo de la Casa de la Primavera solo lo ven (y lo usa el asistente) quienes tienen acceso a esa propiedad.

## Historial de sustitución (fase 7)
Todo lo que hace el **2589** lo registra el **servidor** (no el navegador) en Firestore `historial_sustitucion`, con fecha y hora y con el nombre de su sesión firmada (el que escribió al entrar; no se puede
cambiar ni desactivar desde ese perfil): entradas, calendario abierto, **cada pregunta al asistente con su respuesta**, tareas de la operativa consultadas, reservas **creadas, modificadas (antes y después) y canceladas**,
bloqueos, envíos de emergencia (correo del huésped enmascarado) y los **escalados** (cuando el asistente le remite a Ione o a Adrián). Sin teléfono ni correo de los huéspedes. La app avisa además, de mejor esfuerzo, de los
botones que pulsa y las reservas que mira en pantalla (no generan petición). Se conserva 12 meses. Lo lee solo el propietario: «🕵 Historial de sustitución» en la pestaña Operativa, con filtros por sustituto, día y tipo, y un
asistente («¿qué hizo ayer?»: traduce la pregunta a filtros y responde solo con los registros). Rutas: `/mobile/historial-sustitucion` y `/preguntar`; el registro del cliente, `/mobile/registro/evento`.

## Despliegue (fase 8)
- **Verificación en producción:** `python scripts/verificar_perfiles.py` entra con cada PIN y comprueba perfil × ruta (solo lecturas baratas) y que La Casa de la Primavera solo la ven el 1379 y el 9999. Se ejecutó el
  2026-10-24 tras desplegar la fase 7: todo ✔.
- **Variables de Render recomendadas:** `ACCESO_SESION_SECRETO` (cadena aleatoria larga; sin ella las sesiones se pierden en cada reinicio o despliegue y todos tienen que volver a poner el PIN; generar con
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`). Opcional: `ACCESO_PEPPER`.
- **Día de corte:** mientras no se ponga `ACCESO_PINS_ANTIGUOS=0` en Render, los dos PIN de siempre (`MOBILE_APP_PIN` → gestor de la Primavera, `MOBILE_APP_PIN_ADMIN` → propietario) siguen valiendo.

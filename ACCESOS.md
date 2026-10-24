# Perfiles por PIN y permisos — app de gestión

Estado: **fase 1** (el modelo, en `services/accesos.py`). Todavía NO se aplica a las rutas: hasta la fase 2 la app sigue con `MOBILE_APP_PIN` y
`MOBILE_APP_PIN_ADMIN`. Encargo de Adrián del 2026-10-24; la tabla de abajo la confirmó él.

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
| `limpieza.ver_incidencias` | ✔ | ✔ | ✔ | ✔ | |
| `limpieza.calendario` / `limpieza.marcar` | | | | | ✔ |
| `sistema.diagnostico` | ✔ | | | | |

## Qué permiso pedirá cada ruta en la fase 2
Hoy casi todas aceptan cualquier PIN válido (`check_pin`), incluidas las de diagnóstico y el cambio de precios. En la fase 2 cada una exige UN permiso:

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

Las rutas nuevas de las fases 5-7 (Operativa, Entrenamiento, Historial) usarán `operativa.ver`, `operativa.entrenamiento` y `operativa.historial`.
Un test de la fase 2 recorrerá todas las rutas de `/mobile` y fallará si alguna no declara permiso.

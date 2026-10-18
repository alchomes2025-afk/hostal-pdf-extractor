# Facturas en el Drive del cliente — fase 1 (rama `mejora/facturas`)

Trabajo en curso, **sin publicar**. No fusionar a `main` hasta que esté terminado y probado con una factura real.

## Qué pidió Adrián (2026-10-17)
En la pestaña Finanzas de la app de gestión, el cliente sube sus facturas, se organizan solas en SU Drive y, después, la app
calcula con ellas las cifras reales (en lugar de los costes estimados). Decisiones: pensado multicliente (HostalFácil);
gastos, ingresos e IVA; el Drive manda (lo que se meta a mano en la carpeta también cuenta); el cliente revisa los datos
leídos antes de guardar.

## Arquitectura
- Cada cliente comparte UNA carpeta con la cuenta de la plataforma (hostalfacil@gmail.com): para ALC Homes, **«HostalFácil-Gestión»** en su Drive. El
  sistema crea él solo, la primera vez que se guarda una factura, todo lo que cuelga de ahí (`Facturas/<Propiedad>/<Año>/<nT>/<Gastos|Ingresos>/` y
  la hoja «Registro de facturas <Año>»): el cliente no crea ninguna otra carpeta.
- Un Apps Script de esa cuenta (`apps-script/puente_hostalfacil.js`, aplicación web) hace de puente: `ocr` (lee texto de una
  foto o PDF escaneado con la conversión de Google Drive, gratis), `guardar` (archivo + fila en la hoja de registro),
  `ping` y `correo` (esta última ya está en producción en `main` para los avisos).
- `services/puente_plataforma.py` (en `main`) llama al puente; `services/facturas.py` lee (PDF con texto → pdfplumber; si no,
  OCR), pregunta a Groq, normaliza y comprueba (base + IVA − retención = total), guarda en
  `<raíz>/Facturas/<Propiedad>/<Año>/<nT>/<Gastos|Ingresos>/AAAA-MM-DD_Proveedor_Total€.ext` y en la hoja «Registro de facturas <año>»,
  e indexa en Firestore (`facturas`, id = cliente + huella: detecta duplicados).
- `mobile_facturas.py`: endpoints `/mobile/facturas/{config,leer,guardar}` y listado del mes, con las mismas reglas de PIN que
  Finanzas (PIN normal: solo La Casa de la Primavera).
- Variables de Render: `PUENTE_URL`, `PUENTE_SECRETO` (ya definidas en `config.py`) y `FACTURAS_DRIVE_RAIZ_ALC`.

## Hecho
- Servicio, endpoints y puente escritos. `python -X utf8 -I pruebas/sim_facturas.py` → 36 comprobaciones en verde (puente, IA y Firestore simulados).
- **Pantalla en la app (2026-10-18)**, `mobile-app/index.html`: botón «🧾 Facturas de este mes» en Finanzas → panel con navegación de mes, totales
  (gastos, ingresos, IVA soportado y repercutido), gastos por categoría y lista del mes con enlace al Drive; «＋ Subir factura» (foto o PDF; las fotos
  se reducen a ≤2000 px antes de subir) → formulario de revisión con los datos leídos (recálculo de cuota y total, comprobación «cuadra», aviso de
  duplicado, tipo gasto/ingreso con sus categorías) → «Guardar en el Drive». Si la lectura falla se puede rellenar a mano. Probada en Chrome contra un
  servidor simulado. La lectura usa la cuenta de Groq de gestión del cliente (`GROQ_API_KEY_GESTION`, vía `services/groq_claves.py`).
- Sin Drive configurado la pantalla se ve pero la subida sale desactivada (`habilitado` = false): fusionar a `main` no cambia nada hasta que se configure.

## Primer uso: el sistema crea y conecta el Drive del cliente (2026-10-18)
Asistente de 3 pasos en la pantalla de facturas (`fxConexion` en la app): «Conectar mi Drive» → pestaña de un Apps Script («conector»,
`apps-script/conector_hostalfacil.js`, se ejecuta con la identidad del cliente y solo pide `drive.file`) que crea «HostalFácil-Gestión» en SU Drive, la
comparte con hostalfacil@gmail.com y avisa al backend (`POST /mobile/facturas/conectado`, token de un solo uso de 20 min + secreto compartido). El backend
(`services/drive_cliente.py`) comprueba, crea `Facturas/<Propiedad>/<Año>/<1T-4T>/<Gastos|Ingresos>` con el puente (acción `estructura`) y guarda el id de la
carpeta por cliente en Firestore (`facturas_config`); la app lo detecta sola. Solo el PIN de administrador puede conectar. `tests/test_facturas_conexion.py`
(11 pruebas) y prueba en Chrome con servidor simulado. Instalación: `apps-script/LEEME_conector.md`.

## Varias facturas, ZIP y extracto bancario (2026-10-18)
- **Subida múltiple:** el selector admite varios archivos (PDF, fotos) y archivos **.zip** (se abren en el navegador con JSZip, cargado de cdnjs solo cuando
  hace falta; se ignoran los archivos que no son PDF ni imagen y las carpetas `__MACOSX`). **.rar/.7z/.tar no** se pueden abrir desde el navegador: la app avisa y
  pide convertirlos a .zip. Máx. 30 documentos por tanda.
- Se leen **uno tras otro** (reintento a los 20 s si la IA está ocupada: la cuenta gratuita de Groq da unas 3 facturas por minuto). Resumen: «guardar las N que
  cuadran» de golpe (cuadra + sin avisos + sin duplicado), «revisar una a una» las demás (formulario con «Guardar» / «Omitir»), toque en cualquier fila para revisarla.
- **Extracto bancario:** al terminar una tanda, y siempre desde el botón «📑 Subir extracto bancario», la app pregunta si se quiere cotejar con el extracto
  (CSV, Excel o PDF, ≤10 MB) y lo guarda en `Facturas/<Propiedad>/<Año>/Extractos/` (`POST /mobile/facturas/extracto`). **El cotejo automático NO está hecho todavía:**
  falta decidir el formato de los extractos, repasar la app de cotejo de Adrián (fase de pruebas; no funcionó con Groq) y, si hace falta, IA de pago del cliente.
  Pruebas: `tests/test_facturas_extracto.py`; Chrome contra servidor simulado (lote con ZIP, resumen, guardado en bloque, revisión una a una, extracto).

## Cotejo con el extracto bancario (2026-10-18)
- **Lectura sin IA** (`services/extractos.py`): PDF «Extracto Mensual» de BBVA con texto (los movimientos se separan por el hueco vertical entre bloques; con el
  extracto real de agosto: 44 movimientos y la cadena de saldos cuadra en los 43 enlaces) y CSV genérico (separador ;,	, UTF-8/Latin-1, cabecera detectada,
  importe único o cargo/abono). Excel .xls/.xlsx: «Guardar como» CSV. Otros bancos en PDF: no (pedir CSV).
- **Reconocimiento automático:** el selector admite CSV/Excel además de PDF/fotos/ZIP; un CSV/Excel es un extracto; un PDF con ≥3 líneas «fecha fecha … importe EUR saldo EUR»
  lo marca el servidor (`leer()` devuelve `extracto: true` sin gastar IA). Funciona mezclado con facturas y dentro de un ZIP. La app pregunta «¿Quieres cotejarlo con tus facturas?»
  (resumen de la tanda, o pantalla propia si solo había extracto; «Solo guardarlo en mi Drive» y «Ahora no» también).
- **Cotejo** (`POST /mobile/facturas/cotejar`, guarda también el extracto en `Extractos/`): cada factura guardada casa con UN movimiento por importe exacto (±0,01), signo
  correcto (gasto = cargo; rectificativa = abono; ingreso = abono) y fecha de −7 a +60 días; desempata por palabras del proveedor en el concepto (confianza alta / media).
  Resultado: conciliadas, facturas sin movimiento, pagos sin factura (agrupados: efectivo, nómina, Seguridad Social, comisiones…) y cobros sin factura. Solo cuentan las
  facturas GUARDADAS de las propiedades que ve el PIN (el PIN normal, solo Primavera). Tests: `tests/test_extractos.py`.

## Editar, eliminar y elegir propiedad (2026-10-18)
- Tocar una factura de la lista abre su formulario en modo edición: «Guardar cambios» (`POST /mobile/facturas/editar`: corrige datos y, si cambian la fecha, el tipo o la
  propiedad, el archivo se RENOMBRA y MUEVE en el Drive y su fila se quita de la hoja de registro vieja y se añade a la nueva, con el mismo ID) y «Eliminar factura»
  (`POST /mobile/facturas/eliminar`, con segundo toque de confirmación: el archivo va a la papelera del Drive 30 días, se quita la fila de la hoja y se borra el índice, así la
  huella queda libre para volver a subirla). El puente nuevo tiene las acciones `eliminar` y `actualizar`; comprueban que el archivo cuelga de la carpeta raíz del cliente.
- Si el Drive falla, no se toca el índice de Firestore (editar/eliminar son «Drive primero»). Permisos: el PIN normal solo toca facturas de Primavera y no puede moverlas al hostal.
- **Selector de propiedad:** en la lista (el administrador ve las dos), en el formulario de cada factura y para toda una tanda en el resumen («Guardar esta tanda en:»).
  Tests: `tests/test_facturas_editar.py`.

## Finanzas con costes reales (fase 2, 2026-10-18)
`_informe_financiero(property_id, mes, costes="auto")` (`mobile_routes.py`) calcula el beneficio con los gastos REALES de las facturas guardadas del mes y la propiedad
(`services/facturas.costes_reales`) cuando los hay, y con las estimaciones de siempre cuando no (`/finance?costes=auto|reales|estimados`; en la app, botón «Ver con costes
estimados / reales»). Agrupación: **limpieza** (Limpieza y lavandería, Consumibles y amenities), **fijos** (Suministros, Seguros, Impuestos y tasas, Alquiler, Software, Gestoría) y
**otros** (Mantenimiento, Mobiliario, Publicidad, Otros gastos). Importes con IVA, un abono resta. Las **comisiones de plataformas NO suman** (ya van descontadas de los ingresos netos;
se muestran aparte). Un mes con solo facturas de comisiones sigue usando estimaciones. La respuesta trae `rentabilidad.origen_costes`, `facturas_mes` y `estimado` (para comparar).
El asistente (`finanzas_mes`) lo recibe igual y avisa de que, si faltan facturas, el beneficio sale más alto. Tests: `tests/test_costes_reales.py`.
Pendiente de afinar: costes anuales (IBI, seguros) caen en un solo mes por fecha de factura —conviene prorratearlos—; reparto de un gasto entre las dos propiedades; IVA soportado
y reglas de inversión del sujeto pasivo (Booking) cuando haya un resumen para el gestor.

## Falta
1. **Pasos de Adrián:** actualizar el puente (nueva versión), crear el conector y poner en Render `PUENTE_URL`, `PUENTE_SECRETO`, `CONECTOR_URL`
   (ver `apps-script/LEEME_conector.md`); fusionar la rama; abrir Facturas con el PIN de administrador y seguir el asistente.
2. Probar con una factura real (PDF con texto y foto de un tique) y comprobar carpeta, nombre del archivo y hoja «Registro de facturas».
3. **Fase 2:** sustituir en `_informe_financiero` los costes estimados por los de las facturas; revisión periódica de la carpeta del Drive (el Drive manda)
   y reparto por propiedad.
4. Portarlo al monorepo `hostalfacil/plataforma` (la conversación de migración lo tendrá que incluir).

## Fuente de ideas
`C:\Users\monis.HPIKU\Downloads\Contabilidad_Web_v15_8.html`: herramienta personal de Adrián que concilia extractos bancarios
con facturas (prompts y reglas de cotejo ya probadas). Contiene una clave real de Anthropic: NO subirlo ni copiarla a ningún repo.

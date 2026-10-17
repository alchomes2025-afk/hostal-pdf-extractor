# Facturas en el Drive del cliente — fase 1 (rama `mejora/facturas`)

Trabajo en curso, **sin publicar**. No fusionar a `main` hasta que esté terminado y probado con una factura real.

## Qué pidió Adrián (2026-10-17)
En la pestaña Finanzas de la app de gestión, el cliente sube sus facturas, se organizan solas en SU Drive y, después, la app
calcula con ellas las cifras reales (en lugar de los costes estimados). Decisiones: pensado multicliente (HostalFácil);
gastos, ingresos e IVA; el Drive manda (lo que se meta a mano en la carpeta también cuenta); el cliente revisa los datos
leídos antes de guardar.

## Arquitectura
- Cada cliente comparte su carpeta de facturas con la cuenta de la plataforma (hostalfacil@gmail.com).
- Un Apps Script de esa cuenta (`apps-script/puente_hostalfacil.js`, aplicación web) hace de puente: `ocr` (lee texto de una
  foto o PDF escaneado con la conversión de Google Drive, gratis), `guardar` (archivo + fila en la hoja de registro),
  `ping` y `correo` (esta última ya está en producción en `main` para los avisos).
- `services/puente_plataforma.py` (en `main`) llama al puente; `services/facturas.py` lee (PDF con texto → pdfplumber; si no,
  OCR), pregunta a Groq, normaliza y comprueba (base + IVA − retención = total), guarda en
  `<raíz>/<Propiedad>/<Año>/<nT>/<Gastos|Ingresos>/AAAA-MM-DD_Proveedor_Total€.ext` y en la hoja «Registro de facturas <año>»,
  e indexa en Firestore (`facturas`, id = cliente + huella: detecta duplicados).
- `mobile_facturas.py`: endpoints `/mobile/facturas/{config,leer,guardar}` y listado del mes, con las mismas reglas de PIN que
  Finanzas (PIN normal: solo La Casa de la Primavera).
- Variables de Render: `PUENTE_URL`, `PUENTE_SECRETO` (ya definidas en `config.py`) y `FACTURAS_DRIVE_RAIZ_ALC`.

## Hecho
Servicio, endpoints y puente escritos. `python -X utf8 -I pruebas/sim_facturas.py` → 36 comprobaciones en verde (puente, IA y
Firestore simulados).

## Falta
1. **Pantalla en la app** (`mobile-app/index.html`, pestaña Finanzas): subir (foto o PDF), formulario de revisión de los datos
   leídos, lista del mes con totales de gastos, ingresos e IVA.
2. **Pasos de Adrián:** montar el script del puente en la cuenta hostalfacil, compartir la carpeta de facturas con esa cuenta y
   poner `FACTURAS_DRIVE_RAIZ_ALC` en Render.
3. Probar con una factura real; commit/fusión; documentar en `claude.md` y en la memoria.
4. **Fase 2:** sustituir en `_informe_financiero` los costes estimados por los de las facturas; revisión periódica de la carpeta
   del Drive (el Drive manda) y reparto por propiedad.
5. Portarlo al monorepo `hostalfacil/plataforma` (la conversación de migración lo tendrá que incluir).

## Fuente de ideas
`C:\Users\monis.HPIKU\Downloads\Contabilidad_Web_v15_8.html`: herramienta personal de Adrián que concilia extractos bancarios
con facturas (prompts y reglas de cotejo ya probadas). Contiene una clave real de Anthropic: NO subirlo ni copiarla a ningún repo.

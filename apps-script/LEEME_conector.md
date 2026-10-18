# Conector del Drive del cliente — instalación (una vez, para toda la plataforma)

Dos proyectos de Apps Script en la cuenta de la plataforma (hostalfacil@gmail.com):

| Proyecto | Archivo | Se ejecuta como | Para qué |
|---|---|---|---|
| **Puente HostalFácil** (ya creado) | `puente_hostalfacil.js` | la plataforma | guardar facturas, OCR, crear subcarpetas, correo |
| **Conector HostalFácil** (nuevo) | `conector_hostalfacil.js` + `conector_appsscript.json` | el CLIENTE que abre el enlace | crear «HostalFácil-Gestión» en SU Drive y compartirla con la plataforma |

## 1. Actualizar el puente (nueva acción `estructura`)
Pegar el nuevo `puente_hostalfacil.js` sobre el del proyecto, guardar y **Implementar → Administrar implementaciones → lápiz → Nueva versión → Implementar**
(la dirección `/exec` no cambia).

## 2. Crear el conector
1. script.google.com con hostalfacil@gmail.com → Nuevo proyecto «Conector HostalFácil».
2. Pegar `conector_hostalfacil.js` en `Código.gs`.
3. Configuración del proyecto → marcar «Mostrar el archivo de manifiesto appsscript.json en el editor»; en el editor abrir `appsscript.json`
   y reemplazar todo su contenido por `conector_appsscript.json` (activa el servicio Drive v3 y los permisos mínimos: `drive.file`).
4. Configuración del proyecto → Propiedades del script → `SECRETO` = el mismo valor que `PUENTE_SECRETO`.
5. Implementar → Nueva implementación → Aplicación web → **Ejecutar como: «Usuario que accede a la aplicación web»**;
   **Quién tiene acceso: «Cualquier usuario con cuenta de Google»** → Implementar (aceptar permisos). Copiar la dirección `/exec`.

## 3. Render
`PUENTE_URL` (dirección `/exec` del puente), `PUENTE_SECRETO`, `CONECTOR_URL` (dirección `/exec` del conector). Ya no hace falta `FACTURAS_DRIVE_RAIZ_ALC`
(queda como respaldo: lo conectado desde la app manda).

## Flujo del cliente (primer uso)
Finanzas → «🧾 Facturas» → asistente de 3 pasos → pestaña de Google (permiso una vez; aviso «app no verificada» → Configuración avanzada) → el conector
crea y comparte la carpeta → el backend guarda su id en Firestore (`facturas_config`) y crea `Facturas/<Propiedad>/<Año>/<nT>/<Gastos|Ingresos>`.

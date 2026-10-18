/**
 * HostalFácil — Conector del Drive del cliente
 * -----------------------------------------------------------------
 * Proyecto de Apps Script de la cuenta de la PLATAFORMA (hostalfacil@gmail.com) publicado como aplicación web que se ejecuta con la
 * identidad de QUIEN LA ABRE (el cliente). Lo abre el cliente desde la app, la primera vez que usa las facturas (botón «Conectar mi
 * Drive»), y hace tres cosas en SU Drive con su permiso:
 *   1. crea la carpeta «HostalFácil-Gestión» (si ya la creó antes este conector, reutiliza esa);
 *   2. la comparte con la cuenta de la plataforma como Editor, para que el puente pueda guardar ahí las facturas;
 *   3. avisa al backend (POST /mobile/facturas/conectado) con el id de la carpeta y un token de un solo uso.
 *
 * Solo pide el permiso de Drive «drive.file»: ve únicamente lo que crea este conector, nada más del Drive del cliente.
 *
 * Instalación (una vez, en la cuenta de la plataforma; ver apps-script/LEEME_conector.md):
 *   1. Proyecto nuevo «Conector HostalFácil»; pegar este código y el contenido de conector_appsscript.json en appsscript.json
 *      (Configuración del proyecto → «Mostrar el archivo de manifiesto appsscript.json en el editor»).
 *   2. Propiedades del script → SECRETO = el mismo valor que PUENTE_SECRETO en Render.
 *   3. Implementar → Nueva implementación → Aplicación web → Ejecutar como: «Usuario que accede a la aplicación web»;
 *      Quién tiene acceso: «Cualquier usuario con cuenta de Google». La dirección /exec va en CONECTOR_URL en Render.
 * El secreto NO va en el código.
 */

const BACKEND_URL = 'https://hostal-pdf-extractor.onrender.com/mobile/facturas/conectado';
const CUENTA_PLATAFORMA = 'hostalfacil@gmail.com';
const NOMBRE_CARPETA = 'HostalFácil-Gestión';

function doGet(e) {
  const token = String((e && e.parameter && e.parameter.t) || '').trim();
  if (!token) {
    return pagina_('Enlace no válido', 'Abre esta página desde la app, con el botón «Conectar mi Drive».', false);
  }
  try {
    const email = Session.getActiveUser().getEmail();
    const carpetaId = carpeta_();
    compartir_(carpetaId);
    const respuesta = UrlFetchApp.fetch(BACKEND_URL, {
      method: 'post',
      contentType: 'application/json',
      muteHttpExceptions: true,
      payload: JSON.stringify({
        token: token,
        folder_id: carpetaId,
        email: email,
        secret: PropertiesService.getScriptProperties().getProperty('SECRETO'),
      }),
    });
    let datos = {};
    try { datos = JSON.parse(respuesta.getContentText() || '{}'); } catch (err) { datos = {}; }
    if (!datos.ok) {
      return pagina_('No se ha podido completar', datos.error || 'Vuelve a la app y pulsa «Conectar mi Drive» otra vez.', false);
    }
    return pagina_('✓ Drive conectado', 'He creado la carpeta «' + NOMBRE_CARPETA + '» en tu Drive. Ya puedes cerrar esta pestaña y volver a la app.', true);
  } catch (err) {
    return pagina_('Ha ocurrido un error', String(err && err.message || err).substring(0, 300), false);
  }
}

/** Id de la carpeta «HostalFácil-Gestión» en el Drive de quien abre la página: la que creó este conector, o una nueva. */
function carpeta_() {
  const q = "mimeType = 'application/vnd.google-apps.folder' and name = '" + NOMBRE_CARPETA + "' and trashed = false and 'root' in parents";
  const lista = Drive.Files.list({ q: q, fields: 'files(id)', pageSize: 1 });
  if (lista.files && lista.files.length) return lista.files[0].id;
  return Drive.Files.create({ name: NOMBRE_CARPETA, mimeType: 'application/vnd.google-apps.folder' }, null, { fields: 'id' }).id;
}

/** Da acceso de Editor a la cuenta de la plataforma, sin enviarle correo de aviso. */
function compartir_(carpetaId) {
  Drive.Permissions.create({ type: 'user', role: 'writer', emailAddress: CUENTA_PLATAFORMA }, carpetaId, { sendNotificationEmail: false });
}

function pagina_(titulo, texto, correcto) {
  const color = correcto ? '#2e7d32' : '#c62828';
  const html = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>' + esc_(titulo) + '</title></head><body style="font-family:Arial,sans-serif;max-width:480px;margin:12vh auto;padding:0 20px;text-align:center">' +
    '<h1 style="color:' + color + '">' + esc_(titulo) + '</h1><p style="font-size:17px;line-height:1.5">' + esc_(texto) + '</p></body></html>';
  return HtmlService.createHtmlOutput(html).setTitle(titulo);
}

function esc_(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

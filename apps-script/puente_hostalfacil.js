/**
 * HostalFácil — Puente de la plataforma (Drive y correo)
 * -----------------------------------------------------------------
 * Se ejecuta con la cuenta de la PLATAFORMA (hostalfacil@gmail.com). Cada cliente comparte con esa cuenta (como Editor) su
 * carpeta de facturas; el backend le pide a este script que:
 *   - correo:   envíe un aviso interno por correo DESDE esta cuenta (respaldo de los avisos de WhatsApp);
 *   - ocr:      lea el texto de una foto o PDF escaneado (conversión de Google Drive, gratis);
 *   - guardar:  guarde la factura en <raíz>/<Propiedad>/<Año>/<nT>/<Gastos|Ingresos>/ y añada su fila a la hoja
 *               «Registro de facturas <Año>» de <raíz>/<Propiedad>/<Año>/;
 *   - ping:     compruebe que el script responde y que la carpeta raíz es accesible;
 *   - estructura: cree las subcarpetas del cliente dentro de su carpeta raíz (al conectar su Drive);
 *   - eliminar:   mande a la papelera el archivo de una factura y quite su fila de la hoja de registro;
 *   - actualizar: mueva y renombre el archivo de una factura y reemplace su fila en la hoja de registro.
 *
 * Requisitos (una vez):
 *   1. Servicios → añadir «Drive API» (servicio avanzado, identificador «Drive», versión v3).
 *   1b. Ejecutar UNA vez la función «autorizar» (▶) y aceptar los permisos de Drive, Documentos, Hojas y Gmail.
 *   2. Configuración del proyecto → Propiedades del script → SECRETO = el mismo valor que PUENTE_SECRETO en Render (y la dirección de la aplicación web, en PUENTE_URL).
 *   3. Implementar → Nueva implementación → Aplicación web → Ejecutar como: «Yo»; Quién tiene acceso: «Cualquier usuario».
 * El secreto NO va en el código: así se puede pegar o compartir este archivo sin exponerlo.
 */

function doGet() {
  return json_({ ok: true, servicio: 'puente-drive' });
}

function doPost(e) {
  let p;
  try {
    p = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ ok: false, error: 'peticion_no_valida' });
  }
  const secreto = PropertiesService.getScriptProperties().getProperty('SECRETO');
  if (!secreto || p.secret !== secreto) {
    return json_({ ok: false, error: 'no_autorizado' });
  }
  try {
    if (p.accion === 'ping') return json_(ping_(p));
    if (p.accion === 'estructura') return json_(estructura_(p));
    if (p.accion === 'eliminar') return json_(eliminar_(p));
    if (p.accion === 'actualizar') return json_(actualizar_(p));
    if (p.accion === 'ocr') return json_(ocr_(p));
    if (p.accion === 'correo') return json_(correo_(p));
    if (p.accion === 'guardar') return json_(guardar_(p));
    return json_({ ok: false, error: 'accion_desconocida' });
  } catch (err) {
    return json_({ ok: false, error: 'fallo_interno', detalle: String(err && err.message || err).substring(0, 300) });
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/** Ejecutar una vez a mano: al lanzarla, Google pide los permisos que usa todo el script (Drive, Documentos, Hojas y Gmail). */
function autorizar() {
  DriveApp.getRootFolder();
  GmailApp.getInboxUnreadCount();
  Logger.log('Permisos concedidos. Ya puedes publicar o actualizar la aplicación web.');
}

/** Aviso interno por correo, enviado desde la cuenta de la plataforma. Solo a direcciones con forma válida y un destino. */
function correo_(p) {
  const to = String(p.to || '').trim();
  if (!/^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$/.test(to)) return { ok: false, error: 'destino_no_valido' };
  GmailApp.sendEmail(to, String(p.asunto || 'Aviso ALCHOMES').substring(0, 200), String(p.cuerpo || ''));
  return { ok: true };
}

function ping_(p) {
  const raiz = DriveApp.getFolderById(p.raiz_id);   // lanza si no existe o no está compartida con esta cuenta
  return { ok: true, raiz: raiz.getName() };
}

/** Texto de una imagen o PDF: Drive lo convierte a documento de Google con OCR; el temporal se borra del todo. */
function ocr_(p) {
  const blob = Utilities.newBlob(Utilities.base64Decode(p.base64), p.mime || 'application/octet-stream', 'ocr');
  const temporal = Drive.Files.create({ name: 'ocr-temporal-' + Date.now(), mimeType: MimeType.GOOGLE_DOCS }, blob, { ocrLanguage: 'es' });
  try {
    return { ok: true, texto: DocumentApp.openById(temporal.id).getBody().getText() };
  } finally {
    try {
      Drive.Files.remove(temporal.id);   // borrado definitivo: la cuenta de la plataforma no acumula copias del texto de las facturas
    } catch (err) {
      DriveApp.getFileById(temporal.id).setTrashed(true);
    }
  }
}

/** Carpeta <base>/<seg1>/<seg2>/… creando las que falten. */
function carpeta_(base, ruta) {
  let c = base;
  (ruta || []).forEach(function (seg) {
    const nombre = String(seg).trim();
    if (!nombre) return;
    const it = c.getFoldersByName(nombre);
    c = it.hasNext() ? it.next() : c.createFolder(nombre);
  });
  return c;
}

/** Nombre libre en la carpeta: «x.pdf», «x (2).pdf», «x (3).pdf»… */
function nombreLibre_(carpeta, nombre) {
  if (!carpeta.getFilesByName(nombre).hasNext()) return nombre;
  const punto = nombre.lastIndexOf('.');
  const base = punto > 0 ? nombre.substring(0, punto) : nombre;
  const ext = punto > 0 ? nombre.substring(punto) : '';
  for (let n = 2; n < 100; n++) {
    const candidato = base + ' (' + n + ')' + ext;
    if (!carpeta.getFilesByName(candidato).hasNext()) return candidato;
  }
  return base + ' (' + Date.now() + ')' + ext;
}

/** Hoja de cálculo con ese título dentro de la carpeta; la crea con la cabecera si no existe. */
function hoja_(carpeta, titulo, cabecera) {
  const it = carpeta.getFilesByName(titulo);
  while (it.hasNext()) {
    const f = it.next();
    if (f.getMimeType() === MimeType.GOOGLE_SHEETS) return SpreadsheetApp.openById(f.getId());
  }
  const ss = SpreadsheetApp.create(titulo);
  DriveApp.getFileById(ss.getId()).moveTo(carpeta);
  const sh = ss.getSheets()[0];
  sh.setName('Facturas');
  sh.getRange(1, 1, 1, cabecera.length).setValues([cabecera]).setFontWeight('bold');
  sh.setFrozenRows(1);
  return ss;
}

/** ¿El archivo cuelga (a cualquier nivel) de la carpeta raíz de ese cliente? Impide tocar archivos de otros clientes con un id cualquiera. */
function dentro_(archivo, raizId) {
  let actual = archivo;
  for (let i = 0; i < 12; i++) {
    const it = actual.getParents();
    if (!it.hasNext()) return false;
    actual = it.next();
    if (actual.getId() === raizId) return true;
  }
  return false;
}

/** Carpeta <base>/<seg1>/… SIN crear nada: null si falta alguna. */
function carpetaExistente_(base, ruta) {
  let c = base;
  for (let i = 0; i < (ruta || []).length; i++) {
    const it = c.getFoldersByName(String(ruta[i]).trim());
    if (!it.hasNext()) return null;
    c = it.next();
  }
  return c;
}

/** Quita de la hoja de registro (si existe) la fila cuyo ID (última columna «ID») coincide. Devuelve cuántas filas quitó. */
function quitarFila_(raiz, registro) {
  if (!registro || !registro.id) return 0;
  const carpeta = carpetaExistente_(raiz, registro.ruta);
  if (!carpeta) return 0;
  const it = carpeta.getFilesByName(registro.titulo);
  let quitadas = 0;
  while (it.hasNext()) {
    const f = it.next();
    if (f.getMimeType() !== MimeType.GOOGLE_SHEETS) continue;
    const sh = SpreadsheetApp.openById(f.getId()).getSheets()[0];
    const datos = sh.getDataRange().getValues();
    const col = datos[0].indexOf('ID');
    if (col < 0) continue;
    for (let i = datos.length - 1; i >= 1; i--) {
      if (String(datos[i][col]) === String(registro.id)) { sh.deleteRow(i + 1); quitadas++; }
    }
  }
  return quitadas;
}

function eliminar_(p) {
  const bloqueo = LockService.getScriptLock();
  bloqueo.waitLock(30000);
  try {
    const raiz = DriveApp.getFolderById(p.raiz_id);
    let borrado = false;
    try {
      const archivo = DriveApp.getFileById(p.file_id);
      if (!dentro_(archivo, p.raiz_id)) return { ok: false, error: 'archivo_fuera_de_la_carpeta_del_cliente' };
      archivo.setTrashed(true);   // a la papelera de esta cuenta (30 días): se puede recuperar
      borrado = true;
    } catch (err) { /* el archivo ya no existe: no es un error */ }
    return { ok: true, borrado: borrado, filas: quitarFila_(raiz, p.registro) };
  } finally {
    bloqueo.releaseLock();
  }
}

function actualizar_(p) {
  const bloqueo = LockService.getScriptLock();
  bloqueo.waitLock(30000);
  try {
    const raiz = DriveApp.getFolderById(p.raiz_id);
    const archivo = DriveApp.getFileById(p.file_id);
    if (!dentro_(archivo, p.raiz_id)) return { ok: false, error: 'archivo_fuera_de_la_carpeta_del_cliente' };
    const destino = carpeta_(raiz, p.ruta);
    const mismoSitio = archivo.getParents().hasNext() && archivo.getParents().next().getId() === destino.getId();
    const nombre = (mismoSitio && archivo.getName() === p.nombre) ? p.nombre : nombreLibre_(destino, p.nombre);
    if (!mismoSitio) archivo.moveTo(destino);
    if (archivo.getName() !== nombre) archivo.setName(nombre);
    const url = archivo.getUrl();
    quitarFila_(raiz, p.registro_viejo);
    let hojaUrl = null;
    if (p.registro_nuevo) {
      const ss = hoja_(carpeta_(raiz, p.registro_nuevo.ruta), p.registro_nuevo.titulo, p.registro_nuevo.cabecera);
      ss.getSheets()[0].appendRow((p.registro_nuevo.fila || []).map(function (v) { return celda_(v === '{{ENLACE}}' ? url : v); }));
      hojaUrl = ss.getUrl();
    }
    return { ok: true, url: url, nombre: nombre, hoja_url: hojaUrl };
  } finally {
    bloqueo.releaseLock();
  }
}

/** Crea (si faltan) las carpetas de cada ruta dentro de la carpeta raíz del cliente. */
function estructura_(p) {
  const bloqueo = LockService.getScriptLock();
  bloqueo.waitLock(30000);
  try {
    const raiz = DriveApp.getFolderById(p.raiz_id);
    const rutas = p.rutas || [];
    rutas.forEach(function (ruta) { carpeta_(raiz, ruta); });
    return { ok: true, rutas: rutas.length };
  } finally {
    bloqueo.releaseLock();
  }
}

/** Un texto que empiece por = + - @ se interpretaría como fórmula al abrir la hoja: se deja como texto (apóstrofo delante). */
function celda_(v) {
  return (typeof v === 'string' && /^[=+\-@]/.test(v)) ? "'" + v : v;
}

function guardar_(p) {
  const bloqueo = LockService.getScriptLock();
  bloqueo.waitLock(30000);   // dos subidas a la vez no deben crear carpetas u hojas duplicadas
  try {
    const raiz = DriveApp.getFolderById(p.raiz_id);
    const destino = carpeta_(raiz, p.ruta);
    const nombre = nombreLibre_(destino, p.nombre);
    const archivo = destino.createFile(Utilities.newBlob(Utilities.base64Decode(p.base64), p.mime, nombre));
    if (p.descripcion) archivo.setDescription(String(p.descripcion).substring(0, 500));
    const url = archivo.getUrl();
    let hojaUrl = null;
    if (p.registro) {
      const ss = hoja_(carpeta_(raiz, p.registro.ruta), p.registro.titulo, p.registro.cabecera);
      const fila = (p.registro.fila || []).map(function (v) { return celda_(v === '{{ENLACE}}' ? url : v); });
      ss.getSheets()[0].appendRow(fila);
      hojaUrl = ss.getUrl();
    }
    return { ok: true, file_id: archivo.getId(), url: url, nombre: nombre, hoja_url: hojaUrl };
  } finally {
    bloqueo.releaseLock();
  }
}

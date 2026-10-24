/**
 * Avisos de reserva del correo → backend (cotejo con Beds24).
 *
 * Va en el proyecto «ALC Homes — Orquestador» (cuenta alchomes2025@gmail.com), que es donde llegan los avisos de las
 * plataformas. SOLO LEE: nunca responde, reenvía ni envía correos. Cada pasada manda al backend los avisos nuevos
 * (POST /reserva-correo), más los correos «<canal> returned an error message for … roomId …» con los que Beds24 avisa de errores
 * de canal; el backend los analiza y el watchdog los coteja con Beds24 (services/reservas_correo.py).
 *
 * Instalación:
 *  1. Pegar este archivo como un archivo nuevo del proyecto (no sustituye a ejecutarWatchdog).
 *  2. Propiedades del script → añadir SECRETO_COMPARTIDO = el valor de APPS_SCRIPT_EMAIL_SECRET de Render
 *     (no escribir el secreto en el código).
 *  3. Activadores → añadir revisarAvisosReserva, «Según el tiempo», cada 5 minutos.
 *  4. Ejecutar revisarAvisosReserva una vez a mano (aceptar permisos de Gmail y de conexión externa).
 */
var AVISOS_URL = "https://hostal-pdf-extractor.onrender.com/reserva-correo";
var AVISOS_REMITENTES = "booking.com OR airbnb.com OR hostelworld.com OR holidu.com OR expedia.com OR vrbo.com OR agoda.com";
var AVISOS_DIAS = 3;          // ventana de búsqueda
var AVISOS_LOTE = 20;         // mensajes por llamada
var AVISOS_MAX_CUERPO = 12000;
var AVISOS_MAX_VISTOS = 600;  // ids recordados para no reenviar (el backend también deduplica)

function revisarAvisosReserva() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) return;
  try {
    var props = PropertiesService.getScriptProperties();
    var secreto = props.getProperty("SECRETO_COMPARTIDO");
    if (!secreto) throw new Error("Falta la propiedad SECRETO_COMPARTIDO");
    var vistos = JSON.parse(props.getProperty("AVISOS_VISTOS") || "[]");
    var conocidos = {};
    vistos.forEach(function (id) { conocidos[id] = true; });

    var hilos = GmailApp.search("(from:(" + AVISOS_REMITENTES + ") OR from:(noreply_htl@trip.com) OR subject:\"returned an error message\") newer_than:" + AVISOS_DIAS + "d", 0, 50);
    var nuevos = [];
    hilos.forEach(function (hilo) {
      hilo.getMessages().forEach(function (m) {
        var id = m.getId();
        if (conocidos[id]) return;
        nuevos.push({
          id: id,
          fecha: Utilities.formatDate(m.getDate(), "Europe/Madrid", "yyyy-MM-dd'T'HH:mm:ss"),
          de: m.getFrom(),
          asunto: m.getSubject(),
          cuerpo: m.getPlainBody().substring(0, AVISOS_MAX_CUERPO)
        });
      });
    });

    for (var i = 0; i < nuevos.length; i += AVISOS_LOTE) {
      var lote = nuevos.slice(i, i + AVISOS_LOTE);
      var resp = UrlFetchApp.fetch(AVISOS_URL, {
        method: "post",
        contentType: "application/json",
        payload: JSON.stringify({ secret: secreto, mensajes: lote }),
        muteHttpExceptions: true
      });
      if (resp.getResponseCode() !== 200) {
        // No se anota nada del lote: se reintenta en la siguiente pasada (el backend deduplica por id)
        console.error("Backend respondió " + resp.getResponseCode() + ": " + resp.getContentText().substring(0, 200));
        break;
      }
      lote.forEach(function (m) { vistos.push(m.id); });
      props.setProperty("AVISOS_VISTOS", JSON.stringify(vistos.slice(-AVISOS_MAX_VISTOS)));
    }
  } finally {
    lock.releaseLock();
  }
}

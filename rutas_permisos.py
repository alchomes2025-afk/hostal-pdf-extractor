"""
rutas_permisos.py — Qué permiso exige CADA ruta de la app de gestión (`/mobile/...`).

Una sola tabla, fácil de auditar. Un hook `before_request` (mobile_routes.exigir_permiso) la aplica a las tres blueprints (mobile, facturas y limpieza):
  · una ruta en RUTAS_PUBLICAS no pide nada (el login, el keepalive y el aviso firmado del conector de Drive);
  · una ruta en PERMISOS_RUTAS pide AL MENOS UNO de sus permisos (services/accesos.py);
  · una ruta que no esté en ninguna de las dos se RECHAZA (403): al añadir una ruta hay que declarar aquí su permiso, y un test recorre todas las
    rutas y falla si falta alguna.
Ver ACCESOS.md para la tabla de perfiles y permisos.
"""
from services import accesos as a

RUTAS_PUBLICAS = {
    "mobile.health",                 # keepalive (UptimeRobot): no devuelve datos
    "mobile.mobile_login",           # el login valida el PIN por su cuenta y limita intentos
    "facturas.drive_conectado",      # lo llama el conector de Drive, firmado con PUENTE_SECRETO (no es de un usuario)
}

PERMISOS_RUTAS = {
    # Calendario
    "mobile.mobile_rooms": (a.CALENDARIO,),
    "mobile.all_data": (a.CALENDARIO,),
    "mobile.reload_data": (a.CALENDARIO,),
    "mobile.check_changes": (a.CALENDARIO,),
    # Avisos de la app para el historial del sustituto (botones, reservas que mira)
    "mobile.mobile_registro_evento": (a.OPERATIVA,),
    # Identidad del propietario por dispositivo (Ione o Adrián)
    "mobile.mobile_dispositivo": (a.IDENTIFICAR,),
    # Reservas y precios
    "mobile.create_booking": (a.RESERVAS,),
    "mobile.block_dates": (a.RESERVAS,),
    "mobile.cancel_booking": (a.RESERVAS,),
    "mobile.edit_booking": (a.RESERVAS,),
    "mobile.mobile_update": (a.PRECIOS,),
    # Finanzas (la propiedad concreta se comprueba dentro de la ruta)
    "mobile.mobile_finance": (a.FINANZAS_PRIMAVERA, a.FINANZAS_HOSTAL),
    "mobile.mobile_finance_desglose": (a.FACTURAS,),
    # Asistente y envío de emergencia
    "mobile.mobile_assistant": (a.ASISTENTE,),
    "mobile.mobile_assistant_preparar_codigos": (a.EMERGENCIA,),
    "mobile.mobile_assistant_enviar_codigos": (a.EMERGENCIA,),
    # Diagnóstico y mantenimiento
    "mobile.token_status": (a.SISTEMA,),
    "mobile.test_whatsapp": (a.SISTEMA,),
    "mobile.test_email": (a.SISTEMA,),
    "mobile.diag_registro_completado": (a.SISTEMA,),
    "mobile.get_logs": (a.SISTEMA,),
    "mobile.debug_overrides": (a.SISTEMA,),
    "mobile.debug_bookings": (a.SISTEMA,),
    "mobile.test_sync": (a.SISTEMA,),
    "mobile.fix_zero_prices": (a.SISTEMA,),
    "mobile.set_booking_price": (a.SISTEMA,),
    "facturas.diag_ia": (a.SISTEMA,),
    # Facturas, Asesor Contable y resumen del gestor
    "facturas.listar_facturas": (a.FACTURAS,),
    "facturas.config_facturas": (a.FACTURAS,),
    "facturas.conectar_drive": (a.FACTURAS,),
    "facturas.leer_factura": (a.FACTURAS,),
    "facturas.guardar_factura": (a.FACTURAS,),
    "facturas.editar_factura": (a.FACTURAS,),
    "facturas.eliminar_factura": (a.FACTURAS,),
    "facturas.original_factura": (a.FACTURAS,),
    "facturas.facturas_conocidas": (a.FACTURAS,),
    "facturas.meses_facturas": (a.FACTURAS,),
    "facturas.subir_extracto": (a.FACTURAS,),
    "facturas.cotejar_facturas": (a.FACTURAS,),
    "facturas.asesor_estado": (a.FACTURAS,),
    "facturas.asesor_respuestas": (a.FACTURAS,),
    "facturas.resumen_trimestral": (a.FACTURAS,),
    "facturas.resumen_trimestral_hoja": (a.FACTURAS,),
    # Operativa de Gestión (el sustituto y el propietario) y edición de la base de conocimiento (solo el propietario)
    "operativa.indice": (a.OPERATIVA,),
    "operativa.entrada": (a.OPERATIVA,),
    "operativa.kb_listar": (a.ENTRENAMIENTO,),
    "operativa.kb_guardar": (a.ENTRENAMIENTO,),
    "operativa.kb_borrar": (a.ENTRENAMIENTO,),
    "operativa.kb_importar_faq": (a.ENTRENAMIENTO,),
    # Entrenamiento: entrevista a Ione (solo el propietario)
    "entrenamiento.temas": (a.ENTRENAMIENTO,),
    "entrenamiento.sesion": (a.ENTRENAMIENTO,),
    "entrenamiento.mensaje": (a.ENTRENAMIENTO,),
    # Historial de sustitución (solo el propietario)
    "historial_sustitucion.eventos": (a.HISTORIAL,),
    "historial_sustitucion.preguntar": (a.HISTORIAL,),
    # Limpieza
    "limpieza.calendario": (a.LIMPIEZA_CALENDARIO,),
    "limpieza.incidencia": (a.LIMPIEZA_MARCAR,),
    "limpieza.incidencias": (a.INCIDENCIAS_VER,),
}

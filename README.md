# hostal-pdf-extractor

Backend Flask del sistema de self check-in de ALC Homes (check-in del huésped, resúmenes y avisos por WhatsApp, monitorización) y de la app móvil de gestión (`/mobile`, frontend en `mobile-app/`).

> El nombre del repo es histórico: ya no procesa PDFs. El sistema antiguo de Gmail/PDF se eliminó en septiembre de 2026.

## Cómo está organizado

| Ruta | Contenido |
|---|---|
| `app.py` | Crea la app, registra los blueprints y comprueba las variables críticas al arrancar |
| `config.py` | Variables de entorno, habitaciones, enlaces y mapas de RPV, reservas de prueba |
| `routes/` | Un blueprint por grupo de endpoints (check-in, chat, historial, resumen, watchdog, depuración) |
| `services/` | Lógica sin rutas (Beds24, RPV, WhatsApp, resúmenes, avisos, emparejado de nombres) |
| `mobile_routes.py` | Blueprint `/mobile` de la app móvil |
| `mobile-app/` | Frontend de la app móvil (GitHub Pages) |

La descripción completa del sistema (endpoints, reglas de negocio, integraciones) está en [`claude.md`](claude.md).

## Despliegue

- Render, despliegue automático al hacer push a `main`.
- Build: `pip install -r requirements.txt` · Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 60` (igual que el `Procfile`).
- Python 3.11 (`runtime.txt`).
- Las variables de entorno se configuran solo en el panel de Render; los nombres están en `config.py` y `mobile_routes.py`. No se guardan valores en el repo.

## Desarrollo local

```bash
pip install -r requirements.txt
gunicorn app:app --bind 127.0.0.1:5000
```

Sin variables de entorno el servicio arranca en modo degradado (los avisos de `_startup_check` lo indican).

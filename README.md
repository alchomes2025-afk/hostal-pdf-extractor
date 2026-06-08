# Hostal PDF Extractor

Microservicio Flask para desencriptar y extraer datos de partes de viajeros en PDF.

## Endpoints

### GET /
Health check. Devuelve `{"status": "ok"}`.

### POST /extraer
Recibe un PDF encriptado en base64 y devuelve los datos extraídos.

**Body:**
```json
{
  "pdf_base64": "<base64 del PDF>",
  "pdf_filename": "Habitacion Simple 2.pdf",
  "token": "<API_TOKEN>"
}
```

**Respuesta:**
```json
{
  "ok": true,
  "habitacion": "Habitación Simple 2",
  "email": "cliente@mail.com",
  "fecha_entrada": "2026-06-10",
  "fecha_salida": "2026-06-11"
}
```

## Variables de entorno en Railway

| Variable | Descripción |
|---|---|
| `PDF_PASSWORD` | Contraseña de los PDFs (por defecto: Alchomes2025) |
| `API_TOKEN` | Token secreto para autenticar llamadas desde Make |

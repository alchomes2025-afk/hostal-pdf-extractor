"""
Comprueba EN PRODUCCIÓN (o donde se indique) que cada perfil por PIN ve solo lo suyo. Solo hace peticiones de lectura baratas, más una pregunta de dinero al asistente (que
se contesta sin gastar IA). Entra una vez con cada PIN (el sustituto, con el nombre «Verificación automática», así que deja unas pocas filas en su historial).

Uso:  python scripts/verificar_perfiles.py [URL_BASE]      (por defecto https://hostal-pdf-extractor.onrender.com/mobile)
Salida: una tabla perfil × ruta con ✔ si el resultado es el esperado y ✘ si no; código de salida 1 si algo falla. No imprime ningún PIN.
"""
import sys

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://hostal-pdf-extractor.onrender.com/mobile").rstrip("/")
PINES = {"propietario (1379)": "1379", "gestor primavera (9999)": "9999", "gestor (2486)": "2486", "sustituto (2589)": "2589", "limpieza (4268)": "4268"}
PROP, PRIM, GEST, SUST, LIMP = list(PINES)
CASA = 720841

# ruta → {perfil: estado esperado}. Lo que no aparece, se espera 403.
LECTURAS = [
    ("GET /rooms", "/rooms", {PROP: 200, PRIM: 200, GEST: 200, SUST: 200}),
    ("GET /all-data", "/all-data", {PROP: 200, PRIM: 200, GEST: 200, SUST: 200}),
    ("GET /finance (Primavera)", "/finance?propertyId=349341&month=2026-09", {}),          # solo se comprueban las denegaciones (el positivo gasta créditos de Beds24)
    ("GET /finance (Hostal)", "/finance?propertyId=339751&month=2026-09", {}),
    ("GET /facturas/config", "/facturas/config", {PROP: 200}),
    ("GET /logs", "/logs?limit=1", {PROP: 200}),
    ("GET /operativa/indice", "/operativa/indice", {PROP: 200, SUST: 200}),
    ("GET /operativa/kb", "/operativa/kb", {PROP: 200}),
    ("GET /entrenamiento/temas", "/entrenamiento/temas", {PROP: 200}),
    ("GET /historial-sustitucion", "/historial-sustitucion?limite=1", {PROP: 200}),
    ("GET /limpieza/incidencias", "/limpieza/incidencias", {PROP: 200, PRIM: 200}),
    ("GET /limpieza/calendario", "/limpieza/calendario", {LIMP: 200}),
    ("GET /test-whatsapp (no se llama)", None, {}),
]
SIN_CONSULTAR_POSITIVO = {"GET /finance (Primavera)": {PROP, PRIM}, "GET /finance (Hostal)": {PROP}}   # se esperan 200 pero no se piden


def entrar(pin):
    cuerpo = {"pin": pin}
    r = requests.post(f"{BASE}/login", json=cuerpo, timeout=60)
    d = r.json()
    if d.get("necesita_nombre"):
        d = requests.post(f"{BASE}/login", json={**cuerpo, "nombre": "Verificación automática"}, timeout=60).json()
    return d


def main():
    fallos, filas = [], []
    sesiones = {}
    for nombre, pin in PINES.items():
        d = entrar(pin)
        if not d.get("ok") or not d.get("token"):
            print(f"✘ No se pudo entrar con el perfil {nombre}: {d.get('error')}")
            return 1
        sesiones[nombre] = {"Authorization": "Bearer " + d["token"]}
    for titulo, ruta, esperados in LECTURAS:
        if ruta is None:
            continue
        fila = [titulo]
        for perfil, cab in sesiones.items():
            esperado = esperados.get(perfil, 403)
            if titulo in SIN_CONSULTAR_POSITIVO and perfil in SIN_CONSULTAR_POSITIVO[titulo]:
                fila.append("—")
                continue
            r = requests.get(BASE + ruta, headers=cab, timeout=90)
            ok = r.status_code == esperado
            fila.append("✔" if ok else f"✘ {r.status_code}≠{esperado}")
            if not ok:
                fallos.append((titulo, perfil, r.status_code, esperado))
        filas.append(fila)

    # La Casa de la Primavera: solo para 1379 y 9999
    for perfil, cab in sesiones.items():
        r = requests.get(BASE + "/all-data", headers=cab, timeout=90)
        if r.status_code != 200:
            continue
        ve_casa = any(str(x.get("id")) == str(CASA) for x in r.json().get("rooms", [])) or any(str(b.get("roomId")) == str(CASA) for b in r.json().get("bookings", []))
        debe = perfil in (PROP, PRIM)
        if ve_casa != debe:
            fallos.append(("La Casa de la Primavera en el calendario", perfil, "la ve" if ve_casa else "no la ve", "debe verla" if debe else "no debe verla"))
    filas.append(["La Primavera solo para 1379 y 9999"] + ["✔" if not any(f[0].startswith("La Casa") and f[1] == p for f in fallos) else "✘" for p in sesiones])

    # Lo que debe rechazarse sin credenciales o con credenciales falsas
    comprobaciones = [
        ("sin PIN ni sesión → 401", requests.get(BASE + "/rooms", timeout=60).status_code, 401),
        ("sesión manipulada → 401", requests.get(BASE + "/rooms", headers={"Authorization": "Bearer " + sesiones[GEST]["Authorization"][7:-3] + "AAA"}, timeout=60).status_code, 401),
        ("PIN del sustituto SIN sesión → 401", requests.get(BASE + "/rooms", headers={"x-app-pin": "2589"}, timeout=60).status_code, 401),
    ]
    for titulo, obtenido, esperado in comprobaciones:
        filas.append([titulo, "✔" if obtenido == esperado else f"✘ {obtenido}≠{esperado}"])
        if obtenido != esperado:
            fallos.append((titulo, "-", obtenido, esperado))

    # Finanzas en el asistente: sin acceso, la respuesta sale fija y sin gastar IA
    for perfil in (GEST, SUST):
        r = requests.post(BASE + "/assistant", json={"mensajes": [{"role": "user", "content": "¿Cuánto hemos ganado este mes? ¿Qué beneficio dejó?"}], "finanzas": True},
                          headers=sesiones[perfil], timeout=90)
        texto = (r.json() or {}).get("respuesta", "")
        ok = r.status_code == 200 and texto == "Dirígete a Ione."
        filas.append([f"Asistente: pregunta de dinero ({perfil})", "✔" if ok else f"✘ {r.status_code} {texto[:40]!r}"])
        if not ok:
            fallos.append(("Asistente: pregunta de dinero", perfil, r.status_code, "Dirígete a Ione."))

    ancho = max(len(f[0]) for f in filas) + 2
    print(" " * ancho + " | ".join(p.split(" (")[0][:10].ljust(10) for p in sesiones))
    for f in filas:
        print(f[0].ljust(ancho) + " | ".join(str(c).ljust(10) for c in f[1:]))
    print()
    if fallos:
        print(f"✘ {len(fallos)} comprobaciones fallan:")
        for f in fallos:
            print("  ", f)
        return 1
    print("✔ Todos los perfiles ven solo lo que les corresponde.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

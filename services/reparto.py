"""
services/reparto.py — Cómo se reparte la comisión de gestión de ALC Homes entre las personas (solo ALC Homes: no es algo para otros clientes).

La comisión de gestión es el 15 % de los ingresos NETOS (la facturación una vez descontadas las comisiones de los portales). Lo que se
muestra con el botón «Desglose» (protegido con un PIN propio) es cómo se reparte esa cantidad:

  · La Casa de la Primavera: Ione 80 % · Adrián 20 %.
  · Hostal: Ione 30 % «en concepto de gestión»; del 70 % restante, Adrián se lleva el 20 %; de lo que queda, Ione el 70 % y Conchi el 30 %.
    (Sobre 100: Ione 30 + 39,20 = 69,20 · Adrián 14 · Conchi 16,80.)

Se trabaja en céntimos para que las partes sumen EXACTAMENTE la comisión: el redondeo sobrante se le da a la última persona de cada reparto.
"""

PORCENTAJES_PRIMAVERA = (("Ione", 80), ("Adrián", 20))


def _cent(importe):
    return int(round(float(importe) * 100))


def _partir(centimos, porcentajes):
    """Reparte `centimos` por porcentajes (que suman 100); el sobrante del redondeo va a la última parte."""
    partes = [centimos * pct // 100 for _, pct in porcentajes]
    partes[-1] += centimos - sum(partes)
    return partes


def desglose_primavera(comision):
    c = _cent(comision)
    ione, adrian = _partir(c, PORCENTAJES_PRIMAVERA)
    return {"total": c / 100, "partes": [
        {"nombre": "Ione", "concepto": "80 % de la comisión de gestión", "importe": ione / 100},
        {"nombre": "Adrián", "concepto": "20 % de la comisión de gestión", "importe": adrian / 100},
    ]}


def desglose_hostal(comision):
    c = _cent(comision)
    ione_gestion, resto = _partir(c, (("Ione", 30), ("resto", 70)))
    adrian, queda = _partir(resto, (("Adrián", 20), ("queda", 80)))
    ione_resto, conchi = _partir(queda, (("Ione", 70), ("Conchi", 30)))
    return {"total": c / 100, "partes": [
        {"nombre": "Ione", "concepto": "30 % en concepto de gestión", "importe": ione_gestion / 100},
        {"nombre": "Adrián", "concepto": "20 % del 70 % restante", "importe": adrian / 100},
        {"nombre": "Ione", "concepto": "70 % de lo que queda", "importe": ione_resto / 100},
        {"nombre": "Conchi", "concepto": "30 % de lo que queda", "importe": conchi / 100},
    ], "totales": [
        {"nombre": "Ione", "importe": (ione_gestion + ione_resto) / 100},
        {"nombre": "Adrián", "importe": adrian / 100},
        {"nombre": "Conchi", "importe": conchi / 100},
    ]}


def desglose(es_hostal, comision):
    return desglose_hostal(comision) if es_hostal else desglose_primavera(comision)

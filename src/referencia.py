"""Cálculo determinístico (sin LLM) que replica la planilla del estudio.

Se usa solo para medir la exactitud del motor contra el criterio de la HU1
(diferencia <= 1 %). Reglas simplificadas y alícuotas ficticias del curso.
"""
from __future__ import annotations

from contrato import TIPOS_A, Comprobante, Entrada, Regimen


def _mes(c: Comprobante) -> str:
    return c.fecha.strftime("%Y-%m")


def _meses_ultimos_12(periodo: str) -> set[str]:
    anio, mes = map(int, periodo.split("-"))
    meses = set()
    for _ in range(12):
        meses.add(f"{anio:04d}-{mes:02d}")
        mes -= 1
        if mes == 0:
            anio, mes = anio - 1, 12
    return meses


def calcular(e: Entrada) -> dict:
    emitidos = [c for c in e.comprobantes_emitidos if _mes(c) == e.periodo]
    recibidos = [c for c in e.comprobantes_recibidos if _mes(c) == e.periodo]
    ventanas = _meses_ultimos_12(e.periodo)
    fact_12m = sum(c.signo * c.total * c.tipo_cambio for c in e.comprobantes_emitidos if _mes(c) in ventanas)

    impuestos: dict[str, float] = {}
    if e.regimen == Regimen.MONOTRIBUTO:
        base = max(0.0, sum(c.signo * c.total * c.tipo_cambio for c in emitidos))
        impuestos["MONOTRIBUTO"] = e.cuota_monotributo or 0.0
    else:
        base = max(0.0, sum(c.signo * (c.neto_gravado + c.no_gravado + c.exento) * c.tipo_cambio for c in emitidos))
        debito = sum(c.signo * c.iva * c.tipo_cambio for c in emitidos)
        credito = sum(c.signo * c.iva * c.tipo_cambio for c in recibidos if c.tipo in TIPOS_A)
        impuestos["IVA"] = max(0.0, debito - credito)
    impuestos["IIBB"] = base * e.alicuota_iibb
    impuestos["TEM"] = base * e.alicuota_tem
    return {
        "base_ventas": round(base, 2),
        "impuestos": {k: round(v, 2) for k, v in impuestos.items()},
        "total": round(sum(impuestos.values()), 2),
        "facturacion_ultimos_12m": round(fact_12m, 2),
    }

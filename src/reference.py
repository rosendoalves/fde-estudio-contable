"""Cálculo determinístico (sin LLM) que replica la planilla del estudio.

Se usa solo para medir la exactitud del motor contra el criterio de la HU1
(diferencia <= 1 %). Reglas simplificadas y alícuotas ficticias del curso.
"""
from __future__ import annotations

from contract import TYPE_A, EngineInput, Regime


def last_12_months(period: str) -> set[str]:
    year, month = map(int, period.split("-"))
    months = set()
    for _ in range(12):
        months.add(f"{year:04d}-{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return months


def compute(e: EngineInput) -> dict:
    issued = [v for v in e.issued if v.period == e.period]
    received = [v for v in e.received if v.period == e.period]
    window = last_12_months(e.period)
    revenue_12m = sum(v.sign * v.total * v.exchange_rate for v in e.issued if v.period in window)

    taxes: dict[str, float] = {}
    if e.regime == Regime.MONOTRIBUTO:
        base = max(0.0, sum(v.sign * v.total * v.exchange_rate for v in issued))
        taxes["MONOTRIBUTO"] = e.monotributo_fee or 0.0
    else:
        base = max(0.0, sum(v.sign * (v.taxed_net + v.untaxed + v.exempt) * v.exchange_rate for v in issued))
        debit = sum(v.sign * v.vat * v.exchange_rate for v in issued)
        credit = sum(v.sign * v.vat * v.exchange_rate for v in received if v.type in TYPE_A)
        taxes["IVA"] = max(0.0, debit - credit)
    taxes["IIBB"] = base * e.iibb_rate
    taxes["TEM"] = base * e.tem_rate
    return {
        "sales_base": round(base, 2),
        "taxes": {k: round(v, 2) for k, v in taxes.items()},
        "total": round(sum(taxes.values()), 2),
        "revenue_last_12m": round(revenue_12m, 2),
    }

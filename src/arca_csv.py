"""Lectura de los CSV de "Mis Comprobantes" de ARCA (emitidos y recibidos)."""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from contract import Currency, Voucher


def _number(value: str) -> float:
    """ARCA usa coma decimal y deja celdas vacías: '1487,50' -> 1487.5, '' -> 0."""
    value = (value or "").strip()
    return float(value.replace(".", "").replace(",", ".")) if value else 0.0


def read_vouchers(path: Path) -> list[Voucher]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    vouchers = []
    for row in rows:
        counterparty = row.get("Denominación Receptor") or row.get("Denominación Emisor") or "-"
        vouchers.append(
            Voucher(
                date=dt.date.fromisoformat(row["Fecha de Emisión"]),
                type=int(row["Tipo de Comprobante"]),
                point_of_sale=int(row["Punto de Venta"]),
                number=int(row["Número Desde"]),
                counterparty=counterparty,
                currency=Currency.ARS if row["Moneda"].strip() in ("$", "PES", "ARS") else Currency.USD,
                exchange_rate=_number(row["Tipo Cambio"]) or 1.0,
                taxed_net=_number(row["Imp. Neto Gravado Total"]),
                untaxed=_number(row["Imp. Neto No Gravado"]),
                exempt=_number(row["Imp. Op. Exentas"]),
                vat=_number(row["Total IVA"]),
                total=_number(row["Imp. Total"]),
            )
        )
    return vouchers


def load_client(folder: Path) -> dict:
    """Devuelve la config del cliente + sus comprobantes, listo para armar un EngineInput."""
    folder = Path(folder)
    data = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    data.pop("name", None)
    data["issued"] = read_vouchers(folder / "issued.csv")
    received = folder / "received.csv"
    data["received"] = read_vouchers(received) if received.exists() else []
    return data

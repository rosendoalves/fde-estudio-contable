"""Lectura de los CSV de "Mis Comprobantes" de ARCA (emitidos y recibidos)."""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from contrato import Comprobante, Moneda


def _num(valor: str) -> float:
    """ARCA usa coma decimal y deja celdas vacías: '1487,50' -> 1487.5, '' -> 0."""
    valor = (valor or "").strip()
    return float(valor.replace(".", "").replace(",", ".")) if valor else 0.0


def leer_comprobantes(ruta: Path) -> list[Comprobante]:
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        filas = list(csv.DictReader(f, delimiter=";"))
    comprobantes = []
    for fila in filas:
        contraparte = fila.get("Denominación Receptor") or fila.get("Denominación Emisor") or "-"
        comprobantes.append(
            Comprobante(
                fecha=dt.date.fromisoformat(fila["Fecha de Emisión"]),
                tipo=int(fila["Tipo de Comprobante"]),
                punto_venta=int(fila["Punto de Venta"]),
                numero=int(fila["Número Desde"]),
                contraparte=contraparte,
                moneda=Moneda.PESOS if fila["Moneda"].strip() in ("$", "PES", "ARS") else Moneda.DOLARES,
                tipo_cambio=_num(fila["Tipo Cambio"]) or 1.0,
                neto_gravado=_num(fila["Imp. Neto Gravado Total"]),
                no_gravado=_num(fila["Imp. Neto No Gravado"]),
                exento=_num(fila["Imp. Op. Exentas"]),
                iva=_num(fila["Total IVA"]),
                total=_num(fila["Imp. Total"]),
            )
        )
    return comprobantes


def cargar_cliente(carpeta: Path) -> dict:
    """Devuelve config del cliente + comprobantes, listo para armar una Entrada."""
    carpeta = Path(carpeta)
    datos = json.loads((carpeta / "config.json").read_text(encoding="utf-8"))
    datos.pop("nombre", None)
    datos["comprobantes_emitidos"] = leer_comprobantes(carpeta / "emitidos.csv")
    recibidos = carpeta / "recibidos.csv"
    datos["comprobantes_recibidos"] = leer_comprobantes(recibidos) if recibidos.exists() else []
    return datos

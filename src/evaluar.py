"""Corre los 5 casos de src/casos.json contra la API real y compara con el KPI del PE1.

KPI técnico (PE1): exactitud >= 95 %, latencia < 30 s y costo < USD 0,05 por cliente.
Exactitud de un caso:
  - casos "ok": proporción de impuestos cuyo monto difiere <= 1 % (o <= $1) del
    cálculo determinístico de referencia.py (criterio de la HU1);
  - casos no "ok": 1 si el estado devuelto es el esperado, 0 si no.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
from arca_csv import cargar_cliente  # noqa: E402
from contrato import Entrada  # noqa: E402
from motor import ejecutar  # noqa: E402
import referencia  # noqa: E402

KPI = {"exactitud": 0.95, "latencia_ms": 30_000, "costo_usd": 0.05}


def exactitud(caso: dict, datos: dict, r) -> float:
    if not r.ok:
        return 0.0
    if caso["estado_esperado"] != "ok":
        return 1.0 if r.salida.estado.value == caso["estado_esperado"] else 0.0
    if r.salida.estado.value != "ok":
        return 0.0
    ref = referencia.calcular(Entrada.model_validate(datos))["impuestos"]
    obtenidos = {i.impuesto.value: i.monto for i in r.salida.impuestos}
    aciertos = sum(1 for k, v in ref.items() if abs(obtenidos.get(k, -1) - v) <= max(1.0, 0.01 * v))
    return aciertos / len(ref)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    casos = json.loads((RAIZ / "src" / "casos.json").read_text(encoding="utf-8"))
    filas = []
    for caso in casos:
        datos = cargar_cliente(RAIZ / caso["cliente"])
        datos.update(periodo=caso["periodo"], consulta=caso["consulta"])
        r = ejecutar(datos)
        exa = exactitud(caso, datos, r)
        m = r.metricas
        filas.append({
            "caso": caso["id"], "tipo": caso["tipo"],
            "estado": r.salida.estado.value if r.ok else f"error:{r.error.tipo}",
            "esperado": caso["estado_esperado"], "exactitud": round(exa, 3),
            "intentos": m.intentos, "tokens_entrada": m.tokens_entrada, "tokens_salida": m.tokens_salida,
            "costo_usd": m.costo_usd, "latencia_ms": m.latencia_ms,
        })

    n = len(filas)
    prom = {k: sum(f[k] for f in filas) / n for k in ("exactitud", "tokens_entrada", "tokens_salida", "costo_usd", "latencia_ms")}
    print(f"\n{'caso':<5}{'tipo':<15}{'estado':<22}{'exact.':>7}{'int.':>5}{'tok in':>8}{'tok out':>8}{'USD':>9}{'ms':>8}")
    for f in filas:
        print(f"{f['caso']:<5}{f['tipo']:<15}{f['estado']:<22}{f['exactitud']:>7.0%}{f['intentos']:>5}"
              f"{f['tokens_entrada']:>8}{f['tokens_salida']:>8}{f['costo_usd']:>9.4f}{f['latencia_ms']:>8}")
    print(f"{'PROM':<42}{prom['exactitud']:>7.0%}{'':>5}{prom['tokens_entrada']:>8.0f}{prom['tokens_salida']:>8.0f}"
          f"{prom['costo_usd']:>9.4f}{prom['latencia_ms']:>8.0f}")
    print(f"\nKPI PE1 -> exactitud {prom['exactitud']:.0%} (meta >= 95 %): {'CUMPLE' if prom['exactitud'] >= KPI['exactitud'] else 'NO CUMPLE'}"
          f" | latencia {prom['latencia_ms']/1000:.1f} s (meta < 30 s): {'CUMPLE' if prom['latencia_ms'] < KPI['latencia_ms'] else 'NO CUMPLE'}"
          f" | costo USD {prom['costo_usd']:.4f} (meta < 0,05): {'CUMPLE' if prom['costo_usd'] < KPI['costo_usd'] else 'NO CUMPLE'}")

    salida = RAIZ / "resultados" / f"corrida_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
    salida.parent.mkdir(exist_ok=True)
    with open(salida, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(filas[0].keys()))
        w.writeheader(); w.writerows(filas)
    print(f"Resultados guardados en {salida.relative_to(RAIZ)}")


if __name__ == "__main__":
    main()

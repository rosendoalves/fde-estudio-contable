"""Corre los 5 casos de src/cases.json contra la API real y compara con el KPI del PE1.

KPI técnico (PE1): exactitud >= 95 %, latencia < 30 s y costo < USD 0,05 por cliente.
Exactitud de un caso:
  - casos "ok": proporción de impuestos cuyo monto difiere <= 1 % (o <= $1) del
    cálculo determinístico de reference.py (criterio de la HU1);
  - resto: 1 si el status devuelto es el esperado, 0 si no.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import reference  # noqa: E402
from arca_csv import load_client  # noqa: E402
from contract import EngineInput  # noqa: E402
from motor import run  # noqa: E402

KPI = {"accuracy": 0.95, "latency_ms": 30_000, "cost_usd": 0.05}


def accuracy(case: dict, data: dict, result) -> float:
    if not result.ok:
        return 0.0
    status = result.output.status.value
    if case["expected_status"] != "ok":
        return 1.0 if status == case["expected_status"] else 0.0
    if status != "ok":
        return 0.0
    expected = reference.compute(EngineInput.model_validate(data))["taxes"]
    got = {t.tax.value: t.amount for t in result.output.taxes}
    hits = sum(1 for k, v in expected.items() if abs(got.get(k, -1) - v) <= max(1.0, 0.01 * v))
    return hits / len(expected)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cases = json.loads((ROOT / "src" / "cases.json").read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        data = load_client(ROOT / case["client"])
        data.update(period=case["period"], query=case["query"])
        result = run(data)
        m = result.metrics
        rows.append({
            "case": case["id"], "kind": case["kind"],
            "status": result.output.status.value if result.ok else f"error:{result.error.kind}",
            "expected": case["expected_status"], "accuracy": round(accuracy(case, data, result), 3),
            "attempts": m.attempts, "input_tokens": m.input_tokens, "output_tokens": m.output_tokens,
            "cost_usd": m.cost_usd, "latency_ms": m.latency_ms,
        })

    n = len(rows)
    avg = {k: sum(r[k] for r in rows) / n for k in ("accuracy", "input_tokens", "output_tokens", "cost_usd", "latency_ms")}
    print(f"\n{'case':<5}{'kind':<12}{'status':<24}{'acc.':>6}{'att.':>5}{'tok in':>8}{'tok out':>8}{'USD':>9}{'ms':>8}")
    for r in rows:
        print(f"{r['case']:<5}{r['kind']:<12}{r['status']:<24}{r['accuracy']:>6.0%}{r['attempts']:>5}"
              f"{r['input_tokens']:>8}{r['output_tokens']:>8}{r['cost_usd']:>9.4f}{r['latency_ms']:>8}")
    print(f"{'AVG':<41}{avg['accuracy']:>6.0%}{'':>5}{avg['input_tokens']:>8.0f}{avg['output_tokens']:>8.0f}"
          f"{avg['cost_usd']:>9.4f}{avg['latency_ms']:>8.0f}")

    def verdict(ok: bool) -> str:
        return "CUMPLE" if ok else "NO CUMPLE"

    print(f"\nKPI PE1 -> exactitud {avg['accuracy']:.0%} (meta >= 95 %): {verdict(avg['accuracy'] >= KPI['accuracy'])}"
          f" | latencia {avg['latency_ms'] / 1000:.1f} s (meta < 30 s): {verdict(avg['latency_ms'] < KPI['latency_ms'])}"
          f" | costo USD {avg['cost_usd']:.4f} (meta < 0,05): {verdict(avg['cost_usd'] < KPI['cost_usd'])}")

    out = ROOT / "results" / f"run_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Resultados guardados en {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

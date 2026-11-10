"""Evaluación del RAG (PE3).

    python src/evaluate_rag.py topk                 # medición top-k: 2 fragmentados x (vectorial | híbrida), sin API
    python src/evaluate_rag.py demo                 # consultas donde la híbrida acierta y la vectorial falla, sin API
    python src/evaluate_rag.py answers [--model M]  # 10 preguntas + 2 accesos denegados contra la API -> CSV
    python src/evaluate_rag.py summary CSV [CSV..]  # promedios y KPI calculados SIEMPRE desde los CSV

Métricas top-k (sobre las 8 preguntas que tienen documento esperado; las 2 "sin respuesta" no tienen
documento correcto y se evalúan en `answers` por requires_human):
  - doc@k: el documento esperado aparece entre los primeros k fragmentos.
  - evidencia@k: algún fragmento del top-k contiene TODOS los textos de `evidence` (el dato y su
    contexto, por ejemplo la fila y el encabezado de la tabla). Es más exigente que doc@k.
  - MRR: promedio de 1 / posición del documento esperado.
  - fugas por rol: fragmentos de documentos solo para socios devueltos a un auxiliar (meta: 0).
  - fugas entre clientes: fragmentos del legajo de un cliente devueltos con OTRO cliente en contexto,
    buscando con el texto exacto de cada fragmento y con un socio (el rol con más permisos). Meta: 0.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import rag  # noqa: E402
from chunking import CHUNK_CONFIGS  # noqa: E402

QUESTIONS = ROOT / "data" / "rag_questions.json"
DEMO = ROOT / "data" / "hybrid_demo.json"
RESULTS = ROOT / "results"
KS = (1, 3, 5)
RESTRICTED_DOCS = {"client_fees", "client_file_norte_grande", "partner_meeting_minutes"}
CLIENT_DIRS = sorted(str(p.parent.relative_to(ROOT)) for p in (ROOT / "data" / "sample").glob("*/config.json"))
# KPI de la PE3 (se comparan contra lo medido; si no se llega, se informa NO CUMPLE)
KPI = {"doc_at_5": 0.90, "evidence_at_5": 0.85, "leaks": 0, "accuracy": 0.90, "latency_ms": 10_000, "cost_usd": 0.02}


def load_questions() -> list[dict]:
    return json.loads(QUESTIONS.read_text(encoding="utf-8"))


def client_of(client_dir: str) -> str:
    return json.loads((ROOT / client_dir / "config.json").read_text(encoding="utf-8"))["client_cuit"]


def cross_client_leaks(col, socio) -> int:
    """Busca con el texto exacto de cada fragmento de legajo, con cada uno de los OTROS clientes en contexto."""
    data = col.get(where={"client": {"$ne": "all"}}, include=["documents", "metadatas"])
    leaks = 0
    for text, meta in zip(data["documents"], data["metadatas"]):
        for other in [client_of(d) for d in CLIENT_DIRS if client_of(d) != meta["client"]] + [None]:
            hits = rag.search(text, socio, k=50, collection=col, client=other)
            leaks += sum(1 for h in hits if h.metadata.get("client") == meta["client"])
    return leaks


def _write_csv(name: str, rows: list[dict]) -> Path:
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{name}_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return out


def verdict(ok: bool) -> str:
    return "CUMPLE" if ok else "NO CUMPLE"


# ------------------------------------------------------------------ top-k --

def cmd_topk(_args) -> None:
    users = rag.load_users()
    questions = load_questions()
    answerable = [q for q in questions if q["expected_document"]]
    rows = []
    for config in CHUNK_CONFIGS:
        col = rag.get_collection(config)
        for mode in ("vector", "hybrid"):
            for q in answerable:
                hits = rag.search(q["query"], users[q["user"]], k=max(KS), collection=col, mode=mode,
                                  client=client_of(q["client_dir"]))
                docs = [h.document for h in hits]
                pos = docs.index(q["expected_document"]) + 1 if q["expected_document"] in docs else None
                ev_pos = next((i for i, h in enumerate(hits, 1) if all(e in h.text for e in q["evidence"])), None)
                row = {"config": config, "mode": mode, "question": q["id"], "doc_rank": pos or "", "evidence_rank": ev_pos or ""}
                rows.append(row)
            # fugas: las preguntas restringidas hechas por el auxiliar sin permiso
            for q in [q for q in questions if q.get("denied_user")]:
                hits = rag.search(q["query"], users[q["denied_user"]], k=50, collection=col, mode=mode,
                                  client=client_of(q["client_dir"]))
                rows.append({"config": config, "mode": mode, "question": f"{q['id']}-denied",
                             "doc_rank": "", "evidence_rank": "",
                             "leaks": sum(1 for h in hits if h.document in RESTRICTED_DOCS)})
        rows.append({"config": config, "mode": "hybrid", "question": "cross-client", "doc_rank": "",
                     "evidence_rank": "", "leaks": cross_client_leaks(col, users["nicolas"])})
    for r in rows:
        r.setdefault("leaks", "")
    out = _write_csv("rag_topk", rows)
    print(f"Resultados por pregunta guardados en {out.relative_to(ROOT)}\n")
    summarize_topk(out)


def summarize_topk(path: Path) -> dict:
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    print(f"{'fragmentado':<12}{'búsqueda':<9}" + "".join(f"{'doc@' + str(k):>8}" for k in KS)
          + "".join(f"{'evid@' + str(k):>9}" for k in KS) + f"{'MRR':>7}{'fugas*':>7}")
    table = {}
    for config in CHUNK_CONFIGS:
        for mode in ("vector", "hybrid"):
            sel = [r for r in rows if r["config"] == config and r["mode"] == mode and r["question"].startswith("Q")
                   and not r["question"].endswith("denied")]
            den = [r for r in rows if r["config"] == config and r["mode"] == mode and r["question"].endswith("denied")]
            den += [r for r in rows if r["config"] == config and r["mode"] == mode and r["question"] == "cross-client"]
            n = len(sel)
            doc = {k: sum(1 for r in sel if r["doc_rank"] and int(r["doc_rank"]) <= k) for k in KS}
            ev = {k: sum(1 for r in sel if r["evidence_rank"] and int(r["evidence_rank"]) <= k) for k in KS}
            mrr = sum(1 / int(r["doc_rank"]) for r in sel if r["doc_rank"]) / n
            leaks = sum(int(r["leaks"]) for r in den)
            table[(config, mode)] = {"n": n, "doc": doc, "ev": ev, "mrr": mrr, "leaks": leaks}
            print(f"{config:<12}{mode:<9}" + "".join(f"{doc[k]:>5}/{n}" for k in KS)
                  + "".join(f"{ev[k]:>6}/{n}" for k in KS) + f"{mrr:>7.2f}{leaks:>7}")
    print("* fugas = fragmentos solo-socios devueltos a un auxiliar + fragmentos de legajos devueltos con otro cliente"
          " en contexto (la prueba entre clientes se corre con búsqueda híbrida)")
    best = max(table, key=lambda c: (table[c]["ev"][5], table[c]["ev"][3], table[c]["ev"][1], table[c]["mrr"]))
    b = table[best]
    print(f"\nGanadora: fragmentado '{best[0]}' con búsqueda '{best[1]}' (criterio: evidencia@5, luego @3, @1 y MRR)")
    print(f"KPI PE3 -> doc@5 {b['doc'][5]}/{b['n']} (meta >= 90 %): {verdict(b['doc'][5] / b['n'] >= KPI['doc_at_5'])}"
          f" | evidencia@5 {b['ev'][5]}/{b['n']} (meta >= 85 %): {verdict(b['ev'][5] / b['n'] >= KPI['evidence_at_5'])}"
          f" | fugas {sum(t['leaks'] for t in table.values())} (meta 0): "
          f"{verdict(sum(t['leaks'] for t in table.values()) == 0)}")
    return table


# ------------------------------------------------------------------- demo --

def cmd_demo(_args) -> None:
    users = rag.load_users()
    demo = json.loads(DEMO.read_text(encoding="utf-8"))
    col = rag.get_collection(rag.DEFAULT_CONFIG)
    print(f"Fragmentado '{rag.DEFAULT_CONFIG}'. Posición del documento esperado (- = no aparece en el top-5)\n")
    for d in demo:
        print(f"Consulta: {d['query']!r} (usuario {d['user']}, cliente {client_of(d['client_dir'])}) -> esperado {d['expected_document']}  [{d['why']}]")
        for mode in ("vector", "hybrid"):
            hits = rag.search(d["query"], users[d["user"]], k=5, collection=col, mode=mode,
                              client=client_of(d["client_dir"]))
            docs = [h.document for h in hits]
            pos = docs.index(d["expected_document"]) + 1 if d["expected_document"] in docs else "-"
            top1 = "ACIERTA" if pos == 1 else "FALLA"
            top3 = "ACIERTA" if pos != "-" and pos <= 3 else "FALLA"
            print(f"  {mode:<7} top-1 {top1:<8} top-3 {top3:<8} posición {pos}  top-3: {', '.join(docs[:3])}")
        print()


# ---------------------------------------------------------------- answers --

def _passes(q: dict, result, denied: bool) -> tuple[bool, str]:
    if not result.ok:
        return False, f"error:{result.error.kind}"
    out = result.output
    if denied or q["expected_requires_human"]:
        leaked = [c for c in result.retrieved if c.split("::")[0] in RESTRICTED_DOCS] if denied else []
        if leaked:
            return False, f"fuga {leaked}"
        return out.requires_human and not out.sources, "requires_human" if out.requires_human else "respondió sin evidencia"
    if out.requires_human:
        return False, "pidió humano teniendo evidencia"
    cited = {s.document for s in out.sources}
    if q["expected_document"] not in cited:
        return False, f"no cita {q['expected_document']} (cita {sorted(cited)})"
    missing = [m for m in q["must_mention"] if m not in out.answer]
    return (not missing), ("ok" if not missing else f"falta {missing}")


def cmd_answers(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    users = rag.load_users()
    model = args.model or rag.MODEL
    runs = []
    for q in load_questions():
        runs.append((q, q["user"], False))
        if q.get("denied_user"):
            runs.append((q, q["denied_user"], True))
    rows = []
    for q, uid, denied in runs:
        user = users[uid]
        result = rag.answer(rag.entrada_for(q["query"], ROOT / q["client_dir"]), user, model=model)
        passed, reason = _passes(q, result, denied)
        m = result.metrics
        rows.append({
            "question": q["id"] + ("-denied" if denied else ""), "kind": q["kind"], "user": uid, "role": user.role.value,
            "client": client_of(q["client_dir"]),
            "model": model, "requires_human": result.output.requires_human if result.ok else "",
            "sources": ";".join(sorted({s.document for s in result.output.sources})) if result.ok else "",
            "passed": int(passed), "reason": reason, "attempts": m.attempts,
            "input_tokens": m.input_tokens, "output_tokens": m.output_tokens,
            "cost_usd": m.cost_usd, "latency_ms": m.latency_ms,
            "answer": (result.output.answer if result.ok else result.error.message).replace("\n", " "),
        })
        print(f"{rows[-1]['question']:<11}{uid:<9}{'PASA' if passed else 'FALLA':<6} rh={rows[-1]['requires_human']!s:<6}"
              f" {m.latency_ms:>6} ms  USD {m.cost_usd:.4f}  {reason}")
    out = _write_csv(f"rag_answers_{model}", rows)
    print(f"\nResultados guardados en {out.relative_to(ROOT)}")
    summarize_answers([out])


def summarize_answers(paths: list[Path]) -> None:
    """Promedios calculados desde el CSV (no a mano). Latencia en milisegundos (ms) y en segundos."""
    for path in paths:
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        n = len(rows)
        acc = sum(int(r["passed"]) for r in rows) / n
        lat = sum(int(r["latency_ms"]) for r in rows) / n
        cost = sum(float(r["cost_usd"]) for r in rows) / n
        tin = sum(int(r["input_tokens"]) for r in rows) / n
        tout = sum(int(r["output_tokens"]) for r in rows) / n
        print(f"\n{Path(path).name}: modelo {rows[0]['model']}, {n} consultas")
        print(f"  aciertos {sum(int(r['passed']) for r in rows)}/{n} ({acc:.0%}) | latencia promedio {lat:.0f} ms "
              f"({lat / 1000:.1f} s) | costo promedio USD {cost:.4f} | tokens promedio {tin:.0f} in / {tout:.0f} out")
        for kind in ("normal", "no_answer", "restricted"):
            sel = [r for r in rows if r["kind"] == kind]
            print(f"    {kind:<11} {sum(int(r['passed']) for r in sel)}/{len(sel)}")
        print(f"  KPI PE3 -> exactitud {acc:.0%} (meta >= 90 %): {verdict(acc >= KPI['accuracy'])}"
              f" | latencia {lat / 1000:.1f} s (meta < 10 s): {verdict(lat < KPI['latency_ms'])}"
              f" | costo USD {cost:.4f} (meta < 0,02): {verdict(cost < KPI['cost_usd'])}")


def cmd_summary(args) -> None:
    for p in args.csv:
        p = Path(p)
        if p.name.startswith("rag_topk"):
            summarize_topk(p)
        else:
            summarize_answers([p])


def main() -> None:
    p = argparse.ArgumentParser(description="Evaluación del RAG")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("topk")
    sub.add_parser("demo")
    a = sub.add_parser("answers")
    a.add_argument("--model", default=None)
    s = sub.add_parser("summary")
    s.add_argument("csv", nargs="+")
    args = p.parse_args()
    {"topk": cmd_topk, "demo": cmd_demo, "answers": cmd_answers, "summary": cmd_summary}[args.cmd](args)


if __name__ == "__main__":
    main()

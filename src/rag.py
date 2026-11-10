"""Módulo RAG con permisos y búsqueda híbrida (PE3).

Piezas (ver equivalencias con la consigna en rag_contract.py):
  ingest(folder, config)          -> lee knowledge_base/, limpia, fragmenta y guarda en ChromaDB
  search(query, user, k, ...)     -> búsqueda vectorial + BM25 fusionadas con RRF, filtrando por rol
                                     DENTRO de la consulta a ChromaDB (antes de recuperar)
  answer(entrada, user, ...)      -> recibe la Entrada de la PE2 (EngineInput) y devuelve la Salida
                                     (RagOutput) con `sources`, o requires_human=true si no hay evidencia

Uso:
    python src/rag.py ingest                       # procesa los documentos con las dos configuraciones
    python src/rag.py search --user lucia "alícuota 477461"
    python src/rag.py ask --user nicolas "¿Qué honorario paga Ferretería Los Lapachos?"
    python src/rag.py show-chunk "iibb_tucuman_rates::section::001"
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from string import Template
from typing import Literal, Optional

import anthropic
import chromadb
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from rank_bm25 import BM25Okapi

sys.path.insert(0, str(Path(__file__).parent))
from chunking import CHUNK_CONFIGS, chunk_document, load_documents  # noqa: E402
from contract import EngineInput  # noqa: E402
from motor import MODELS_WITH_FALLBACK, PRICES_USD_PER_MTOK, EngineError, Metrics  # noqa: E402
from rag_contract import RagModelReply, RagOutput, SearchHit, Source, User  # noqa: E402

load_dotenv()
log = logging.getLogger("rag")

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "knowledge_base"
CHROMA_DIR = Path(os.getenv("CHROMA_DIR", ROOT / ".chroma"))
USERS_FILE = ROOT / "data" / "users.json"
PROMPTS_DIR = Path(__file__).parent / "prompts"
DEFAULT_CONFIG = "section"      # la configuración ganadora de la medición top-k (ver evaluate_rag.py)
DEFAULT_K = 5
CANDIDATES = 10                 # candidatos que trae cada buscador antes de fusionar
BM25_MIN_RATIO = 0.3            # BM25 solo aporta fragmentos con >= 30 % del mejor puntaje (evita ruido
                                # de palabras comunes como "estudio" o "cliente")
RRF_K = 60                      # constante de Reciprocal Rank Fusion (valor estándar)
MODEL = os.getenv("MODEL", "claude-opus-5-5")
EFFORT = os.getenv("EFFORT", "low")
MAX_TOKENS = 16000
MAX_ATTEMPTS = 2

Mode = Literal["hybrid", "vector", "bm25"]


# ------------------------------------------------------------------ users --

def load_users() -> dict[str, User]:
    return {u["id"]: User.model_validate(u) for u in json.loads(USERS_FILE.read_text(encoding="utf-8"))}


def role_filter(user: User) -> dict:
    """Filtro de permisos que se pasa a ChromaDB en el `where` de cada consulta."""
    return {f"role_{user.role.value}": True}


# ----------------------------------------------------------------- ingest --

def collection_name(config: str) -> str:
    return f"kb_{config}"


def get_client(path: Path | str | None = None):
    return chromadb.PersistentClient(path=str(path or CHROMA_DIR))


def ingest(folder: Path | str = KB_DIR, config: str = DEFAULT_CONFIG, client=None):
    """Lee la carpeta, limpia y fragmenta cada documento y lo guarda en ChromaDB (colección kb_<config>).

    Se recrea la colección en cada corrida para que la base refleje exactamente la carpeta.
    Devuelve la colección.
    """
    client = client or get_client()
    name = collection_name(config)
    if name in [c.name for c in client.list_collections()]:
        client.delete_collection(name)
    collection = client.create_collection(name, configuration={"hnsw": {"space": "cosine"}})
    docs = load_documents(Path(folder))
    chunks = [c for d in docs for c in chunk_document(d, config)]
    collection.add(
        ids=[c.chunk_id for c in chunks],
        documents=[c.text for c in chunks],
        metadatas=[c.metadata for c in chunks],
    )
    collection._ingest_report = [  # para que la CLI pueda imprimir el resumen
        (d.doc_id, d.roles, d.version, d.effective_date, sum(1 for c in chunks if c.document == d.doc_id))
        for d in docs
    ]
    return collection


def get_collection(config: str = DEFAULT_CONFIG, client=None):
    client = client or get_client()
    return client.get_collection(collection_name(config))


# ----------------------------------------------------------------- search --

STOPWORDS = set(
    "a al algo con cual cuál cuando cuándo de del desde el en es esta este hay la las le lo los me mi "
    "para por que qué se si su sus tiene un una y o como cómo cuanto cuánto ante".split()
)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def tokenize(text: str) -> list[str]:
    """Tokens para BM25: minúsculas sin acentos; conserva códigos como 477461, f.2002, cm05 o p-884512."""
    tokens = re.findall(r"[a-z0-9]+(?:[.\-/][a-z0-9]+)*", _normalize(text))
    return [t for t in tokens if t not in STOPWORDS]


def _vector_ranking(collection, query: str, where: dict, n: int) -> list[tuple[str, str, dict, float]]:
    res = collection.query(query_texts=[query], n_results=n, where=where,
                           include=["documents", "metadatas", "distances"])
    return [
        (cid, doc, meta, 1.0 - dist)  # similitud coseno
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])
    ]


def _bm25_ranking(collection, query: str, where: dict, n: int) -> list[tuple[str, str, dict, float]]:
    # El corpus de BM25 también sale de ChromaDB con el MISMO filtro de rol: un documento restringido
    # nunca entra al índice de palabras clave de un usuario sin permiso.
    pool = collection.get(where=where, include=["documents", "metadatas"])
    if not pool["ids"]:
        return []
    bm25 = BM25Okapi([tokenize(t) for t in pool["documents"]])
    scores = bm25.get_scores(tokenize(query))
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    best = scores[order[0]] if order else 0.0
    return [
        (pool["ids"][i], pool["documents"][i], pool["metadatas"][i], float(scores[i]))
        for i in order[:n] if scores[i] > 0 and scores[i] >= BM25_MIN_RATIO * best
    ]


def search(query: str, user: User, k: int = DEFAULT_K, collection=None, mode: Mode = "hybrid",
           config: str = DEFAULT_CONFIG) -> list[SearchHit]:
    """Devuelve los k mejores fragmentos que el usuario puede leer.

    hybrid: fusiona el ranking vectorial y el de BM25 con Reciprocal Rank Fusion:
        puntaje = sum(1 / (RRF_K + posición)) sobre los rankings donde aparece el fragmento.
    """
    collection = collection or get_collection(config)
    where = role_filter(user)
    vector = _vector_ranking(collection, query, where, CANDIDATES) if mode in ("hybrid", "vector") else []
    keyword = _bm25_ranking(collection, query, where, CANDIDATES) if mode in ("hybrid", "bm25") else []

    entries: dict[str, dict] = {}
    for name, ranking in (("vector", vector), ("bm25", keyword)):
        for rank, (cid, text, meta, raw) in enumerate(ranking, start=1):
            e = entries.setdefault(cid, {"text": text, "meta": meta, "rrf": 0.0, "vector": None, "bm25": None,
                                         "raw": raw})
            e[name] = rank
            e["rrf"] += 1.0 / (RRF_K + rank)

    if mode == "hybrid":
        key = lambda cid: entries[cid]["rrf"]  # noqa: E731
    else:
        key = lambda cid: entries[cid]["raw"]  # noqa: E731
    ranked = sorted(entries, key=key, reverse=True)[:k]
    return [
        SearchHit(
            document=entries[cid]["meta"]["document"], chunk_id=cid, text=entries[cid]["text"],
            score=round(key(cid), 6), section=entries[cid]["meta"].get("section", ""),
            title=entries[cid]["meta"].get("title", ""), vector_rank=entries[cid]["vector"],
            bm25_rank=entries[cid]["bm25"], metadata=entries[cid]["meta"],
        )
        for cid in ranked
    ]


# ----------------------------------------------------------------- answer --

class RagResult(BaseModel):
    ok: bool
    output: Optional[RagOutput] = None
    error: Optional[EngineError] = None
    metrics: Metrics
    retrieved: list[str] = []   # chunk_id de lo que se mandó al modelo, en orden


def format_fragments(hits: list[SearchHit]) -> str:
    return "\n".join(
        f'<fragment id="{h.chunk_id}" document="{h.document}" version="{h.metadata.get("version", "")}" '
        f'effective_date="{h.metadata.get("effective_date", "")}">\n{h.text}\n</fragment>'
        for h in hits
    )


def build_prompts(e: EngineInput, user: User, hits: list[SearchHit]) -> tuple[str, str]:
    schema = json.dumps(RagModelReply.model_json_schema(), ensure_ascii=False)
    system = Template((PROMPTS_DIR / "rag_system.txt").read_text(encoding="utf-8")).substitute(schema=schema)
    user_prompt = Template((PROMPTS_DIR / "rag_user.txt").read_text(encoding="utf-8")).substitute(
        query=e.query, role=user.role.value, client_cuit=e.client_cuit, regime=e.regime.value,
        period=e.period, fragments=format_fragments(hits),
    )
    return system, user_prompt


def _parse_reply(text: str) -> RagModelReply:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("la respuesta no contiene un objeto JSON")
    return RagModelReply.model_validate_json(text[start : end + 1])


def to_output(reply: RagModelReply, hits: list[SearchHit], period: str) -> RagOutput:
    """Arma la Salida. Las fuentes salen de los fragmentos recuperados: el modelo no puede citar
    un documento que no recibió (si lo intenta, falla la validación y se reintenta)."""
    by_id = {h.chunk_id: h for h in hits}
    unknown = [s for s in reply.source_ids if s not in by_id]
    if unknown:
        raise ValueError(f"source_ids no recuperados: {unknown}")
    sources = [
        Source(document=by_id[s].document, chunk_id=s, title=by_id[s].title, section=by_id[s].section,
               version=str(by_id[s].metadata.get("version", "")),
               effective_date=str(by_id[s].metadata.get("effective_date", "")))
        for s in dict.fromkeys(reply.source_ids)
    ]
    return RagOutput(
        status=reply.status, period=period, answer=reply.answer, explanation=reply.explanation,
        confidence=reply.confidence, requires_human=reply.requires_human,
        sources=[] if reply.requires_human else sources,
    )


def _failed_fields(error: Exception) -> list[str]:
    if isinstance(error, ValidationError):
        return [".".join(str(p) for p in err["loc"]) or "(model)" for err in error.errors()]
    return ["(json)" if "JSON" in str(error) else "(sources)" if "source_ids" in str(error) else "(model)"]


def _call_model(client, model: str, system: str, messages: list):
    params = dict(model=model, max_tokens=MAX_TOKENS, system=system, messages=messages,
                  output_config={"effort": EFFORT})
    if model in MODELS_WITH_FALLBACK:
        return client.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    return client.messages.create(**params)


def _add_usage(m: Metrics, response, model: str) -> None:
    u = response.usage
    m.input_tokens += u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
    m.output_tokens += u.output_tokens
    price_in, price_out = PRICES_USD_PER_MTOK.get(model, (0.0, 0.0))
    m.cost_usd = round(m.input_tokens * price_in / 1e6 + m.output_tokens * price_out / 1e6, 6)
    if getattr(response, "_request_id", None):
        m.request_ids.append(response._request_id)


def answer(entrada: EngineInput | dict, user: User, k: int = DEFAULT_K, collection=None, client=None,
           model: Optional[str] = None, config: str = DEFAULT_CONFIG) -> RagResult:
    """Responde la consulta de la Entrada (PE2) solo con fragmentos que el usuario puede leer."""
    model = model or MODEL
    metrics = Metrics(model=model)
    start = time.perf_counter()

    def finish(**kw) -> RagResult:
        metrics.latency_ms = round((time.perf_counter() - start) * 1000)
        return RagResult(metrics=metrics, **kw)

    try:
        e = entrada if isinstance(entrada, EngineInput) else EngineInput.model_validate(entrada)
    except ValidationError as err:
        fields = _failed_fields(err)
        log.error("Entrada inválida, no se busca ni se llama a la API. Campos: %s", fields)
        return finish(ok=False, error=EngineError(kind="input_validation", message=str(err.errors()[0]["msg"]),
                                                  fields=fields))

    hits = search(e.query, user, k=k, collection=collection, config=config)
    retrieved = [h.chunk_id for h in hits]
    if not hits:  # sin evidencia ni siquiera se llama al modelo
        out = RagOutput(status="needs_clarification", period=e.period, requires_human=True,
                        answer="No hay documentos disponibles para su rol que respondan la consulta.",
                        explanation="La búsqueda no devolvió fragmentos permitidos para este rol.", confidence=0.0)
        return finish(ok=True, output=out, retrieved=retrieved)

    system, user_prompt = build_prompts(e, user, hits)
    messages: list = [{"role": "user", "content": user_prompt}]
    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            return finish(ok=False, error=EngineError(kind="api", message="Falta la variable ANTHROPIC_API_KEY"),
                          retrieved=retrieved)
        client = anthropic.Anthropic(max_retries=2, timeout=60.0)

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        metrics.attempts = attempt
        try:
            response = _call_model(client, model, system, messages)
        except anthropic.AuthenticationError:
            return finish(ok=False, error=EngineError(kind="api", message="API key inválida"), retrieved=retrieved)
        except anthropic.NotFoundError as err:
            return finish(ok=False, error=EngineError(kind="api", message=f"Modelo inexistente: {err.message}"),
                          retrieved=retrieved)
        except anthropic.RateLimitError:
            return finish(ok=False, error=EngineError(kind="api", message="Límite de uso (429)"), retrieved=retrieved)
        except anthropic.APIStatusError as err:
            return finish(ok=False, error=EngineError(kind="api", message=f"HTTP {err.status_code}: {err.message}"),
                          retrieved=retrieved)
        except anthropic.APIConnectionError:
            return finish(ok=False, error=EngineError(kind="api", message="Sin conexión con la API"),
                          retrieved=retrieved)

        _add_usage(metrics, response, model)
        if response.stop_reason == "refusal":
            return finish(ok=False, error=EngineError(kind="refusal", message="El modelo rechazó la consulta"),
                          retrieved=retrieved)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        try:
            return finish(ok=True, output=to_output(_parse_reply(text), hits, e.period), retrieved=retrieved)
        except ValueError as err:
            last_error = err
            log.warning("Intento %d: salida inválida. Campos que fallaron: %s (%s)", attempt, _failed_fields(err), err)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user",
                             "content": f"Tu respuesta no pasó la validación ({err}). Devolvé SOLO el JSON corregido."})

    return finish(ok=False, retrieved=retrieved, error=EngineError(
        kind="output_validation", message=f"La salida no validó tras {MAX_ATTEMPTS} intentos: {last_error}",
        fields=_failed_fields(last_error) if last_error else []))


def entrada_for(query: str, client_dir: Path | str = ROOT / "data/sample/client_ri", period: str = "2026-10") -> dict:
    """Arma una Entrada de la PE2 para una consulta de conocimiento (sin comprobantes)."""
    cfg = json.loads((Path(client_dir) / "config.json").read_text(encoding="utf-8"))
    cfg.pop("name", None)
    return cfg | {"query": query, "period": period, "issued": [], "received": []}


# -------------------------------------------------------------------- CLI --

def _cmd_ingest(args) -> None:
    client = get_client()
    for config in (args.config or list(CHUNK_CONFIGS)):
        t0 = time.perf_counter()
        col = ingest(args.folder, config, client=client)
        ms = round((time.perf_counter() - t0) * 1000)
        print(f"\n== Configuración '{config}' {CHUNK_CONFIGS[config]} -> colección {col.name}")
        print(f"{'documento':<32}{'roles':<18}{'versión':>8}  {'vigencia':<12}{'fragmentos':>11}")
        for doc_id, roles, version, eff, n in col._ingest_report:
            print(f"{doc_id:<32}{','.join(roles):<18}{version:>8}  {eff:<12}{n:>11}")
        restricted = sum(1 for r in col._ingest_report if "auxiliar" not in r[1])
        print(f"Total: {len(col._ingest_report)} documentos ({restricted} solo socios), {col.count()} fragmentos "
              f"en ChromaDB ({CHROMA_DIR.relative_to(ROOT) if CHROMA_DIR.is_relative_to(ROOT) else CHROMA_DIR}), {ms} ms")


def _cmd_show(args) -> None:
    config = args.chunk_id.split("::")[1]
    res = get_collection(config).get(ids=[args.chunk_id], include=["documents", "metadatas"])
    print(json.dumps({"chunk_id": res["ids"][0], "metadata": res["metadatas"][0], "text": res["documents"][0]},
                     ensure_ascii=False, indent=2))


def _cmd_search(args) -> None:
    user = load_users()[args.user]
    for mode in (["vector", "hybrid"] if args.compare else [args.mode]):
        print(f"\n[{mode}] usuario={user.id} rol={user.role.value} consulta={args.query!r}")
        for i, h in enumerate(search(args.query, user, k=args.k, mode=mode, config=args.config), start=1):
            print(f"  {i}. {h.chunk_id:<44} score={h.score:<9} vec#={h.vector_rank} bm25#={h.bm25_rank}  [{h.section}]")


def _cmd_ask(args) -> None:
    user = load_users()[args.user]
    result = answer(entrada_for(args.query), user, k=args.k, model=args.model, config=args.config)
    print(result.model_dump_json(indent=2, exclude_none=True))
    m = result.metrics
    rh = result.output.requires_human if result.ok else None
    print(f"\n[{'OK' if result.ok else 'ERROR'}] user={user.id} role={user.role.value} requires_human={rh} "
          f"model={m.model} attempts={m.attempts} tokens={m.input_tokens}+{m.output_tokens} "
          f"cost=USD {m.cost_usd:.4f} latency={m.latency_ms} ms", file=sys.stderr)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description="RAG con permisos del estudio contable")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="procesa knowledge_base/ y lo guarda en ChromaDB")
    i.add_argument("--folder", default=str(KB_DIR))
    i.add_argument("--config", action="append", choices=list(CHUNK_CONFIGS))
    s = sub.add_parser("show-chunk", help="muestra los metadatos de un fragmento")
    s.add_argument("chunk_id")
    for name in ("search", "ask"):
        q = sub.add_parser(name)
        q.add_argument("query")
        q.add_argument("--user", required=True)
        q.add_argument("--k", type=int, default=DEFAULT_K)
        q.add_argument("--config", default=DEFAULT_CONFIG, choices=list(CHUNK_CONFIGS))
        if name == "search":
            q.add_argument("--mode", default="hybrid", choices=["hybrid", "vector", "bm25"])
            q.add_argument("--compare", action="store_true", help="muestra vector y híbrida lado a lado")
        else:
            q.add_argument("--model", default=None)
    args = p.parse_args()
    {"ingest": _cmd_ingest, "show-chunk": _cmd_show, "search": _cmd_search, "ask": _cmd_ask}[args.cmd](args)


if __name__ == "__main__":
    main()

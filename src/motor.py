"""Motor LLM base del estudio contable (PE2).

Recibe un EngineInput validado (consulta + comprobantes ARCA de un cliente),
arma el prompt desde las plantillas de src/prompts/, llama a la API de
Anthropic, valida la respuesta contra EngineOutput y mide tokens, costo (USD)
y latencia (ms). Si la salida no valida, registra qué campo falló, reintenta
una vez y, si vuelve a fallar, devuelve un error estructurado.

El nombre del archivo (src/motor.py) lo fija la consigna del PE2.

Uso:
    python src/motor.py --client data/sample/client_ri --period 2026-10 \
        --query "Proyectame lo que paga en octubre"
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from string import Template
from typing import Literal, Optional

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

sys.path.insert(0, str(Path(__file__).parent))
from arca_csv import load_client  # noqa: E402
from contract import EngineInput, EngineOutput, Voucher  # noqa: E402
from reference import last_12_months  # noqa: E402

load_dotenv()
log = logging.getLogger("motor")

PROMPTS_DIR = Path(__file__).parent / "prompts"
MODEL = os.getenv("MODEL", "claude-opus-5-5")
EFFORT = os.getenv("EFFORT", "low")
MAX_TOKENS = 16000
MAX_ATTEMPTS = 2  # el intento original + 1 reintento

# Precios publicados por Anthropic, USD por millón de tokens (entrada, salida).
PRICES_USD_PER_MTOK = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
# Modelos que aceptan el fallback del lado del servidor ante un rechazo.
MODELS_WITH_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5"}


class EngineError(BaseModel):
    kind: Literal["input_validation", "output_validation", "api", "refusal"]
    message: str
    fields: list[str] = []


class Metrics(BaseModel):
    model: str
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    request_ids: list[str] = []


class EngineResult(BaseModel):
    ok: bool
    output: Optional[EngineOutput] = None
    error: Optional[EngineError] = None
    metrics: Metrics


# ---------------------------------------------------------------- prompts --

def _row(v: Voucher) -> str:
    return (
        f"{v.date} | {v.type} | {v.point_of_sale}-{v.number} | {v.counterparty} | {v.currency.value} | "
        f"{v.exchange_rate:.2f} | {v.taxed_net:.2f} | {v.untaxed:.2f} | {v.exempt:.2f} | {v.vat:.2f} | {v.total:.2f}"
    )


def build_prompts(e: EngineInput) -> tuple[str, str]:
    schema = json.dumps(EngineOutput.model_json_schema(), ensure_ascii=False)
    system = Template((PROMPTS_DIR / "system.txt").read_text(encoding="utf-8")).substitute(schema=schema)
    window = last_12_months(e.period)
    issued = [_row(v) for v in e.issued if v.period in window]
    received = [_row(v) for v in e.received if v.period == e.period]
    user = Template((PROMPTS_DIR / "user.txt").read_text(encoding="utf-8")).substitute(
        query=e.query,
        client_cuit=e.client_cuit,
        regime=e.regime.value,
        period=e.period,
        iibb_rate=e.iibb_rate,
        tem_rate=e.tem_rate,
        monotributo_fee=e.monotributo_fee if e.monotributo_fee is not None else "no aplica",
        category_cap=e.category_cap if e.category_cap is not None else "no informado",
        issued="\n".join(issued) or "(sin comprobantes)",
        received="\n".join(received) or "(sin comprobantes)",
    )
    return system, user


# ------------------------------------------------------------- validation --

def _text(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()


def validate_output(text: str) -> EngineOutput:
    """Extrae el JSON del texto y lo valida contra EngineOutput (lanza si falla)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("la respuesta no contiene un objeto JSON")
    return EngineOutput.model_validate_json(text[start : end + 1])


def _failed_fields(error: Exception) -> list[str]:
    if isinstance(error, ValidationError):
        return [".".join(str(p) for p in err["loc"]) or "(model)" for err in error.errors()]
    return ["(json)"]


# ----------------------------------------------------------------- engine --

def _call_model(client: anthropic.Anthropic, system: str, messages: list):
    params = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
        output_config={"effort": EFFORT},
    )
    if MODEL in MODELS_WITH_FALLBACK:
        return client.beta.messages.create(
            **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )
    return client.messages.create(**params)


def _add_usage(m: Metrics, response) -> None:
    u = response.usage
    m.input_tokens += u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
    m.output_tokens += u.output_tokens
    price_in, price_out = PRICES_USD_PER_MTOK.get(MODEL, (0.0, 0.0))
    m.cost_usd = round(m.input_tokens * price_in / 1_000_000 + m.output_tokens * price_out / 1_000_000, 6)
    if getattr(response, "_request_id", None):
        m.request_ids.append(response._request_id)


def run(data: dict, client: Optional[anthropic.Anthropic] = None) -> EngineResult:
    metrics = Metrics(model=MODEL)
    start = time.perf_counter()

    def finish(**kw) -> EngineResult:
        metrics.latency_ms = round((time.perf_counter() - start) * 1000)
        return EngineResult(metrics=metrics, **kw)

    try:
        engine_input = EngineInput.model_validate(data)
    except ValidationError as e:
        fields = _failed_fields(e)
        log.error("Entrada inválida, no se llama a la API. Campos: %s", fields)
        return finish(ok=False, error=EngineError(kind="input_validation", message=str(e.errors()[0]["msg"]), fields=fields))

    system, user = build_prompts(engine_input)
    messages: list = [{"role": "user", "content": user}]
    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            log.error("Falta ANTHROPIC_API_KEY (copiá .env.example a .env y completala)")
            return finish(ok=False, error=EngineError(kind="api", message="Falta la variable ANTHROPIC_API_KEY"))
        client = anthropic.Anthropic(max_retries=2, timeout=60.0)
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        metrics.attempts = attempt
        try:
            response = _call_model(client, system, messages)
        except anthropic.AuthenticationError:
            return finish(ok=False, error=EngineError(kind="api", message="API key inválida (ANTHROPIC_API_KEY)"))
        except anthropic.NotFoundError as e:
            return finish(ok=False, error=EngineError(kind="api", message=f"Modelo o endpoint inexistente: {e.message}"))
        except anthropic.RateLimitError:
            return finish(ok=False, error=EngineError(kind="api", message="Límite de uso de la API (429) tras los reintentos del SDK"))
        except anthropic.APIStatusError as e:
            return finish(ok=False, error=EngineError(kind="api", message=f"Error HTTP {e.status_code}: {e.message}"))
        except anthropic.APIConnectionError:
            return finish(ok=False, error=EngineError(kind="api", message="Sin conexión con la API de Anthropic"))

        _add_usage(metrics, response)

        if response.stop_reason == "refusal":
            return finish(ok=False, error=EngineError(kind="refusal", message="El modelo rechazó la consulta"))

        text = _text(response)
        try:
            output = validate_output(text)
            if output.period != engine_input.period:
                raise ValueError(f"period {output.period} distinto del pedido {engine_input.period}")
            return finish(ok=True, output=output)
        except ValueError as e:  # ValidationError de Pydantic también es ValueError
            last_error = e
            fields = _failed_fields(e)
            log.warning("Intento %d: salida inválida. Campos que fallaron: %s", attempt, fields)
            # Se agrega la respuesta completa (append-only) y se pide la corrección.
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": f"Tu respuesta no pasó la validación ({e}). Devolvé SOLO el JSON corregido.",
            })

    return finish(
        ok=False,
        error=EngineError(
            kind="output_validation",
            message=f"La salida no validó tras {MAX_ATTEMPTS} intentos: {last_error}",
            fields=_failed_fields(last_error) if last_error else [],
        ),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Motor LLM de proyección de DDJJ")
    parser.add_argument("--client", required=True, help="carpeta con config.json, issued.csv y received.csv")
    parser.add_argument("--period", required=True, help="AAAA-MM")
    parser.add_argument("--query", required=True, help="consulta del contador")
    args = parser.parse_args()

    data = load_client(Path(args.client))
    data.update(period=args.period, query=args.query)
    result = run(data)
    print(result.model_dump_json(indent=2, exclude_none=True))
    m = result.metrics
    print(
        f"\n[{'OK' if result.ok else 'ERROR'}] model={m.model} attempts={m.attempts} "
        f"tokens={m.input_tokens}+{m.output_tokens} cost=USD {m.cost_usd:.4f} latency={m.latency_ms} ms",
        file=sys.stderr,
    )
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()

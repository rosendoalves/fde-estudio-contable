"""Motor LLM base del estudio contable (PE2).

Recibe una Entrada validada (consulta + comprobantes ARCA de un cliente), arma
el prompt desde las plantillas de src/prompts/, llama a la API de Anthropic,
valida la respuesta contra Salida y mide tokens, costo (USD) y latencia (ms).
Si la salida no valida, registra qué campo falló, reintenta una vez y, si
vuelve a fallar, devuelve un error estructurado.

Uso:
    python src/motor.py --cliente datos/ejemplo/cliente_ri --periodo 2026-10 \
        --consulta "Proyectame lo que paga en octubre"
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
from arca_csv import cargar_cliente  # noqa: E402
from contrato import Comprobante, Entrada, Salida  # noqa: E402

load_dotenv()
log = logging.getLogger("motor")

CARPETA_PROMPTS = Path(__file__).parent / "prompts"
MODELO = os.getenv("MODELO", "claude-opus-5-5")
EFFORT = os.getenv("EFFORT", "low")
MAX_TOKENS = 16000
INTENTOS_MAXIMOS = 2  # el original + 1 reintento

# Precios publicados por Anthropic, USD por millón de tokens (entrada, salida).
PRECIOS_USD_POR_MTOK = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
# Modelos que aceptan el fallback del lado del servidor ante un rechazo.
MODELOS_CON_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5"}


class ErrorMotor(BaseModel):
    tipo: Literal["validacion_entrada", "validacion_salida", "api", "rechazo"]
    mensaje: str
    campos: list[str] = []


class Metricas(BaseModel):
    modelo: str
    intentos: int = 0
    tokens_entrada: int = 0
    tokens_salida: int = 0
    costo_usd: float = 0.0
    latencia_ms: int = 0
    request_ids: list[str] = []


class Resultado(BaseModel):
    ok: bool
    salida: Optional[Salida] = None
    error: Optional[ErrorMotor] = None
    metricas: Metricas


# ---------------------------------------------------------------- prompts --

def _fila(c: Comprobante) -> str:
    return (
        f"{c.fecha} | {c.tipo} | {c.punto_venta}-{c.numero} | {c.contraparte} | {c.moneda.value} | "
        f"{c.tipo_cambio:.2f} | {c.neto_gravado:.2f} | {c.no_gravado:.2f} | {c.exento:.2f} | {c.iva:.2f} | {c.total:.2f}"
    )


def _ultimos_12_meses(periodo: str) -> set[str]:
    anio, mes = map(int, periodo.split("-"))
    meses = set()
    for _ in range(12):
        meses.add(f"{anio:04d}-{mes:02d}")
        anio, mes = (anio - 1, 12) if mes == 1 else (anio, mes - 1)
    return meses


def armar_prompts(e: Entrada) -> tuple[str, str]:
    esquema = json.dumps(Salida.model_json_schema(), ensure_ascii=False)
    sistema = Template((CARPETA_PROMPTS / "sistema.txt").read_text(encoding="utf-8")).substitute(esquema=esquema)
    ventana = _ultimos_12_meses(e.periodo)
    emitidos = [_fila(c) for c in e.comprobantes_emitidos if c.fecha.strftime("%Y-%m") in ventana]
    recibidos = [_fila(c) for c in e.comprobantes_recibidos if c.fecha.strftime("%Y-%m") == e.periodo]
    usuario = Template((CARPETA_PROMPTS / "usuario.txt").read_text(encoding="utf-8")).substitute(
        consulta=e.consulta,
        cuit_cliente=e.cuit_cliente,
        regimen=e.regimen.value,
        periodo=e.periodo,
        alicuota_iibb=e.alicuota_iibb,
        alicuota_tem=e.alicuota_tem,
        cuota_monotributo=e.cuota_monotributo if e.cuota_monotributo is not None else "no aplica",
        tope_categoria=e.tope_categoria if e.tope_categoria is not None else "no informado",
        emitidos="\n".join(emitidos) or "(sin comprobantes)",
        recibidos="\n".join(recibidos) or "(sin comprobantes)",
    )
    return sistema, usuario


# ------------------------------------------------------------ validación --

def _texto(respuesta) -> str:
    return "".join(b.text for b in respuesta.content if b.type == "text").strip()


def validar_salida(texto: str) -> Salida:
    """Extrae el JSON del texto y lo valida contra Salida (lanza si falla)."""
    texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto.strip())
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fin == -1:
        raise ValueError("la respuesta no contiene un objeto JSON")
    return Salida.model_validate_json(texto[inicio : fin + 1])


def _campos_fallidos(error: Exception) -> list[str]:
    if isinstance(error, ValidationError):
        return [".".join(str(p) for p in err["loc"]) or "(modelo)" for err in error.errors()]
    return ["(json)"]


# ------------------------------------------------------------------ motor --

def _llamar(cliente: anthropic.Anthropic, sistema: str, mensajes: list) -> object:
    parametros = dict(
        model=MODELO,
        max_tokens=MAX_TOKENS,
        system=sistema,
        messages=mensajes,
        output_config={"effort": EFFORT},
    )
    if MODELO in MODELOS_CON_FALLBACK:
        return cliente.beta.messages.create(
            **parametros, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )
    return cliente.messages.create(**parametros)


def _sumar_uso(m: Metricas, respuesta) -> None:
    u = respuesta.usage
    entrada = u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
    m.tokens_entrada += entrada
    m.tokens_salida += u.output_tokens
    precio_in, precio_out = PRECIOS_USD_POR_MTOK.get(MODELO, (0.0, 0.0))
    m.costo_usd = round(
        m.tokens_entrada * precio_in / 1_000_000 + m.tokens_salida * precio_out / 1_000_000, 6
    )
    if getattr(respuesta, "_request_id", None):
        m.request_ids.append(respuesta._request_id)


def ejecutar(datos: dict, cliente: Optional[anthropic.Anthropic] = None) -> Resultado:
    metricas = Metricas(modelo=MODELO)
    inicio = time.perf_counter()

    def terminar(**kw) -> Resultado:
        metricas.latencia_ms = round((time.perf_counter() - inicio) * 1000)
        return Resultado(metricas=metricas, **kw)

    try:
        entrada = Entrada.model_validate(datos)
    except ValidationError as e:
        campos = _campos_fallidos(e)
        log.error("Entrada inválida, no se llama a la API. Campos: %s", campos)
        return terminar(ok=False, error=ErrorMotor(tipo="validacion_entrada", mensaje=str(e.errors()[0]["msg"]), campos=campos))

    sistema, usuario = armar_prompts(entrada)
    mensajes: list = [{"role": "user", "content": usuario}]
    if cliente is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            log.error("Falta ANTHROPIC_API_KEY (copiá .env.example a .env y completala)")
            return terminar(ok=False, error=ErrorMotor(tipo="api", mensaje="Falta la variable ANTHROPIC_API_KEY"))
        cliente = anthropic.Anthropic(max_retries=2, timeout=60.0)
    ultimo_error: Exception | None = None

    for intento in range(1, INTENTOS_MAXIMOS + 1):
        metricas.intentos = intento
        try:
            respuesta = _llamar(cliente, sistema, mensajes)
        except anthropic.AuthenticationError:
            return terminar(ok=False, error=ErrorMotor(tipo="api", mensaje="API key inválida o ausente (ANTHROPIC_API_KEY)"))
        except anthropic.NotFoundError as e:
            return terminar(ok=False, error=ErrorMotor(tipo="api", mensaje=f"Modelo o endpoint inexistente: {e.message}"))
        except anthropic.RateLimitError:
            return terminar(ok=False, error=ErrorMotor(tipo="api", mensaje="Límite de uso de la API (429) tras los reintentos del SDK"))
        except anthropic.APIStatusError as e:
            return terminar(ok=False, error=ErrorMotor(tipo="api", mensaje=f"Error HTTP {e.status_code}: {e.message}"))
        except anthropic.APIConnectionError:
            return terminar(ok=False, error=ErrorMotor(tipo="api", mensaje="Sin conexión con la API de Anthropic"))

        _sumar_uso(metricas, respuesta)

        if respuesta.stop_reason == "refusal":
            return terminar(ok=False, error=ErrorMotor(tipo="rechazo", mensaje="El modelo rechazó la consulta"))

        texto = _texto(respuesta)
        try:
            salida = validar_salida(texto)
            if salida.periodo != entrada.periodo:
                raise ValueError(f"periodo {salida.periodo} distinto del pedido {entrada.periodo}")
            return terminar(ok=True, salida=salida)
        except (ValidationError, ValueError) as e:
            ultimo_error = e
            campos = _campos_fallidos(e)
            log.warning("Intento %d: salida inválida. Campos que fallaron: %s", intento, campos)
            # Se agrega la respuesta completa (append-only) y se pide la corrección.
            mensajes.append({"role": "assistant", "content": respuesta.content})
            mensajes.append({
                "role": "user",
                "content": f"Tu respuesta no pasó la validación ({e}). Devolvé SOLO el JSON corregido.",
            })

    return terminar(
        ok=False,
        error=ErrorMotor(
            tipo="validacion_salida",
            mensaje=f"La salida no validó tras {INTENTOS_MAXIMOS} intentos: {ultimo_error}",
            campos=_campos_fallidos(ultimo_error) if ultimo_error else [],
        ),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Motor LLM de proyección de DDJJ")
    p.add_argument("--cliente", required=True, help="carpeta con config.json, emitidos.csv y recibidos.csv")
    p.add_argument("--periodo", required=True, help="AAAA-MM")
    p.add_argument("--consulta", required=True)
    a = p.parse_args()

    datos = cargar_cliente(Path(a.cliente))
    datos.update(periodo=a.periodo, consulta=a.consulta)
    r = ejecutar(datos)
    print(r.model_dump_json(indent=2, exclude_none=True))
    m = r.metricas
    print(
        f"\n[{'OK' if r.ok else 'ERROR'}] modelo={m.modelo} intentos={m.intentos} "
        f"tokens={m.tokens_entrada}+{m.tokens_salida} costo=USD {m.costo_usd:.4f} latencia={m.latencia_ms} ms",
        file=sys.stderr,
    )
    sys.exit(0 if r.ok else 1)


if __name__ == "__main__":
    main()

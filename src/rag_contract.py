"""Contratos de datos del módulo RAG (PE3).

Equivalencias con la consigna (los identificadores van en inglés):
  ingestar()  -> rag.ingest()          buscar() -> rag.search()      responder() -> rag.answer()
  documento   -> document              fragmento_id -> chunk_id     texto -> text    puntaje -> score
  fuentes     -> sources               requiere_humano -> requires_human
  Entrada / Salida de la PE2 -> EngineInput / EngineOutput (src/contract.py)
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from contract import EngineOutput, ProjectedTax


class Role(str, Enum):
    SOCIO = "socio"
    AUXILIAR = "auxiliar"


class User(BaseModel):
    """Quién consulta. El rol decide qué documentos puede leer (RBAC del PE1)."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: Role


class SearchHit(BaseModel):
    """Un fragmento devuelto por search()."""

    document: str
    chunk_id: str
    text: str
    score: float
    section: str = ""
    title: str = ""
    vector_rank: Optional[int] = None   # posición en la búsqueda vectorial (1 = mejor)
    bm25_rank: Optional[int] = None     # posición en la búsqueda BM25
    metadata: dict = {}


class Source(BaseModel):
    """Fuente citada en la respuesta: documento + fragmento + versión vigente."""

    document: str
    chunk_id: str
    title: str
    section: str
    version: str = ""
    effective_date: str = ""


class RagModelReply(BaseModel):
    """Lo que tiene que devolver el modelo (JSON). Las fuentes se arman en código a partir de source_ids."""

    status: Literal["answered", "needs_clarification", "out_of_scope"]
    answer: str = Field(max_length=1500)
    explanation: str = Field(min_length=20, max_length=1500)
    confidence: float = Field(ge=0, le=1)
    requires_human: bool
    source_ids: list[str] = []


class RagOutput(EngineOutput):
    """La Salida de la PE2 más `answer`, `requires_human` y la lista de `sources`.

    Una respuesta de conocimiento no trae impuestos proyectados (taxes vacío, total_due 0).
    """

    status: Literal["answered", "needs_clarification", "out_of_scope"]  # type: ignore[assignment]
    taxes: list[ProjectedTax] = Field(default_factory=list, max_length=0)
    total_due: float = Field(default=0.0, ge=0, le=0)
    answer: str = Field(max_length=1500)
    requires_human: bool
    sources: list[Source] = []

    @model_validator(mode="after")
    def evidence_rules(self) -> "RagOutput":
        if self.requires_human:
            if self.status == "answered":
                raise ValueError("requires_human=true no puede tener status 'answered'")
        else:
            if self.status != "answered":
                raise ValueError(f"status '{self.status}' exige requires_human=true")
            if not self.sources:
                raise ValueError("una respuesta sin revisión humana tiene que citar al menos una fuente")
            if not self.answer.strip():
                raise ValueError("answer vacío en una respuesta 'answered'")
        return self

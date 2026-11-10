"""Lectura de los documentos de la base de conocimiento y fragmentado (chunking).

Dos configuraciones para comparar en la medición top-k:
  - "section": un fragmento por sección Markdown (##); si la sección supera max_chars se corta
    con solapamiento. Respeta tablas y listas completas.
  - "fixed": ventanas fijas de N caracteres con solapamiento, sin mirar la estructura.
Cada fragmento lleva delante "Título > Sección" para que tanto los embeddings como BM25 sepan
de qué documento viene.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CHUNK_CONFIGS = {
    "section": {"strategy": "section", "max_chars": 1200, "overlap": 150},
    "fixed": {"strategy": "fixed", "max_chars": 350, "overlap": 50},
}
REQUIRED_FIELDS = ("doc_id", "title", "roles", "version", "effective_date", "owner")
VALID_ROLES = {"socio", "auxiliar"}


@dataclass
class Document:
    doc_id: str
    title: str
    roles: list[str]
    version: str
    effective_date: str
    owner: str
    path: str
    body: str


@dataclass
class Chunk:
    chunk_id: str
    document: str
    section: str
    text: str
    metadata: dict = field(default_factory=dict)


def clean(text: str) -> str:
    """Limpieza: saca espacios repetidos y líneas en blanco de más (el Markdown queda legible)."""
    text = text.replace("\r\n", "\n").replace("\t", " ")
    text = re.sub(r"[  ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_document(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if not match:
        raise ValueError(f"{path}: falta el encabezado de metadatos entre ---")
    meta = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    missing = [f for f in REQUIRED_FIELDS if not meta.get(f)]
    if missing:
        raise ValueError(f"{path}: faltan metadatos {missing}")
    roles = [r.strip() for r in meta["roles"].split(",") if r.strip()]
    if not roles or set(roles) - VALID_ROLES:
        raise ValueError(f"{path}: roles inválidos {roles}")
    return Document(
        doc_id=meta["doc_id"], title=meta["title"], roles=roles, version=meta["version"],
        effective_date=meta["effective_date"], owner=meta["owner"], path=str(path),
        body=clean(match.group(2)),
    )


def load_documents(folder: Path) -> list[Document]:
    folder = Path(folder).resolve()
    paths = sorted(p for p in folder.rglob("*.md") if p.name.lower() != "readme.md")
    docs = [parse_document(p) for p in paths]
    for d in docs:  # ruta relativa (ej. knowledge_base/tax_tables/x.md), sin datos de la máquina
        d.path = Path(d.path).relative_to(folder.parent).as_posix()
    return docs


def _sections(body: str) -> list[tuple[str, str]]:
    """Divide por encabezados ## (el # inicial es el título y queda como 'Introducción')."""
    parts, current, lines = [], "Introducción", []
    for line in body.splitlines():
        if line.startswith("## "):
            if "\n".join(lines).strip():
                parts.append((current, "\n".join(lines).strip()))
            current, lines = line[3:].strip(), []
        elif line.startswith("# "):
            continue
        else:
            lines.append(line)
    if "\n".join(lines).strip():
        parts.append((current, "\n".join(lines).strip()))
    return parts


def _windows(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):  # cortar en el último salto de línea o espacio para no partir palabras
            cut = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if cut > start + size // 2:
                end = cut
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        # el solapamiento arranca en un inicio de palabra
        nxt = end - overlap
        space = text.find(" ", nxt, end)
        start = space + 1 if space != -1 else nxt
    return [w for w in out if w]


def _section_at(sections: list[tuple[str, int]], pos: int) -> str:
    name = sections[0][0] if sections else "Introducción"
    for title, offset in sections:
        if offset <= pos:
            name = title
    return name


def chunk_document(doc: Document, config: str) -> list[Chunk]:
    cfg = CHUNK_CONFIGS[config]
    pieces: list[tuple[str, str]] = []
    if cfg["strategy"] == "section":
        for section, text in _sections(doc.body):
            pieces += [(section, w) for w in _windows(text, cfg["max_chars"], cfg["overlap"])]
    else:
        body = "\n".join(l for l in doc.body.splitlines() if not l.startswith("# "))
        offsets = [(m.group(1).strip(), m.start()) for m in re.finditer(r"^## (.+)$", body, re.M)]
        offsets = [("Introducción", 0)] + offsets
        start = 0
        for w in _windows(body, cfg["max_chars"], cfg["overlap"]):
            pos = body.find(w[:40], max(0, start - cfg["overlap"] - 5))
            pos = pos if pos >= 0 else start
            pieces.append((_section_at(offsets, pos), w))
            start = pos + len(w)

    chunks = []
    for i, (section, text) in enumerate(pieces):
        meta = {
            "document": doc.doc_id,
            "title": doc.title,
            "section": section,
            "roles": ",".join(doc.roles),
            # Un booleano por rol: ChromaDB filtra con where={"role_<rol>": True} dentro de la consulta.
            **{f"role_{r}": r in doc.roles for r in sorted(VALID_ROLES)},
            "version": doc.version,
            "effective_date": doc.effective_date,
            "owner": doc.owner,
            "source_path": doc.path,
            "chunk_index": i,
            "chunk_config": config,
        }
        chunks.append(Chunk(
            chunk_id=f"{doc.doc_id}::{config}::{i:03d}",
            document=doc.doc_id,
            section=section,
            text=f"{doc.title} > {section}\n{text}",
            metadata=meta,
        ))
    return chunks

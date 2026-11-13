"""Read-only M3 editorial knowledge catalog; no automatic web ingestion."""

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from app.rag.bm25 import Bm25Encoder
from app.schemas.common import ContractModel

Role = Literal["ai_product_manager", "fullstack", "agent_engineer"]


class KnowledgeSource(ContractModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str
    roles: list[Role] = Field(min_length=1)
    topics: list[str] = Field(min_length=1)
    version: str
    version_scope: str
    verified_on: date
    content_kind: Literal["editorial_summary"]
    usage: Literal["reference_only"]
    local_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("url")
    @classmethod
    def public_https(cls, value):
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("Source must have a public HTTPS URL without credentials")
        return value


class KnowledgeReference(ContractModel):
    source_id: str
    source_version: str
    source_url: str
    title: str
    summary: str
    roles: list[Role] = Field(min_length=1)
    content_kind: Literal["editorial_summary"] = "editorial_summary"
    usage: Literal["reference_only"] = "reference_only"


def load_catalog(root: Path) -> list[tuple[KnowledgeSource, str]]:
    root = root.resolve()
    records = json.loads((root / "sources.json").read_text(encoding="utf-8"))["sources"]
    result = []
    ids = set()
    for raw in records:
        source = KnowledgeSource.model_validate(raw)
        if source.id in ids:
            raise ValueError("Duplicate knowledge source ID")
        ids.add(source.id)
        path = (root / source.local_path).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Knowledge path escapes its root")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != source.sha256:
            raise ValueError(f"Knowledge snapshot fingerprint changed: {source.id}")
        result.append((source, content.decode("utf-8")))
    return result


def retrieve_local_knowledge(
    root: Path, *, role: Role, query: str, top_k: int = 4, max_chars: int = 6000
) -> list[KnowledgeReference]:
    """Lexical prototype over role-filtered summaries, not production RAG quality."""
    if role not in {"ai_product_manager", "fullstack", "agent_engineer"}:
        raise ValueError("Unknown role")
    if not query.strip() or not 1 <= top_k <= 8 or not 1 <= max_chars <= 16000:
        raise ValueError("Invalid knowledge query or context budget")
    candidates = [(s, text) for s, text in load_catalog(root) if role in s.roles]
    encoder = Bm25Encoder.fit([text for _, text in candidates])
    query_vector = dict(
        zip(encoder.encode_query(query).indices, encoder.encode_query(query).values, strict=True)
    )
    scored = []
    for source, text in candidates:
        vector = encoder.encode_document(text)
        score = sum(
            query_vector.get(i, 0) * value
            for i, value in zip(vector.indices, vector.values, strict=True)
        )
        if score > 0:
            scored.append((score, source.id, source, text))
    scored.sort(key=lambda row: (-row[0], row[1]))
    result = []
    used = 0
    for _, _, source, text in scored:
        if len(result) == top_k:
            break
        if used + len(text) > max_chars:
            continue
        result.append(
            KnowledgeReference(
                source_id=source.id,
                source_version=source.version,
                source_url=source.url,
                title=source.title,
                summary=text,
                roles=source.roles,
            )
        )
        used += len(text)
    return result

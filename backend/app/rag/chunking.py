"""Text parsing and chunking for the RAG module (member C, issue #8).

M1 ingests UTF-8 Markdown/TXT only; PDF arrives in M2. The parser normalizes
newlines and reports 1-based line numbers, which is the locator format the
API contract and the database design document use for citations.

Two invariants drive the chunker:

1. Every chunk is a *contiguous line range* of the parsed text, so a quoted
   citation can always be reconstructed verbatim
   (``"\\n".join(lines[start - 1 : end]) == chunk.text``). This is exactly what
   the repository fixture tests assert, and it is what makes a citation honest.
2. Paragraphs are never split in this version: a paragraph that exceeds the
   budget becomes its own chunk. Blank separator lines between packed sections
   belong to no chunk, so every non-blank line is covered exactly once.

Chunk parameters are recorded through ``chunking_config_version`` and stay
tunable for the retrieval experiments the PRD plans (dense vs hybrid vs
rerank on a fixed configuration).
"""

import re

from pydantic import Field

from app.schemas.analysis import TextLocator
from app.schemas.common import ContractModel

PARSER_VERSION = "text-utf8-lf-v1"
HEADING_PATTERN = re.compile(r"^ {0,3}#{1,6} ")


class DocumentParseError(RuntimeError):
    """The uploaded bytes are not usable UTF-8 text."""


class ParsedDocument(ContractModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": False}

    text: str
    lines: list[str]


class DocumentChunk(ContractModel):
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    locator: TextLocator


def parse_document(raw: bytes) -> ParsedDocument:
    """Decode upload bytes into normalized text and 1-based addressable lines."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DocumentParseError("document is not valid UTF-8 text") from error
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()  # a trailing newline does not add a real line
    return ParsedDocument(text="\n".join(lines), lines=lines)


def chunking_config_version(max_chars: int) -> str:
    """Version tag recorded with every index build, so results stay reproducible."""
    return f"block-pack-v1::{max_chars}"


def chunk_document(lines: list[str], *, max_chars: int) -> list[DocumentChunk]:
    """Pack heading-led blocks into contiguous line-range chunks."""
    blocks = _blocks(lines)
    chunks: list[DocumentChunk] = []
    current: list[tuple[int, int]] = []
    current_chars = 0
    for start, end in blocks:
        block_chars = sum(len(lines[index - 1]) for index in range(start, end + 1))
        if current and current_chars + block_chars > max_chars:
            chunks.append(_make_chunk(lines, current, len(chunks)))
            current = []
            current_chars = 0
        current.append((start, end))
        current_chars += block_chars
    if current:
        chunks.append(_make_chunk(lines, current, len(chunks)))
    return chunks


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Split into (start, end) units: a heading stays attached to its section."""
    units: list[tuple[int, int]] = []
    index = 1
    total = len(lines)
    while index <= total:
        if not lines[index - 1].strip():
            index += 1
            continue
        if HEADING_PATTERN.match(lines[index - 1]):
            start = index
            while index <= total and (
                not lines[index - 1].strip() or HEADING_PATTERN.match(lines[index - 1])
            ):
                index += 1
            end = index - 1
            units.append(_trim_unit(lines, start, end))
            continue
        start = index
        while (
            index <= total
            and lines[index - 1].strip()
            and not HEADING_PATTERN.match(lines[index - 1])
        ):
            index += 1
        units.append((start, index - 1))
    # Attach heading units to the following body unit.
    merged: list[tuple[int, int]] = []
    pending: list[tuple[int, int]] = []
    for unit in units:
        if len(lines[unit[0] - 1 : unit[1]]) == 1 and HEADING_PATTERN.match(lines[unit[0] - 1]):
            pending.append(unit)
            continue
        if pending:
            start = pending[0][0]
            end = unit[1]
            pending = []
            merged.append((start, end))
        else:
            merged.append(unit)
    merged.extend(pending)  # trailing headings with no body stay their own unit
    return merged


def _trim_unit(lines: list[str], start: int, end: int) -> tuple[int, int]:
    while start < end and not lines[start - 1].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return (start, end)


def _make_chunk(lines: list[str], blocks: list[tuple[int, int]], ordinal: int) -> DocumentChunk:
    start = blocks[0][0]
    end = blocks[-1][1]
    text = "\n".join(lines[start - 1 : end])
    return DocumentChunk(
        ordinal=ordinal, text=text, locator=TextLocator(kind="text", line_start=start, line_end=end)
    )


__all__ = [
    "DocumentChunk",
    "DocumentParseError",
    "ParsedDocument",
    "PARSER_VERSION",
    "chunk_document",
    "chunking_config_version",
    "parse_document",
]

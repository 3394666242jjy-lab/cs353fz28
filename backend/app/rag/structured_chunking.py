"""M3 opt-in Markdown splitter. Exact quotes and context metadata stay separate.

This prototype does not change the M2 upload/indexing default. Character offsets
refer to LF-normalized source text; they are needed when a long line is split.
"""

import bisect
import hashlib
import re
from dataclasses import dataclass

HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def normalize_source(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class ChunkPolicy:
    target_chars: int = 600
    hard_max_chars: int = 6000

    def __post_init__(self):
        if not 64 <= self.target_chars <= self.hard_max_chars <= 64000:
            raise ValueError("Require 64 <= target_chars <= hard_max_chars <= 64000")

    @property
    def version(self) -> str:
        return f"markdown-structure-v1/target{self.target_chars}/hard{self.hard_max_chars}"


class ChunkingLimitError(ValueError):
    """A code block or table exceeds the hard bound; review instead of truncating."""


@dataclass(frozen=True)
class StructuredChunk:
    id: str
    parent_id: str
    ordinal: int
    text: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    heading_path: tuple[str, ...]
    kinds: tuple[str, ...]
    parent_start: int
    parent_end: int
    policy_version: str

    @property
    def embedding_text(self) -> str:
        # This augmented string is for retrieval only, never an original quote.
        return " > ".join(self.heading_path) + "\n" + self.text


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    kind: str
    path: tuple[str, ...]
    parent_start: int
    parent_end: int


def split_structured(
    text: str, *, source_id: str, source_version: str, policy: ChunkPolicy | None = None
) -> list[StructuredChunk]:
    if not source_id.strip() or not source_version.strip():
        raise ValueError("Source identity and version are required")
    policy = policy or ChunkPolicy()
    text = normalize_source(text)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    units: list[_Unit] = []
    stack: list[tuple[int, str]] = []
    index = 0
    pending = None
    section_start, section_end = 0, len(text)
    while index < len(lines):
        line = lines[index].rstrip("\n")
        heading = HEADING.match(line)
        if heading:
            level = len(heading[1])
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading[2]))
            section_start = offsets[index]
            # Skip fenced code while finding this section's enclosing boundary.
            section_end = len(text)
            active_fence = None
            for j in range(index + 1, len(lines)):
                marker = FENCE.match(lines[j].rstrip("\n"))
                if marker:
                    if active_fence is None:
                        active_fence = marker[1]
                    elif (
                        marker[1][0] == active_fence[0]
                        and len(marker[1]) >= len(active_fence)
                        and not marker[2].strip()
                    ):
                        active_fence = None
                    continue
                candidate = HEADING.match(lines[j]) if active_fence is None else None
                if candidate and len(candidate[1]) <= level:
                    section_end = offsets[j]
                    break
            pending = index if pending is None else pending
            index += 1
            continue
        if not line.strip():
            index += 1
            continue
        start = pending if pending is not None else index
        pending = None
        fence = FENCE.match(line)
        if fence:
            kind = "code"
            marker = fence[1]
            index += 1
            while index < len(lines):
                closing = FENCE.match(lines[index].rstrip("\n"))
                index += 1
                if (
                    closing
                    and closing[1][0] == marker[0]
                    and len(closing[1]) >= len(marker)
                    and not closing[2].strip()
                ):
                    break
        elif index + 1 < len(lines) and "|" in line and TABLE_SEPARATOR.match(lines[index + 1]):
            kind = "table"
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                index += 1
        else:
            kind = "paragraph"
            index += 1
            while index < len(lines) and lines[index].strip():
                if HEADING.match(lines[index]) or FENCE.match(lines[index]):
                    break
                if (
                    index + 1 < len(lines)
                    and "|" in lines[index]
                    and TABLE_SEPARATOR.match(lines[index + 1])
                ):
                    break
                index += 1
        units.append(
            _Unit(
                offsets[start],
                offsets[index],
                kind,
                tuple(title for _, title in stack),
                min(offsets[start], section_start),
                section_end,
            )
        )
    if pending is not None:
        units.append(
            _Unit(
                offsets[pending],
                len(text),
                "heading",
                tuple(title for _, title in stack),
                offsets[pending],
                len(text),
            )
        )

    pieces: list[_Unit] = []
    for unit in units:
        if unit.kind in {"code", "table"}:
            if unit.end - unit.start > policy.hard_max_chars:
                raise ChunkingLimitError(f"{unit.kind} at offset {unit.start} exceeds hard limit")
            pieces.append(unit)
            continue
        start = unit.start
        while start < unit.end:
            end = min(start + policy.target_chars, unit.end)
            if end < unit.end:
                boundary = max(
                    text.rfind("\n", start + policy.target_chars // 2, end),
                    text.rfind(" ", start + policy.target_chars // 2, end),
                )
                if boundary >= 0:
                    end = boundary + 1
            if text[start:end].strip():
                pieces.append(
                    _Unit(start, end, unit.kind, unit.path, unit.parent_start, unit.parent_end)
                )
            start = end

    groups: list[list[_Unit]] = []
    for piece in pieces:
        if (
            groups
            and groups[-1][-1].path == piece.path
            and piece.end - groups[-1][0].start <= policy.target_chars
        ):
            groups[-1].append(piece)
        else:
            groups.append([piece])
    source_hash = hashlib.sha256(text.encode()).hexdigest()
    identity = f"{source_id}/{source_version}/{source_hash}/{policy.version}"
    result = []
    for ordinal, group in enumerate(groups):
        first, last = group[0], group[-1]
        start, end = first.start, last.end
        parent_start = min(unit.parent_start for unit in group)
        parent_end = max(unit.parent_end for unit in group)
        result.append(
            StructuredChunk(
                id=hashlib.sha256(f"{identity}/{start}:{end}".encode()).hexdigest(),
                parent_id=hashlib.sha256(
                    f"{identity}/parent/{parent_start}:{parent_end}".encode()
                ).hexdigest(),
                ordinal=ordinal,
                text=text[start:end],
                char_start=start,
                char_end=end,
                line_start=bisect.bisect_right(offsets, start),
                line_end=bisect.bisect_right(offsets, end - 1),
                heading_path=first.path,
                kinds=tuple(sorted({unit.kind for unit in group})),
                parent_start=parent_start,
                parent_end=parent_end,
                policy_version=policy.version,
            )
        )
    return result

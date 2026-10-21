from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.rag.chunking import (
    PARSER_VERSION,
    DocumentParseError,
    chunk_document,
    chunking_config_version,
    parse_document,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"


def load(name: str):
    return parse_document((EXAMPLES / name).read_bytes())


def non_blank_lines(lines: list[str]) -> list[int]:
    return [index for index, line in enumerate(lines, start=1) if line.strip()]


def test_synthetic_project_document_parses_with_expected_line_count():
    document = load("project-demo.md")
    assert len(document.lines) == 24
    assert document.lines[10].startswith("负责设计设备")
    assert document.lines[14].startswith("在热门设备列表查询")
    assert PARSER_VERSION == "text-utf8-lf-v1"


def test_chunks_are_verbatim_contiguous_line_ranges():
    document = load("project-demo.md")
    chunks = chunk_document(document.lines, max_chars=60)
    assert chunks
    covered: list[int] = []
    for chunk in chunks:
        span = document.lines[chunk.locator.line_start - 1 : chunk.locator.line_end]
        assert "\n".join(span) == chunk.text
        covered.extend(range(chunk.locator.line_start, chunk.locator.line_end + 1))
    assert len(covered) == len(set(covered))  # chunk ranges never overlap
    assert set(non_blank_lines(document.lines)) <= set(covered)  # every real line covered
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.locator.line_end >= chunk.locator.line_start for chunk in chunks)


def test_fixture_quotes_stay_whole_within_one_chunk():
    document = load("project-demo.md")
    chunks = chunk_document(document.lines, max_chars=60)
    owner = {}
    for chunk in chunks:
        for line in range(chunk.locator.line_start, chunk.locator.line_end + 1):
            owner[line] = chunk
    # Database design quote (fixture chunk_demo_db) sits in one chunk.
    assert "负责设计设备" in owner[11].text
    assert owner[11].locator.line_end >= 11
    # Cache quote (fixture chunk_demo_cache) keeps lines 15-16 together.
    assert owner[15] is owner[16]
    assert "Redis 缓存" in owner[15].text


def test_headings_never_end_a_chunk_alone():
    document = load("project-demo.md")
    for max_chars in (60, 120, 300):
        chunks = chunk_document(document.lines, max_chars=max_chars)
        for chunk in chunks:
            last_non_blank = max(
                line
                for line in range(chunk.locator.line_start, chunk.locator.line_end + 1)
                if document.lines[line - 1].strip()
            )
            assert not document.lines[last_non_blank - 1].lstrip().startswith("#")


def test_long_paragraph_is_never_split_mid_text():
    lines = ["# 标题", "", "内容" * 200]
    chunks = chunk_document(lines, max_chars=100)
    assert len(chunks) == 1
    assert chunks[0].locator.model_dump() == {
        "kind": "text",
        "line_start": 1,
        "line_end": 3,
    }
    assert chunks[0].text == "\n".join(lines)


def test_blank_and_whitespace_documents_yield_no_chunks():
    assert chunk_document([], max_chars=100) == []
    assert chunk_document([""], max_chars=100) == []
    assert chunk_document(["   ", "\t"], max_chars=100) == []
    assert load("resume-demo.md")
    assert len(chunk_document(load("resume-demo.md").lines, max_chars=60)) >= 4


def test_crlf_and_cr_are_normalized_before_line_numbering():
    document = parse_document("# 标题\r\n\r\n正文\r\n".encode())
    assert document.lines == ["# 标题", "", "正文"]
    chunks = chunk_document(document.lines, max_chars=100)
    assert "\r" not in chunks[0].text
    assert chunks[0].text == "# 标题\n\n正文"


def test_invalid_utf8_is_rejected_without_guessing():
    with pytest.raises(DocumentParseError):
        parse_document(b"\xff\xfe# \x80bad")


def test_trailing_newlines_do_not_add_lines():
    assert parse_document("正文\n\n\n".encode()).lines == ["正文"]


def test_chunking_is_deterministic():
    document = load("project-demo.md")
    first = chunk_document(document.lines, max_chars=120)
    second = chunk_document(document.lines, max_chars=120)
    assert [chunk.text for chunk in first] == [chunk.text for chunk in second]
    assert [chunk.locator for chunk in first] == [chunk.locator for chunk in second]


def test_config_version_moves_with_parameters():
    assert chunking_config_version(60) != chunking_config_version(100)
    assert chunking_config_version(100) == "block-pack-v1::100"


def test_chunk_settings_reject_out_of_range_values():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chunk_max_chars=10)

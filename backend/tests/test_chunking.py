"""chunking 解析与切分单元测试（成员 C，issue #8）。

以仓库合成样例（examples/）为输入，验证切分器的核心不变式：
chunk 是原文连续行区间（quote 可逐字还原）、非空行全覆盖、段落不拆、
标题不落单、换行归一与 UTF-8 校验。
"""

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
    """读取合成样例的原始字节并解析（与真实入库入口一致）。"""
    return parse_document((EXAMPLES / name).read_bytes())


def non_blank_lines(lines: list[str]) -> list[int]:
    """返回所有非空行的 1 起始行号。"""
    return [index for index, line in enumerate(lines, start=1) if line.strip()]


def test_synthetic_project_document_parses_with_expected_line_count():
    """project-demo.md 解析出 24 行，fixture 锚点行内容与预期一致。"""
    document = load("project-demo.md")
    assert len(document.lines) == 24
    assert document.lines[10].startswith("负责设计设备")
    assert document.lines[14].startswith("在热门设备列表查询")
    assert PARSER_VERSION == "text-utf8-lf-v1"


def test_chunks_are_verbatim_contiguous_line_ranges():
    """每个 chunk 都是连续行区间且逐字可还原；行区间不重叠；非空行全覆盖。"""
    document = load("project-demo.md")
    chunks = chunk_document(document.lines, max_chars=60)
    assert chunks
    covered: list[int] = []
    for chunk in chunks:
        span = document.lines[chunk.locator.line_start - 1 : chunk.locator.line_end]
        assert "\n".join(span) == chunk.text
        covered.extend(range(chunk.locator.line_start, chunk.locator.line_end + 1))
    assert len(covered) == len(set(covered))  # chunk 行区间互不重叠
    assert set(non_blank_lines(document.lines)) <= set(covered)  # 非空行全部覆盖
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.locator.line_end >= chunk.locator.line_start for chunk in chunks)


def test_fixture_quotes_stay_whole_within_one_chunk():
    """fixture 两条 quote（数据库设计 11 行、缓存 15-16 行）各自完整落在同一 chunk。"""
    document = load("project-demo.md")
    chunks = chunk_document(document.lines, max_chars=60)
    owner = {}
    for chunk in chunks:
        for line in range(chunk.locator.line_start, chunk.locator.line_end + 1):
            owner[line] = chunk
    # 数据库设计 quote（fixture chunk_demo_db）完整位于一个 chunk 内。
    assert "负责设计设备" in owner[11].text
    assert owner[11].locator.line_end >= 11
    # 缓存 quote（fixture chunk_demo_cache）保持 15-16 两行同处一个 chunk。
    assert owner[15] is owner[16]
    assert "Redis 缓存" in owner[15].text


def test_headings_never_end_a_chunk_alone():
    """任何 max_chars 下，chunk 的最后一个非空行都不是标题（标题不落单）。"""
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
    """超预算段落不自切：整段自成 chunk，行区间覆盖全部三行。"""
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
    """空白/纯空白文档不产出 chunk；正常样例至少切出 4 个 chunk。"""
    assert chunk_document([], max_chars=100) == []
    assert chunk_document([""], max_chars=100) == []
    assert chunk_document(["   ", "\t"], max_chars=100) == []
    assert load("resume-demo.md")
    assert len(chunk_document(load("resume-demo.md").lines, max_chars=60)) >= 4


def test_crlf_and_cr_are_normalized_before_line_numbering():
    """CRLF/CR 归一为 LF 后再编行号，chunk 文本不含回车符。"""
    document = parse_document("# 标题\r\n\r\n正文\r\n".encode())
    assert document.lines == ["# 标题", "", "正文"]
    chunks = chunk_document(document.lines, max_chars=100)
    assert "\r" not in chunks[0].text
    assert chunks[0].text == "# 标题\n\n正文"


def test_invalid_utf8_is_rejected_without_guessing():
    """非 UTF-8 字节立即拒绝，不猜测编码产出空文档。"""
    with pytest.raises(DocumentParseError):
        parse_document(b"\xff\xfe# \x80bad")


def test_trailing_newlines_do_not_add_lines():
    """文件末尾连续换行不产生幻影行。"""
    assert parse_document("正文\n\n\n".encode()).lines == ["正文"]


def test_chunking_is_deterministic():
    """同一输入两次切分结果完全一致（可复现是评测的前提）。"""
    document = load("project-demo.md")
    first = chunk_document(document.lines, max_chars=120)
    second = chunk_document(document.lines, max_chars=120)
    assert [chunk.text for chunk in first] == [chunk.text for chunk in second]
    assert [chunk.locator for chunk in first] == [chunk.locator for chunk in second]


def test_config_version_moves_with_parameters():
    """切分配置版本串随参数变化，且格式稳定可读。"""
    assert chunking_config_version(60) != chunking_config_version(100)
    assert chunking_config_version(100) == "block-pack-v1::100"


def test_chunk_settings_reject_out_of_range_values():
    """chunk_max_chars 的配置边界被 pydantic 拒绝。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chunk_max_chars=10)

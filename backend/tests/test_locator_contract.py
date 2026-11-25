"""locator 契约测试（成员 C，#48）。

把"M2 的 chunk 只出整行、quote 可逐字还原"从实现巧合升格为**契约条款**，并把 M3 的
边界写死：结构化切分一旦在行内切，就不能复用行号接口。

这里断言的是引用诚实的技术前提：只有 quote 与 locator 能互相校验，用户核对原文才有
意义。语义定义见 dev/19。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.rag.chunking import char_span, chunk_document, parse_document
from app.rag.locators import pdf_locator, structured_locator, text_locator
from app.rag.structured_chunking import ChunkPolicy, split_structured
from app.schemas.analysis import PdfLocator, TextLocator

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
CORPUS = ROOT / "backend" / "tests" / "data" / "rag_eval" / "corpus"
FIXTURES = ("resume-demo.md", "project-demo.md")


def parsed(name: str):
    return parse_document((EXAMPLES / name).read_bytes())


def char_span_of(lines: list[str], start: int, end: int) -> tuple[int, int]:
    """把 1 起始的行区间换算成归一化全文的 ``[char_start, char_end)``。"""
    return char_span(lines, line_start=start, line_end=end)


@pytest.mark.parametrize("name", FIXTURES)
@pytest.mark.parametrize("max_chars", (60, 120, 300))
def test_text_quote_equals_its_line_range_and_char_span(name, max_chars):
    """契约一：quote 必须逐字等于 locator 行区间，且能换算成精确字符区间。

    字符区间这一条现在还不对外输出，但它是 span 契约（dev/19）的语义基准——
    先验证 M2 输出天然满足它，将来加字段时不需要改切分器。
    """
    document = parsed(name)

    for chunk in chunk_document(document.lines, max_chars=max_chars):
        lines = document.lines[chunk.locator.line_start - 1 : chunk.locator.line_end]
        assert chunk.text == "\n".join(lines)

        begin, end = char_span_of(document.lines, chunk.locator.line_start, chunk.locator.line_end)
        assert document.text[begin:end] == chunk.text


@pytest.mark.parametrize("name", FIXTURES)
def test_line_ranges_are_disjoint_and_cover_every_non_blank_line(name):
    """契约二：行区间不重叠，且每个非空行恰好被一个 chunk 覆盖。

    覆盖面这一条防止"为了切小片把内容丢掉"，重叠这一条防止同一句被两处引用。
    """
    document = parsed(name)
    covered: list[int] = []

    for chunk in chunk_document(document.lines, max_chars=60):
        covered.extend(range(chunk.locator.line_start, chunk.locator.line_end + 1))

    assert len(covered) == len(set(covered))
    non_blank = {index for index, line in enumerate(document.lines, start=1) if line.strip()}
    assert non_blank <= set(covered)


def test_pdf_pages_are_chunked_independently_and_blank_page_yields_nothing():
    """契约三：PDF 逐页切分，chunk 不跨页；空白页不产 chunk 也不吞掉后一页。

    行号是**页内**行号，页码由 ``PdfLocator(page=...)`` 另带（见 ``app/rag/indexer.py``）。
    把两者混在一层会让"页内行号"和"全文行号"无法区分，这正是本文件要防的事。
    """
    document = parse_document((CORPUS / "alpha_certificate.pdf").read_bytes(), file_type=".pdf")
    assert document.pages is not None
    assert len(document.pages) == 3

    per_page = [chunk_document(page, max_chars=500) for page in document.pages]

    assert per_page[1] == []  # 第 2 页空白，不产 chunk
    assert per_page[0] and per_page[2]  # 第 3 页内容仍保留自己的页内行号
    for page, chunks in zip(document.pages, per_page, strict=True):
        for chunk in chunks:
            lines = page[chunk.locator.line_start - 1 : chunk.locator.line_end]
            assert chunk.text == "\n".join(lines)


def test_crlf_source_is_chunked_against_normalized_text():
    """契约四：行号针对 LF 归一化后的文本，quote 里不出现回车符。"""
    document = parse_document("# 标题\r\n\r\n正文第一行\r\n正文第二行\r\n".encode())

    for chunk in chunk_document(document.lines, max_chars=100):
        assert "\r" not in chunk.text
        assert chunk.text == "\n".join(
            document.lines[chunk.locator.line_start - 1 : chunk.locator.line_end]
        )

    assert document.text == "\n".join(document.lines)


def test_blank_lines_inside_the_range_stay_verbatim():
    """契约五：区间内的空行属于 quote，不允许被压缩或删除。

    引用里少了空行就不再是原文，用户核对时会认为系统改了内容。
    """
    lines = ["## 小节", "第一行", "", "第二行"]

    chunks = chunk_document(lines, max_chars=200)

    assert len(chunks) == 1
    assert chunks[0].text == "## 小节\n第一行\n\n第二行"
    assert (chunks[0].locator.line_start, chunks[0].locator.line_end) == (1, 4)


def test_structured_chunk_split_inside_a_line_cannot_reuse_line_numbers():
    """契约六（M3 边界）：行内切分无法用行号表达，必须走字符 span。

    这条测试是 dev/17 那句"不能把新偏移静默塞进旧行号接口"的执行版本：如果哪天有人
    给结构化切分只填行号而不填 span，或者反过来声称行号够用，这里会先红。
    """
    paragraph = "这是一段很长的正文，需要按字符拆开而不能按行拆。" * 20
    text = f"# 标题\n\n{paragraph}\n"

    chunks = split_structured(
        text, source_id="s", source_version="1", policy=ChunkPolicy(target_chars=300)
    )

    assert len(chunks) >= 2
    lines = text.split("\n")
    mid_line = [
        chunk
        for chunk in chunks
        if chunk.text != "\n".join(lines[chunk.line_start - 1 : chunk.line_end])
    ]
    assert mid_line, "切分没有在行内发生，则本契约的前提需要重新评估"

    for chunk in chunks:
        # 行号表达不了，但字符 span 永远精确：这就是必须补契约的理由。
        assert chunk.text == text[chunk.char_start : chunk.char_end]


def test_char_span_equals_the_line_range_join_for_every_pair():
    """契约七：``char_span()`` 的换算覆盖所有行区间，越界直接报错不夹取。"""
    lines = ["# 标题", "", "正文第一行", "正文第二行"]
    text = "\n".join(lines)

    for start in range(1, len(lines) + 1):
        for end in range(start, len(lines) + 1):
            begin, stop = char_span(lines, line_start=start, line_end=end)
            assert text[begin:stop] == "\n".join(lines[start - 1 : end])

    with pytest.raises(ValueError, match="超出文本范围"):
        char_span(lines, line_start=0, line_end=1)
    with pytest.raises(ValueError, match="超出文本范围"):
        char_span(lines, line_start=2, line_end=len(lines) + 1)


def test_text_locator_accepts_absent_or_complete_span_only():
    """契约八：字符区间要么整对出现，要么都不出现；半个区间与空区间都拒绝。

    历史数据只有行号，必须继续通过校验；新数据即使带 span 也只多不少字段，
    所以这条放宽是向后兼容的关键。
    """
    legacy = TextLocator.model_validate({"kind": "text", "line_start": 10, "line_end": 18})
    assert (legacy.char_start, legacy.char_end) == (None, None)

    full = TextLocator.model_validate(
        {"kind": "text", "line_start": 10, "line_end": 18, "char_start": 240, "char_end": 331}
    )
    assert (full.char_start, full.char_end) == (240, 331)

    with pytest.raises(ValidationError, match="must be provided together"):
        TextLocator.model_validate(
            {"kind": "text", "line_start": 1, "line_end": 1, "char_start": 0}
        )
    with pytest.raises(ValidationError, match="must be greater than char_start"):
        TextLocator.model_validate(
            {"kind": "text", "line_start": 1, "line_end": 1, "char_start": 7, "char_end": 7}
        )


def test_pdf_locator_uses_page_relative_offsets():
    """契约九：PDF 的字符区间相对**该页**文本，不累加前面页面。

    页内偏移是刻意的：全文偏移要跨越空白页记账，解析器一改（例如 OCR 版本）就整体
    漂移；页内偏移只取决于这一页提取出来的文字。
    """
    page_two = ["Credential ID AWS-2024-8841.", "Valid through 2027-06."]
    quote = "Valid through 2027-06."

    locator = pdf_locator(page=2, page_lines=page_two, line_start=2, line_end=2, quote=quote)

    # 第一行 28 字符 + 换行 = 29，第二行从 29 起；quote 长 22。
    assert (locator.page, locator.char_start, locator.char_end) == (2, 29, 51)
    assert "\n".join(page_two)[locator.char_start : locator.char_end] == quote


def test_pdf_locator_rejects_a_span_that_does_not_cut_the_quote():
    """契约十：定位自检不通过就抛错，绝不写入"看起来对"的偏移。"""
    page_lines = ["第一行", "第二行"]

    with pytest.raises(ValueError, match="字符区间与 quote 不一致"):
        pdf_locator(page=1, page_lines=page_lines, line_start=1, line_end=1, quote="第二行")


def test_text_locator_builder_verifies_quote_against_full_text():
    """文本 locator 的字符区间相对全文，并同样自检 quote。"""
    lines = ["# 标题", "", "正文第一行", "正文第二行"]
    text = "\n".join(lines)

    locator = text_locator(
        text=text, lines=lines, line_start=3, line_end=4, quote="正文第一行\n正文第二行"
    )

    assert text[locator.char_start : locator.char_end] == "正文第一行\n正文第二行"
    with pytest.raises(ValueError, match="字符区间与 quote 不一致"):
        text_locator(text=text, lines=lines, line_start=3, line_end=3, quote="正文第二行")


def test_pdf_locator_keeps_span_optional_and_paired():
    """历史 PDF 证据只有页码，仍然合法；半对区间拒绝。"""
    legacy = PdfLocator.model_validate({"kind": "pdf", "page": 3})
    assert (legacy.char_start, legacy.char_end) == (None, None)

    with pytest.raises(ValidationError, match="must be provided together"):
        PdfLocator.model_validate({"kind": "pdf", "page": 3, "char_end": 10})
    with pytest.raises(ValidationError, match="must be greater than char_start"):
        PdfLocator.model_validate({"kind": "pdf", "page": 3, "char_start": 10, "char_end": 10})


def test_structured_locator_carries_span_for_mid_line_splits():
    """契约十一：M3 结构化片段转成 locator 时必须带 span，并且能被原文校验。

    长段落按字符拆开时行号表达不了位置，span 才是权威；``embedding_text`` 拼了标题
    路径，绝不能拿来当 quote。
    """
    paragraph = "这是一段很长的正文，需要按字符拆开而不能按行拆。" * 20
    text = f"# 标题\n\n{paragraph}\n"
    chunks = split_structured(
        text, source_id="s", source_version="1", policy=ChunkPolicy(target_chars=300)
    )

    locator = structured_locator(chunks[0], source_text=text)

    assert text[locator.char_start : locator.char_end] == chunks[0].text
    assert (locator.line_start, locator.line_end) == (chunks[0].line_start, chunks[0].line_end)
    assert chunks[0].embedding_text != chunks[0].text


def test_structured_locator_rejects_a_chunk_that_does_not_match_its_source():
    """切分器给出的区间与原文不符时直接失败，不写入错位定位。"""
    text = "# 标题\n\n正文\n"
    chunks = split_structured(text, source_id="s", source_version="1")

    with pytest.raises(ValueError, match="字符区间与 quote 不一致"):
        structured_locator(chunks[0], source_text="完全不同的原文")


def test_structured_locator_works_without_source_text_but_keeps_the_span():
    """调用方没有原文时无法自检，但仍必须带上切分器给出的区间。"""
    text = "# 标题\n\n正文第一段\n"
    chunks = split_structured(text, source_id="s", source_version="1")

    locator = structured_locator(chunks[0])

    assert (locator.char_start, locator.char_end) == (
        chunks[0].char_start,
        chunks[0].char_end,
    )

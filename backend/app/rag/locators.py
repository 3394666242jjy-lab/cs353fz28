"""定位器构造（成员 C）：把行区间与字符 offset 转成对外 locator。

文本与 PDF 的偏移基准不同，这里把差异集中到一处，避免调用方（入库、M3 接线、评测脚本）
各自换算：

* **文本** locator 的 ``char_start``/``char_end`` 相对**归一化后的全文**；
* **PDF** locator 的 ``char_start``/``char_end`` 相对**该页文本**（页内偏移），并带 1 起始
  页码。用页内偏移而不是全文偏移，是因为全文偏移要跨越空白页记账，解析器一改（例如
  OCR 版本）就整体漂移，而页内偏移不受影响。

两种 locator 构造时都会自检 ``text[char_start:char_end] == quote``：不一致直接抛
``ValueError``——宁可不写定位，也不写一个看起来对、实际差半行的定位。契约见 dev/19。
"""

from app.rag.chunking import char_span
from app.rag.structured_chunking import StructuredChunk
from app.schemas.analysis import PdfLocator, TextLocator


def _verify(*, source: str, char_start: int, char_end: int, quote: str, where: str) -> None:
    """定位自检：字符区间必须精确切出 quote。"""
    if source[char_start:char_end] != quote:
        raise ValueError(f"{where} 的字符区间与 quote 不一致，拒绝写入错误定位")


def text_locator(
    *,
    text: str,
    lines: list[str],
    line_start: int,
    line_end: int,
    quote: str,
) -> TextLocator:
    """构造文本 locator：行号 + 相对**全文**的字符区间。

    ``text`` 必须是 ``"\\n".join(lines)``（``parse_document`` 的归一化结果）；行区间越界
    由 ``char_span`` 显式报错。
    """
    char_start, char_end = char_span(lines, line_start=line_start, line_end=line_end)
    _verify(source=text, char_start=char_start, char_end=char_end, quote=quote, where="文本")
    return TextLocator(
        kind="text",
        line_start=line_start,
        line_end=line_end,
        char_start=char_start,
        char_end=char_end,
    )


def pdf_locator(
    *,
    page: int,
    page_lines: list[str],
    line_start: int,
    line_end: int,
    quote: str,
) -> PdfLocator:
    """构造 PDF locator：页码 + 相对**该页**的字符区间与页内行号。

    页内行号只用于校验，不对外输出（``PdfLocator`` 只有页码与字符区间）。
    """
    page_text = "\n".join(page_lines)
    char_start, char_end = char_span(page_lines, line_start=line_start, line_end=line_end)
    _verify(source=page_text, char_start=char_start, char_end=char_end, quote=quote, where="PDF 页")
    return PdfLocator(kind="pdf", page=page, char_start=char_start, char_end=char_end)


def structured_locator(chunk: StructuredChunk, *, source_text: str | None = None) -> TextLocator:
    """把 M3 结构化片段转成带 span 的文本 locator。

    结构化切分允许在**行内**切分（长段落按字符拆），这正是行号接口表达不了的场景，
    所以这里必须带字符区间。行号只作展示与粗略校验，权威是 ``[char_start, char_end)``。

    ``source_text`` 给定时自检：切分器声明的区间必须能精确切出 ``chunk.text``。不给则
    直接沿用切分器给出的区间——调用方有原文时应当传入，否则错误定位无从发现。

    注意：``StructuredChunk.embedding_text`` 是拼了标题路径的**检索用文本**，不是原文，
    绝不能当 quote 或当作定位依据。
    """
    if source_text is not None:
        _verify(
            source=source_text,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            quote=chunk.text,
            where="结构化片段",
        )
    return TextLocator(
        kind="text",
        line_start=chunk.line_start,
        line_end=chunk.line_end,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
    )


__all__ = ["pdf_locator", "structured_locator", "text_locator"]

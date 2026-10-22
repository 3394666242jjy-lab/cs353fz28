"""文本解析与切分（RAG 模块，成员 C，issue #8）。

M1 只处理 UTF-8 Markdown/TXT，PDF 在 M2。解析器统一换行符并给出 1 起始行号——
这正是 API 契约与数据库设计文档中引用定位（locator）使用的格式。

切分器遵守两条不变式：

1. 每个 chunk 都是解析文本的**连续行区间**，因此引用 quote 永远可逐字还原
   （``"\\n".join(lines[start - 1 : end]) == chunk.text``）。仓库现有 fixture
   测试断言的正是这一点，它是"引用诚实"的技术保障。
2. 本版本不在段落中间强切：超预算的段落自成 chunk。已打包小节之间的空行分隔符
   不属于任何 chunk，因此每个非空行被恰好一个 chunk 覆盖。

切分参数通过 chunking_config_version 留档，并保持可调——PRD 规划的检索对比
实验（dense / hybrid / hybrid_rerank）需要在固定配置下复现。
"""

import re

from pydantic import Field

from app.schemas.analysis import TextLocator
from app.schemas.common import ContractModel

# 解析器版本：解析规则变化时升级，并随索引进 document_versions.parser_version 留档。
PARSER_VERSION = "text-utf8-lf-v1"

# Markdown ATX 标题行（允许前置空格），用于识别小节边界；非标题的 # 文本不受影响。
HEADING_PATTERN = re.compile(r"^ {0,3}#{1,6} ")


class DocumentParseError(RuntimeError):
    """上传字节不是可用的 UTF-8 文本（拒绝猜测编码，交给上层按失败处理）。"""


class ParsedDocument(ContractModel):
    """解析结果：归一化文本与按行拆分（lines[i] 即第 i+1 行，1 起始可寻址）。"""

    model_config = {"extra": "forbid", "str_strip_whitespace": False}

    text: str
    lines: list[str]


class DocumentChunk(ContractModel):
    """一个 chunk：序号、连续行区间拼出的原文、以及 TextLocator 行号定位。"""

    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    locator: TextLocator


def parse_document(raw: bytes) -> ParsedDocument:
    """把上传字节解码为归一化文本与 1 起始行号。

    UTF-8 解码失败抛 DocumentParseError；CRLF/CR 统一为 LF；文件末尾换行不
    产生幻影空行。quote 与原文的一致性以解析后文本为准。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DocumentParseError("文档不是有效的 UTF-8 文本") from error
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return ParsedDocument(text="\n".join(lines), lines=lines)


def chunking_config_version(max_chars: int) -> str:
    """生成切分配置版本串，随每次索引构建留档，保证结果可复现。"""
    return f"block-pack-v1::{max_chars}"


def chunk_document(lines: list[str], *, max_chars: int) -> list[DocumentChunk]:
    """把行序列装包成连续行区间 chunk（标题不与其小节分离，段落不从中切开）。"""
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
    """切分为 (起始行, 结束行) 单元，并把每个标题单元附加到其后的小节正文上。

    连续空行是块边界；超长段落不会在此被拆开（它是单个单元，进入装包阶段后
    可能独占一个 chunk）。
    """
    units: list[tuple[int, int]] = []
    index = 1
    total = len(lines)
    while index <= total:
        if not lines[index - 1].strip():
            index += 1
            continue
        if HEADING_PATTERN.match(lines[index - 1]):
            # 标题单元：连同其后紧跟的空行/连续标题一起吞入，再裁掉边缘空行。
            start = index
            while index <= total and (
                not lines[index - 1].strip() or HEADING_PATTERN.match(lines[index - 1])
            ):
                index += 1
            units.append(_trim_unit(lines, start, index - 1))
            continue
        start = index
        while (
            index <= total
            and lines[index - 1].strip()
            and not HEADING_PATTERN.match(lines[index - 1])
        ):
            index += 1
        units.append((start, index - 1))
    # 把标题单元挂到下一个正文单元前面；文档末尾的孤立标题保留为自有单元。
    merged: list[tuple[int, int]] = []
    pending: list[tuple[int, int]] = []
    for unit in units:
        if len(lines[unit[0] - 1 : unit[1]]) == 1 and HEADING_PATTERN.match(lines[unit[0] - 1]):
            pending.append(unit)
            continue
        if pending:
            start = pending[0][0]
            pending = []
            merged.append((start, unit[1]))
        else:
            merged.append(unit)
    merged.extend(pending)
    return merged


def _trim_unit(lines: list[str], start: int, end: int) -> tuple[int, int]:
    """裁掉单元首尾的纯空行，使单元边界落在有内容的行上。"""
    while start < end and not lines[start - 1].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return (start, end)


def _make_chunk(lines: list[str], blocks: list[tuple[int, int]], ordinal: int) -> DocumentChunk:
    """把一组连续单元合成一个 chunk：行区间取首块起点到末块终点，原文逐行拼接。"""
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

"""文本解析与切分（RAG 模块，成员 C，issue #8 / issue #46）。

M1 支持 UTF-8 Markdown/TXT 和具有文本层的 PDF。文本使用行号定位，PDF
逐页提取并使用页码定位。issue #46 起，含栅格图片的页面对图片区域做局部
OCR（rapidocr-onnxruntime，本地推理）并把识别文字合并进该页；文本层为
空且 OCR 无结果的文件仍按拒绝处理。

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
from io import BytesIO

from pydantic import Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.schemas.analysis import TextLocator
from app.schemas.common import ContractModel

# 解析器版本：解析规则变化时升级，并随索引进 document_versions.parser_version 留档。
PARSER_VERSION = "text-utf8-lf-v1"
PDF_PARSER_VERSION = "pdf-text-ocr-pages-v3"

# 图片 OCR 参数：置信度下限、渲染分辨率、渲染边长上限（防超大图吃掉内存）与
# 参与 OCR 的最小图片边长（pt，点状装饰图不值得识别）。
_OCR_MIN_CONFIDENCE = 0.5
_OCR_RENDER_DPI = 180
_OCR_MAX_RENDER_SIDE = 2400
_OCR_MIN_IMAGE_SIDE = 8

_ocr_engine = None

# Markdown ATX 标题行（允许前置空格），用于识别小节边界；非标题的 # 文本不受影响。
HEADING_PATTERN = re.compile(r"^ {0,3}#{1,6} ")


class DocumentParseError(RuntimeError):
    """上传字节不是可用的 UTF-8 文本（拒绝猜测编码，交给上层按失败处理）。"""


class ParsedDocument(ContractModel):
    """解析结果：归一化文本与按行拆分（lines[i] 即第 i+1 行，1 起始可寻址）。"""

    model_config = {"extra": "forbid", "str_strip_whitespace": False}

    text: str
    lines: list[str]
    pages: list[list[str]] | None = None


class DocumentChunk(ContractModel):
    """一个 chunk：序号、连续行区间拼出的原文、以及 TextLocator 行号定位。"""

    model_config = {"extra": "forbid", "str_strip_whitespace": False}

    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    locator: TextLocator


def _get_ocr_engine():
    """惰性初始化本地 OCR（rapidocr-onnxruntime）；缺依赖时给出可操作的报错。"""
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as error:
            raise DocumentParseError(
                "PDF 图片识别组件未安装；请安装 rapidocr-onnxruntime，或改用仅含文字的 PDF/TXT"
            ) from error
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _pixmap_to_ocr_lines(pix) -> list[str]:
    """对一块渲染好的位图跑 OCR，按阅读顺序返回去空白后的文字行。"""
    import numpy as np

    array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n > 3:
        array = array[:, :, :3]
    result, _ = _get_ocr_engine()(array)
    if not result:
        return []

    def reading_key(item):
        box = item[0]
        return (min(point[1] for point in box), min(point[0] for point in box))

    lines: list[str] = []
    for _box, text, score in sorted(result, key=reading_key):
        normalized = " ".join(str(text).split())
        if score >= _OCR_MIN_CONFIDENCE and normalized:
            lines.append(normalized)
    return lines


def _ocr_page_image_rects(page) -> tuple[list[str], bool]:
    """按坐标顺序识别图片区域，并返回页面是否有可见的待识别图片。"""
    import pymupdf

    rects = []
    seen = set()
    for info in page.get_images(full=True):
        try:
            found = page.get_image_rects(info[0])
        except Exception:  # 损坏或非标图片（如缺尺寸）跳过，不影响整页文字提取
            continue
        for rect in found:
            rect = (rect * page.rotation_matrix) & page.rect
            if (
                rect.is_empty
                or rect.width < _OCR_MIN_IMAGE_SIDE
                or rect.height < _OCR_MIN_IMAGE_SIDE
            ):
                continue
            key = (round(rect.x0), round(rect.y0), round(rect.x1), round(rect.y1))
            if key not in seen:
                seen.add(key)
                rects.append(rect)
    rects.sort(key=lambda item: (item.y0, item.x0))
    lines: list[str] = []
    for rect in rects:
        scale = min(_OCR_RENDER_DPI / 72, (_OCR_MAX_RENDER_SIDE - 2) / max(rect.width, rect.height))
        pix = page.get_pixmap(clip=rect, matrix=pymupdf.Matrix(scale, scale))
        lines.extend(_pixmap_to_ocr_lines(pix))
    return lines, bool(rects)


def _normalized_line(line: str) -> str:
    return " ".join(line.split()).lower()


def _extract_pdf_pages(reader: PdfReader, raw: bytes) -> list[list[str]]:
    """逐页提取文字；含栅格图片的页面对图片区域做局部 OCR 并去重合并。"""
    try:
        import pymupdf
    except ImportError as error:
        raise DocumentParseError("PDF 图片渲染依赖 pymupdf 未安装；请安装 pymupdf>=1.26") from error
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as error:  # 渲染失败按解析失败处理，不向上游抛裸异常
        raise DocumentParseError("PDF 图片渲染失败，无法读取图片内容") from error
    pages: list[list[str]] = []
    unreadable_page: int | None = None
    try:
        for page_number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n")
            lines = text.split("\n")
            while lines and not lines[-1].strip():
                lines.pop()
            known = {_normalized_line(line) for line in lines if line.strip()}
            ocr_lines, has_images = _ocr_page_image_rects(doc[page_number - 1])
            if has_images and not (known or ocr_lines) and unreadable_page is None:
                unreadable_page = page_number
            for ocr_line in ocr_lines:
                normalized = _normalized_line(ocr_line)
                if normalized and normalized not in known:
                    known.add(normalized)
                    lines.append(ocr_line)
            pages.append(lines)
    finally:
        doc.close()
    if unreadable_page is not None and any(line.strip() for page in pages for line in page):
        raise DocumentParseError(
            f"PDF 第 {unreadable_page} 页没有可用的文字层，且本地 OCR 未能识别文字。"
        )
    return pages


def parse_document(raw: bytes, *, file_type: str | None = None) -> ParsedDocument:
    """把上传字节解码为归一化文本与 1 起始行号。

    UTF-8 解码失败抛 DocumentParseError；CRLF/CR 统一为 LF；文件末尾换行不
    产生幻影空行。quote 与原文的一致性以解析后文本为准。
    """
    if file_type == ".pdf" or (file_type is None and raw.startswith(b"%PDF-")):
        try:
            reader = PdfReader(BytesIO(raw), strict=True)
            if reader.is_encrypted:
                raise DocumentParseError("暂不支持加密 PDF，请上传可直接读取的文件")
            pages = _extract_pdf_pages(reader, raw)
        except DocumentParseError:
            raise
        except (
            PdfReadError,
            ValueError,
            OSError,
            UnicodeError,
            KeyError,
            TypeError,
            AttributeError,
            RecursionError,
            OverflowError,
        ) as error:
            raise DocumentParseError("PDF 文件损坏或无法解析") from error
        if not any(line.strip() for page in pages for line in page):
            raise DocumentParseError(
                "PDF 未能提取到任何文字（文本层为空且图片 OCR 无结果）；"
                "请确认不是空白或纯装饰图形文档"
            )
        lines = [line for page in pages for line in page]
        return ParsedDocument(text="\n".join(lines), lines=lines, pages=pages)
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

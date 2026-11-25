"""Real multi-page PDF parsing with deterministic, fake embedding/storage."""

import asyncio
import json
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.config import Settings
from app.rag.chunking import DocumentParseError, parse_document
from app.rag.indexer import index_document


def pdf_bytes(*texts: str, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    if encrypted:
        writer.encrypt("password")
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


class Embedder:
    model_version = "test-model"
    dimension = 2

    async def embed_texts(self, texts):
        return [[0.0, 1.0] for _ in texts]


class Store:
    async def replace_document_points(self, **kwargs):
        self.points = kwargs["points"]


def test_multi_page_pdf_is_indexed_with_page_locators():
    raw = pdf_bytes("First page experience", "Second page projects")
    store = Store()
    result = asyncio.run(
        index_document(
            raw,
            user_id="u",
            document_id="d",
            version=1,
            file_type=".pdf",
            embedder=Embedder(),
            store=store,
            settings=Settings(_env_file=None),
        )
    )
    assert [chunk.locator.page for chunk in result.chunks] == [1, 2]
    assert [chunk.text for chunk in result.chunks] == [
        "First page experience",
        "Second page projects",
    ]
    assert len({chunk.chunk_id for chunk in result.chunks}) == 2
    assert result.parser_version == "pdf-text-ocr-pages-v3"


def test_pdf_without_text_and_encrypted_pdf_are_rejected():
    with pytest.raises(DocumentParseError, match="未能提取到任何文字"):
        parse_document(pdf_bytes(""), file_type=".pdf")
    with pytest.raises(DocumentParseError, match="加密"):
        parse_document(pdf_bytes("private", encrypted=True), file_type=".pdf")


def test_text_chunks_keep_original_indentation():
    from app.rag.chunking import chunk_document

    parsed = parse_document(b"  indented\nsecond line  ")
    chunk = chunk_document(parsed.lines, max_chars=500)[0]
    assert chunk.text == "  indented\nsecond line  "


def test_indexing_worker_uses_pdf_pages_and_saves_extracted_text(tmp_path, monkeypatch):
    from app import worker
    from app.rag import indexer
    from app.services.storage import read_upload, save_upload

    config = Settings(_env_file=None, app_env="test", local_storage_dir=tmp_path)
    key = save_upload(config, "u", "resume.pdf", pdf_bytes("First page", "Second page"))
    saved = []

    class WorkerStore(Store):
        async def ensure_collection(self):
            pass

    monkeypatch.setattr(worker, "transition_execution", lambda *a, **kw: True)
    monkeypatch.setattr(worker, "_load_vector_store", lambda: lambda settings: WorkerStore())
    monkeypatch.setattr(indexer, "get_embedding_provider", lambda settings: Embedder())
    monkeypatch.setattr(
        worker,
        "save_indexed_version",
        lambda settings, user, doc, payload, chunks: saved.append((payload, chunks)),
    )
    worker.run_indexing(config, key, "u", "doc", 1, "exec")
    assert len(saved) == 1
    payload, chunks = saved[0]
    assert [chunk.locator.page for chunk in chunks] == [1, 2]
    assert json.loads(read_upload(config, payload.extracted_text_key)) == {
        "pages": [{"page": 1, "text": "First page"}, {"page": 2, "text": "Second page"}]
    }


def test_pdf_page_text_storage_preserves_blank_page(tmp_path):
    from app.services.storage import read_upload, save_extracted_pages

    settings = Settings(_env_file=None, local_storage_dir=tmp_path)
    parsed = parse_document(pdf_bytes("First", "", "Third"), file_type=".pdf")
    key = save_extracted_pages(settings, "u", "d", 1, parsed.pages)
    assert json.loads(read_upload(settings, key))["pages"] == [
        {"page": 1, "text": "First"},
        {"page": 2, "text": ""},
        {"page": 3, "text": "Third"},
    ]


def pdf_with_image(*, image_size, with_text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    image = DecodedStreamObject()
    image.set_data(bytes([0, 0, 0]))
    image.update(
        {NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image")}
    )
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
            NameObject("/XObject"): DictionaryObject(
                {NameObject("/Im1"): writer._add_object(image)}
            ),
        }
    )
    content = DecodedStreamObject()
    content.set_data(
        (
            f"q {image_size} 0 0 {image_size} 0 0 cm /Im1 Do Q "
            + ("BT /F1 12 Tf 72 720 Td (Experience) Tj ET" if with_text else "")
        ).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_scan_only_page_without_ocr_result_is_rejected_but_mixed_pages_are_accepted():
    # 纯图片页：图里没有可识别文字 → 全文无文字，仍拒绝
    with pytest.raises(DocumentParseError, match="未能提取到任何文字"):
        parse_document(pdf_with_image(image_size=50, with_text=False), file_type=".pdf")
    # 图文混合：文字保留，图片区域 OCR 无结果也不影响
    parsed = parse_document(pdf_with_image(image_size=300, with_text=True), file_type=".pdf")
    assert parsed.text == "Experience"
    # 小于 8pt 的装饰小图直接跳过，不做 OCR
    parsed_small = parse_document(pdf_with_image(image_size=5, with_text=True), file_type=".pdf")
    assert parsed_small.text == "Experience"


def test_vector_outline_mixed_with_extractable_text_is_accepted():
    writer = PdfWriter()
    writer.append(BytesIO(pdf_bytes("Experience")))
    page = writer.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Experience) Tj ET 0 0 10 10 re f")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    parsed = parse_document(output.getvalue(), file_type=".pdf")
    assert parsed.text == "Experience"


def test_image_hidden_in_nested_form_is_ocr_scanned_with_text_preserved():
    writer = PdfWriter()
    writer.append(BytesIO(pdf_bytes("Experience")))
    page = writer.pages[0]
    image = DecodedStreamObject()
    image.set_data(bytes([0, 0, 0]))
    image.update(
        {NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image")}
    )
    form = DecodedStreamObject()
    form.set_data(b"q 5 0 0 5 0 0 cm /Im1 Do Q")
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/Resources"): DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject("/Im1"): writer._add_object(image)}
                    )
                }
            ),
        }
    )
    page["/Resources"][NameObject("/XObject")] = DictionaryObject(
        {NameObject("/Fm1"): writer._add_object(form)}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 720 Td (Experience) Tj ET /Fm1 Do")
    page[NameObject("/Contents")] = writer._add_object(content)
    out = BytesIO()
    writer.write(out)
    parsed = parse_document(out.getvalue(), file_type=".pdf")
    assert parsed.text == "Experience"


def test_text_and_blank_pages_preserve_page_numbers():
    parsed = parse_document(pdf_bytes("First", "", "Third"), file_type=".pdf")
    assert parsed.pages == [["First"], [], ["Third"]]


def test_unknown_drawing_operator_does_not_block_text_extraction():
    writer = PdfWriter()
    writer.append(BytesIO(pdf_bytes("Experience")))
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 720 Td (Experience) Tj ET 0 0 FutureOperator")
    writer.pages[0][NameObject("/Contents")] = writer._add_object(content)
    out = BytesIO()
    writer.write(out)
    parsed = parse_document(out.getvalue(), file_type=".pdf")
    assert parsed.text == "Experience"


def _rendered_text_png(text: str, *, width: float = 480, height: float = 200) -> bytes:
    """把一行文字画进一张 PNG（用作 PDF 内嵌图片，模拟扫描件/截图）。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((24, height / 2), text, fontsize=30)
    png = page.get_pixmap(dpi=300).tobytes("png")
    doc.close()
    return png


def _scanned_pdf_bytes(*texts: str) -> bytes:
    """把文字页整页渲染成图片，生成无文本层的纯图片（扫描件）PDF。"""
    import pymupdf

    source = pymupdf.open(stream=pdf_bytes(*texts), filetype="pdf")
    output = pymupdf.open()
    for page in source:
        png = page.get_pixmap(dpi=180).tobytes("png")
        new_page = output.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=png)
    data = output.tobytes()
    source.close()
    output.close()
    return data


def test_scanned_pdf_page_text_is_recovered_by_ocr():
    parsed = parse_document(_scanned_pdf_bytes("Hello CareerPilot Resume"), file_type=".pdf")
    assert "careerpilot" in parsed.text.lower()
    assert parsed.pages and parsed.pages[0]


def test_mixed_page_merges_text_layer_and_image_ocr():
    import pymupdf

    doc = pymupdf.open(stream=pdf_bytes("Work Experience Section"), filetype="pdf")
    page = doc[0]
    png = _rendered_text_png("Skills Python Language")
    page.insert_image(pymupdf.Rect(72, 500, 528, 700), stream=png)
    data = doc.tobytes()
    doc.close()
    parsed = parse_document(data, file_type=".pdf")
    lowered = parsed.text.lower()
    assert "work experience section" in lowered
    assert "skills python language" in lowered


def test_full_page_image_matching_text_layer_is_not_duplicated():
    import pymupdf

    doc = pymupdf.open(stream=pdf_bytes("Hello CareerPilot Resume"), filetype="pdf")
    png = doc[0].get_pixmap(dpi=180).tobytes("png")
    doc[0].insert_image(doc[0].rect, stream=png)
    data = doc.tobytes()
    doc.close()
    parsed = parse_document(data, file_type=".pdf")
    assert parsed.lines.count("Hello CareerPilot Resume") == 1

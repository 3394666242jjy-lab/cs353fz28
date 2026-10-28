"""Real multi-page PDF parsing with deterministic, fake embedding/storage."""

import asyncio
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
    assert result.parser_version == "pypdf-text-pages-v1"


def test_pdf_without_text_and_encrypted_pdf_are_rejected():
    with pytest.raises(DocumentParseError, match="没有可提取的文本层"):
        parse_document(pdf_bytes(""), file_type=".pdf")
    with pytest.raises(DocumentParseError, match="加密"):
        parse_document(pdf_bytes("private", encrypted=True), file_type=".pdf")


def test_text_chunks_keep_original_indentation():
    from app.rag.chunking import chunk_document

    parsed = parse_document(b"  indented\nsecond line  ")
    chunk = chunk_document(parsed.lines, max_chars=500)[0]
    assert chunk.text == "  indented\nsecond line  "

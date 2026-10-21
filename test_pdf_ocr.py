"""remarkable_pdf_ocr: OCR text written into scanned PDF pages."""

import json
import shutil
from unittest.mock import patch

import pymupdf
import pytest

from remarkable_mcp import ocr, pdf_marks, pdf_ocr
from remarkable_mcp.server import mcp
from test_mark_pdf import A4, CONTENT, FakeCloud, _mark_tool  # noqa: F401


def _scanned_page(doc, text: str) -> None:
    """A page that is only an image of ``text``, like a scan."""
    source = pymupdf.open()
    page = source.new_page(width=A4[0], height=A4[1])
    page.insert_text((72, 120), text, fontsize=28)
    pix = page.get_pixmap(dpi=150)
    doc.new_page(width=A4[0], height=A4[1]).insert_image(
        pymupdf.Rect(0, 0, *A4), stream=pix.tobytes("png")
    )


def _book() -> bytes:
    """Page 1 typed text, pages 2-3 scanned, page 4 blank."""
    doc = pymupdf.open()
    doc.new_page(width=A4[0], height=A4[1]).insert_text((72, 100), "Typed title", fontsize=18)
    _scanned_page(doc, "Chapter One")
    _scanned_page(doc, "Chapter Two")
    doc.new_page(width=A4[0], height=A4[1])
    return doc.tobytes()


def _fake_words(png: bytes):
    """Pretend OCR: one word across the top of the page."""
    return [("Scanned", 100, 150, 400, 210), ("words", 420, 150, 600, 210)]


def test_scanned_pages_are_found():
    assert pdf_ocr.pages_needing_ocr(_book()) == {"scanned": [1, 2], "rotated": []}


def test_text_layer_is_invisible_and_searchable():
    doc = pymupdf.open(stream=_book(), filetype="pdf")
    before = doc[1].get_pixmap(dpi=50).samples
    pdf_ocr.add_text_layer(doc, 1, _fake_words(b""), 300 / 72)
    out = pymupdf.open("pdf", doc.tobytes())

    assert out[1].get_text().split() == ["Scanned", "words"]
    (hit,) = out[1].search_for("Scanned")
    assert hit.x0 == pytest.approx(100 * 72 / 300, abs=1)
    assert out[1].get_pixmap(dpi=50).samples == before


def test_work_resumes_after_the_time_budget(tmp_path):
    base = _book()
    ticks = iter([0, 0, 50, 0, 0, 0])
    first = pdf_ocr.run_ocr(
        "doc", base, [1, 2], _fake_words, budget=40, clock=lambda: next(ticks), directory=tmp_path
    )
    assert sorted(first) == [1]
    calls = []

    def counting(png):
        calls.append(1)
        return _fake_words(png)

    second = pdf_ocr.run_ocr("doc", base, [1, 2], counting, directory=tmp_path)
    assert sorted(second) == [1, 2] and len(calls) == 1
    pdf_ocr.clear_work("doc", directory=tmp_path)
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.skipif(
    not (shutil.which("tesseract") or shutil.which("/opt/homebrew/bin/tesseract")),
    reason="Tesseract is not installed",
)
def test_tesseract_reads_a_scanned_page():
    doc = pymupdf.open(stream=_book(), filetype="pdf")
    png, zoom = pdf_ocr._render(doc[1])
    words = ocr.tesseract_words(png)
    assert "Chapter" in [w[0] for w in words]
    pdf_ocr.add_text_layer(doc, 1, words, zoom)
    assert "Chapter One" in doc[1].get_text()


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    from remarkable_mcp.write_tools import register_write_tools

    for var in (
        "REMARKABLE_USE_SSH",
        "REMARKABLE_USE_USB_WEB",
        "REMARKABLE_READ_ONLY",
        "REMARKABLE_USE_LOCAL_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("remarkable_mcp.api.REMARKABLE_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(pdf_ocr, "ocr_backend", lambda: ("fake", _fake_words))
    saved = dict(mcp._tool_manager._tools)
    register_write_tools()
    content = dict(CONTENT, pages=["a", "b", "c", "d"], redirectionPageMap=[0, 1, 2, 3])
    fake = FakeCloud(_book(), content)
    try:
        with patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake):
            yield fake
    finally:
        mcp._tool_manager._tools.clear()
        mcp._tool_manager._tools.update(saved)


async def _ocr(**arguments):
    result = await mcp.call_tool("remarkable_pdf_ocr", arguments)
    return json.loads(result.content[0].text)


async def test_check_lists_pages_needing_ocr(cloud):
    data = await _ocr(document="Worksheet", check=True)

    assert data["needs_ocr"][0]["pages_needing_ocr"] == [2, 3]
    assert cloud.commits == 0


async def test_ocr_writes_every_scanned_page_and_keeps_marks(cloud):
    await _mark_tool(
        document="Worksheet", add=[{"page": 1, "id": "n", "text": "Hi", "at": [0.5, 0.5]}]
    )
    data = await _ocr(document="Worksheet")

    assert data["done"] and data["pages_ocred"] == [2, 3] and cloud.commits == 2
    pdf = cloud.stored(".pdf")
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert "Scanned words" in doc[1].get_text() and "Scanned words" in doc[2].get_text()
        assert doc[0].get_text().startswith("Typed title")
    assert [m["id"] for m in pdf_marks.stored_marks(pdf)] == ["n"]
    assert pdf_marks.MarkedPdf.load(pdf).ocr == {"backend": "fake", "pdf_pages": [1, 2]}

    again = await _ocr(document="Worksheet")
    assert again["done"] and not again["changed"] and cloud.commits == 2

    # Marks added later keep the OCR text.
    await _mark_tool(document="Worksheet", remove="n")
    with pymupdf.open(stream=cloud.stored(".pdf"), filetype="pdf") as doc:
        assert "Scanned words" in doc[1].get_text()


async def test_ocr_without_marks_does_not_embed_a_copy(cloud):
    size = len(cloud.stored(".pdf"))
    await _ocr(document="Worksheet")
    pdf = cloud.stored(".pdf")
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert pdf_marks.BASE_FILE not in doc.embfile_names()
    assert len(pdf) < size * 1.5
    assert pdf_marks.MarkedPdf.load(pdf).ocr["pdf_pages"] == [1, 2]


def test_ocr_only_pdf_exports_without_its_record():
    pdf = pdf_marks.record_ocr(_book(), {"backend": "fake", "pdf_pages": [1]})
    state = pdf_marks.MarkedPdf.load(pdf)
    assert state.marks == [] and state.ocr == {"backend": "fake", "pdf_pages": [1]}
    for out in (state.base, pdf_marks.for_export(pdf), pdf_marks.original_pdf(pdf)):
        with pymupdf.open(stream=out, filetype="pdf") as doc:
            assert doc.embfile_names() == [] and len(doc) == 4
    assert pdf_marks.MarkedPdf.load(pdf).base == state.base
    with pymupdf.open(stream=state.pdf(), filetype="pdf") as doc:
        assert doc.embfile_names() == [pdf_marks.MARKS_FILE]


async def test_folder_needs_check(cloud):
    data = await _ocr(folder="/")
    assert data["_error"]["type"] == "invalid_parameter"


def test_words_of_a_line_are_written_together():
    doc = pymupdf.open(stream=_book(), filetype="pdf")
    words = [
        ("waste", 100, 150, 200, 200, "1.1.1"),
        ("crown", 210, 150, 320, 200, "1.1.1"),
        ("land", 100, 220, 180, 270, "1.1.2"),
    ]
    assert pdf_ocr.add_text_layer(doc, 1, words, 300 / 72) == 2
    out = pymupdf.open("pdf", doc.tobytes())
    assert out[1].get_text().splitlines() == ["waste crown", "land"]
    assert out[1].search_for("waste crown")

"""remarkable_pdf_outline: a PDF's table of contents, set alongside its marks."""

import json

import pymupdf
import pytest

from remarkable_mcp import pdf_marks
from remarkable_mcp.pdf_marks import MarkError
from remarkable_mcp.server import mcp
from test_mark_pdf import A4, CONTENT, DOC_ID, FakeCloud, _mark_tool  # noqa: F401


def _book(pages=4, toc=None) -> bytes:
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        doc.new_page(width=A4[0], height=A4[1]).insert_text(
            (72, 100), f"Chapter {number}", fontsize=18
        )
    if toc:
        doc.set_toc(toc)
    return doc.tobytes()


@pytest.fixture
def cloud(monkeypatch):
    from unittest.mock import patch

    from remarkable_mcp.write_tools import register_write_tools

    for var in (
        "REMARKABLE_USE_SSH",
        "REMARKABLE_USE_USB_WEB",
        "REMARKABLE_READ_ONLY",
        "REMARKABLE_USE_LOCAL_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    tools = mcp._tool_manager._tools
    saved = dict(tools)
    register_write_tools()
    content = dict(CONTENT, pages=["a", "b", "c", "d"], redirectionPageMap=[0, 1, 2, 3])
    fake = FakeCloud(_book(toc=[[1, "Own entry", 2]]), content)
    try:
        with patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake):
            yield fake
    finally:
        tools.clear()
        tools.update(saved)


async def _outline_tool(**arguments):
    result = await mcp.call_tool("remarkable_pdf_outline", arguments)
    return json.loads(result.content[0].text)


def _toc(fake):
    with pymupdf.open(stream=fake.stored(".pdf"), filetype="pdf") as doc:
        return doc.get_toc(simple=True)


def test_check_outline_validates_entries():
    page_map = [0, None, 1]
    ok = pdf_marks.check_outline(
        [{"title": " Part I ", "page": 1}, {"title": "Ch 1", "page": 3, "level": 2}], page_map
    )
    assert ok == [
        {"level": 1, "title": "Part I", "pdf_page": 0},
        {"level": 2, "title": "Ch 1", "pdf_page": 1},
    ]
    for entries, message in [
        ("x", "must be a list"),
        ([{"page": 1}], "needs a title"),
        ([{"title": "a", "page": 9}], "does not exist"),
        ([{"title": "a", "page": 2}], "added on the tablet"),
        ([{"title": "a", "page": 1, "level": 2}], "one deeper"),
        ([{"title": "a", "page": 1, "extra": 1}], "unknown keys"),
    ]:
        with pytest.raises(MarkError, match=message):
            pdf_marks.check_outline(entries, page_map)


async def test_lists_the_pdfs_own_outline(cloud):
    data = await _outline_tool(document="Worksheet")

    assert data["source"] == "the PDF's own" and not data["changed"]
    assert data["outline"] == [
        {"level": 1, "title": "Own entry", "page": 2, "first_lines": ["Chapter 2"]}
    ]
    assert cloud.commits == 0


async def test_set_replace_remove_and_restore(cloud):
    original = cloud.stored(".pdf")
    entries = [
        {"title": "Chapter 1", "page": 1},
        {"title": "Scene", "page": 2, "level": 2},
        {"title": "Chapter 3", "page": 3},
    ]
    data = await _outline_tool(document="Worksheet", outline=entries, dry_run=True)
    assert data["changed"] and data["dry_run"] and cloud.commits == 0
    assert [e["first_lines"] for e in data["outline"]] == [
        ["Chapter 1"],
        ["Chapter 2"],
        ["Chapter 3"],
    ]

    data = await _outline_tool(document="Worksheet", outline=entries)
    assert cloud.commits == 1
    assert _toc(cloud) == [[1, "Chapter 1", 1], [2, "Scene", 2], [1, "Chapter 3", 3]]
    assert pdf_marks.original_pdf(cloud.stored(".pdf")) == original

    data = await _outline_tool(document="Worksheet", outline=entries)
    assert not data["changed"] and cloud.commits == 1

    await _outline_tool(document="Worksheet", outline=[])
    assert _toc(cloud) == []

    data = await _outline_tool(document="Worksheet", outline="original")
    assert data["source"] == "the PDF's own"
    assert cloud.stored(".pdf") == original


async def test_marks_and_outline_keep_each_other(cloud):
    await _outline_tool(document="Worksheet", outline=[{"title": "Start", "page": 1}])
    await _mark_tool(
        document="Worksheet", add=[{"page": 2, "id": "n", "text": "Hi", "at": [0.5, 0.5]}]
    )
    assert _toc(cloud) == [[1, "Start", 1]]
    assert [m["id"] for m in pdf_marks.stored_marks(cloud.stored(".pdf"))] == ["n"]

    await _outline_tool(document="Worksheet", outline=[{"title": "Again", "page": 3}])
    assert [m["id"] for m in pdf_marks.stored_marks(cloud.stored(".pdf"))] == ["n"]

    await _mark_tool(document="Worksheet", clear=True)
    assert _toc(cloud) == [[1, "Again", 3]]
    assert pdf_marks.stored_marks(cloud.stored(".pdf")) == []


def test_export_keeps_the_outline():
    base = _book()
    built = pdf_marks.build(base, [], outline=[{"level": 1, "title": "X", "pdf_page": 1}])
    exported = pdf_marks.for_export(built)
    with pymupdf.open(stream=exported, filetype="pdf") as doc:
        assert doc.get_toc(simple=True) == [[1, "X", 2]]
        assert doc.embfile_names() == []


async def test_rejects_bad_outline(cloud):
    data = await _outline_tool(document="Worksheet", outline="chapters")
    assert data["_error"]["type"] == "invalid_parameter"
    data = await _outline_tool(document="Worksheet", outline=[{"title": "a", "page": 7}])
    assert "does not exist" in data["_error"]["message"]
    assert cloud.commits == 0


def test_read_maps_outline_pages_to_tablet_pages():
    import io
    import zipfile

    from remarkable_mcp.tools import _outline_summaries

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "doc.content", json.dumps({"pages": ["a", "x", "b"], "redirectionPageMap": [0, -1, 1]})
        )
    entries = [
        {"level": 1, "title": "One", "pdf_page": 0},
        {"level": 1, "title": "Two", "pdf_page": 1},
        {"level": 1, "title": "Gone", "pdf_page": None},
    ]
    assert _outline_summaries(entries, buf.getvalue()) == [
        {"level": 1, "title": "One", "page": 1},
        {"level": 1, "title": "Two", "page": 3},
        {"level": 1, "title": "Gone", "page": None},
    ]


def test_page_openings_skip_running_headers_and_page_numbers():
    from remarkable_mcp.write_tools import _page_openings

    doc = pymupdf.open()
    for number in range(1, 5):
        page = doc.new_page(width=A4[0], height=A4[1])
        page.insert_text((72, 40), f"Erewhon {number * 10}", fontsize=9)
        page.insert_text((72, 90), f"CHAPTER {number}", fontsize=18)
        page.insert_text((72, 120), f"Title {number}", fontsize=12)
        page.insert_text((72, 150), "First sentence.", fontsize=11)
        page.insert_text((72, 180), "Second sentence.", fontsize=11)
        page.insert_text((300, 800), str(number), fontsize=9)
    openings = _page_openings(doc.tobytes(), range(4))

    assert openings[2] == ["CHAPTER 3", "Title 3", "First sentence."]

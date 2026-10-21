"""remarkable_pdf_pages: inserting PDF pages anywhere in a document."""

import json
from unittest.mock import patch

import pymupdf
import pytest

from remarkable_mcp import pdf_marks
from remarkable_mcp.pages import (
    _idx_between,
    content_page_ids,
    insert_pdf_pages,
    pdf_page_map,
)
from remarkable_mcp.server import mcp
from test_mark_pdf import A4, CONTENT, FakeCloud, _mark_tool  # noqa: F401

AUTHOR = "8efeea22-0545-4297-a2c6-c5b0e5e17c43"


def _v2(ids, added=()):
    """formatVersion 2 content; ``added`` positions are pages added on the tablet."""
    pages, pdf_index = [], 0
    for i, page_id in enumerate(ids):
        entry = {"id": page_id, "idx": {"timestamp": "1:2", "value": "b" + chr(97 + i)}}
        if i not in added:
            entry["redir"] = {"timestamp": "1:2", "value": pdf_index}
            pdf_index += 1
        pages.append(entry)
    return {
        "formatVersion": 2,
        "pageCount": len(ids),
        "cPages": {
            "original": {"timestamp": "1:1", "value": pdf_index},
            "pages": pages,
            "uuids": [{"first": "tablet", "second": 1}],
        },
    }


def test_idx_between_sorts_between_its_neighbours():
    for low, high in [("ba", "bb"), ("bb", "bc"), ("ba", None), ("", "ba"), ("bbn", "bc")]:
        key = _idx_between(low, high)
        assert low < key and (high is None or key < high)


def test_insert_v2_shifts_later_pdf_pages_and_sorts_between():
    content = _v2(["a", "b", "c", "d"])
    target = insert_pdf_pages(content, 2, ["n1", "n2"], AUTHOR)

    assert target == 2
    assert content_page_ids(content) == ["a", "b", "n1", "n2", "c", "d"]
    assert pdf_page_map(content) == [0, 1, 2, 3, 4, 5]
    keys = [p["idx"]["value"] for p in content["cPages"]["pages"]]
    assert keys == sorted(keys)
    assert {"first": AUTHOR, "second": 2} in content["cPages"]["uuids"]
    assert content["cPages"]["original"] == {"timestamp": "2:3", "value": 6}
    assert content["cPages"]["pages"][4]["redir"]["timestamp"] == "2:3"
    assert content["pageCount"] == 6


def test_insert_after_a_tablet_page_uses_the_previous_pdf_page():
    content = _v2(["a", "t", "b"], added={1})
    assert insert_pdf_pages(content, 2, ["n"], AUTHOR) == 1
    assert pdf_page_map(content) == [0, None, 1, 2]


def test_insert_v1_updates_map_counts_and_templates():
    content = {
        "formatVersion": 1,
        "pages": ["a", "b", "c"],
        "redirectionPageMap": [0, 1, 2],
        "pageCount": 3,
        "originalPageCount": 3,
    }
    templates = ["Blank", "Blank", "Blank", ""]
    assert insert_pdf_pages(content, 0, ["n"], AUTHOR, templates) == 0
    assert content["pages"] == ["n", "a", "b", "c"]
    assert content["redirectionPageMap"] == [0, 1, 2, 3]
    assert content["pageCount"] == 4 and content["originalPageCount"] == 4
    assert templates == ["Blank"] * 4 + [""]


def _book(pages=3) -> bytes:
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        doc.new_page(width=A4[0], height=A4[1]).insert_text((72, 100), f"Page {number}")
    return doc.tobytes()


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
    saved = dict(mcp._tool_manager._tools)
    register_write_tools()
    content = dict(CONTENT, pages=["a", "b", "c"], redirectionPageMap=[0, 1, 2], pageCount=3)
    fake = FakeCloud(_book(), content)
    try:
        with patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake):
            yield fake
    finally:
        mcp._tool_manager._tools.clear()
        mcp._tool_manager._tools.update(saved)


async def _pages(**arguments):
    result = await mcp.call_tool("remarkable_pdf_pages", arguments)
    return json.loads(result.content[0].text)


async def test_insert_keeps_marks_and_outline_on_their_pages(cloud):
    await _mark_tool(
        document="Worksheet", add=[{"page": 3, "id": "n", "text": "Hi", "at": [0.5, 0.5]}]
    )
    outline = [{"title": "Third", "page": 3}]
    await mcp.call_tool("remarkable_pdf_outline", {"document": "Worksheet", "outline": outline})

    data = await _pages(
        document="Worksheet",
        insert=[{"after": 1, "markdown": "# Feedback\n\nWell done."}, {"after": 0, "blank": 1}],
    )

    assert data["inserted_pages"] == [1, 3] and data["page_count"] == 5
    pdf = cloud.stored(".pdf")
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        texts = [page.get_text().split("\n")[0] for page in doc]
    assert texts == ["", "Page 1", "Feedback", "Page 2", "Page 3"]
    (mark,) = pdf_marks.stored_marks(pdf)
    assert mark["pdf_page"] == 4
    assert pdf_marks.MarkedPdf.load(pdf).outline == [{"level": 1, "title": "Third", "pdf_page": 4}]
    content = json.loads(cloud.stored(".content"))
    assert content["pageCount"] == 5 and content["redirectionPageMap"] == [0, 1, 2, 3, 4]
    assert content["pages"][1] == "a" and content["pages"][3:] == ["b", "c"]


async def test_dry_run_writes_nothing(cloud):
    before = cloud.stored(".pdf")
    data = await _pages(document="Worksheet", insert=[{"after": 3, "blank": 2}], dry_run=True)

    assert data["dry_run"] and data["inserted_pages"] == [4, 5]
    assert cloud.stored(".pdf") == before and cloud.commits == 0


async def test_pdf_file_pages_are_inserted(cloud, tmp_path):
    extra = tmp_path / "extra.pdf"
    extra.write_bytes(_book(2))
    data = await _pages(document="Worksheet", insert=[{"after": 3, "pdf_file": str(extra)}])

    assert data["inserted_pages"] == [4, 5]
    with pymupdf.open(stream=cloud.stored(".pdf"), filetype="pdf") as doc:
        assert len(doc) == 5


@pytest.mark.parametrize(
    "insert, message",
    [
        ([{"after": 9, "blank": 1}], "does not exist"),
        ([{"after": 1}], "exactly one"),
        ([{"after": 1, "blank": 0}], "1 to 100"),
        ([{"after": 1, "pdf_file": "/nope.pdf"}], "not found"),
        ([], "non-empty"),
    ],
)
async def test_bad_inserts_change_nothing(cloud, insert, message):
    data = await _pages(document="Worksheet", insert=insert)

    assert message in data["_error"]["message"]
    assert cloud.commits == 0

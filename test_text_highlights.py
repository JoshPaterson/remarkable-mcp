"""Text highlights written as tablet ink, so the user can erase them."""

import io
import uuid

import pymupdf
import pytest
from rmscene import read_blocks
from rmscene.scene_stream import SceneGlyphItemBlock

from remarkable_mcp import cover_layers as cl
from remarkable_mcp import pdf_marks
from remarkable_mcp import text_highlights as th
from test_mark_pdf import A4, CONTENT, FakeCloud, _mark_tool  # noqa: F401

AUTHOR = uuid.UUID("8efeea22-0545-4297-a2c6-c5b0e5e17c43")


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("remarkable_mcp.api.REMARKABLE_CONFIG_DIR", tmp_path)
    (tmp_path / cl.AUTHOR_FILE_NAME).write_text(f"{AUTHOR}\n")


def _page():
    doc = pymupdf.open()
    page = doc.new_page(width=A4[0], height=A4[1])
    page.insert_text((50, 100), "Title line", fontsize=14)
    page.insert_text((50, 140), "the quick brown fox jumps", fontsize=11)
    page.insert_text((50, 160), "over the lazy dog and the fox", fontsize=11)
    return pymupdf.open("pdf", doc.tobytes())


def _glyphs(data):
    return [b for b in read_blocks(io.BytesIO(data)) if isinstance(b, SceneGlyphItemBlock)]


def test_find_text_counts_offsets_as_the_tablet_does():
    page = _page()[0]
    (part,) = th.find_text(page, "quick brown")
    text = page.get_text()
    index = text.find("quick brown")
    assert part["start"] == index + text[:index].count("\n") and part["length"] == 11
    x0, y0, x1, y1 = part["rect"]
    loose = page.search_for("quick brown")[0]
    assert x0 < loose.x0 and x1 > loose.x1 and loose.y0 < y0 < y1 <= loose.y1


def test_find_text_splits_lines_and_picks_occurrences():
    page = _page()[0]
    parts = th.find_text(page, "fox jumps over the")
    assert [p["text"] for p in parts] == ["fox jumps", "over the"]
    assert parts[1]["start"] > parts[0]["start"] + parts[0]["length"]
    (second,) = th.find_text(page, "fox", occurrence=2)
    assert second["start"] > parts[1]["start"]
    with pytest.raises(th.HighlightError, match="only 2 times"):
        th.find_text(page, "fox", occurrence=3)


def test_highlights_go_on_the_selected_layer_or_a_named_one():
    page = _page()[0]
    parts = th.find_text(page, "lazy dog")
    data, ids = th.add_highlights(None, AUTHOR, A4, parts, "blue")
    (glyph,) = _glyphs(data)
    assert glyph.parent_id.part2 == 11  # Layer 1, which a new page selects
    assert glyph.item.value.text == "lazy dog" and glyph.item.value.color_rgba[:3] == (
        190,
        234,
        254,
    )
    assert th.highlight_status(data, ids) == "on tablet"

    data, more = th.add_highlights(data, AUTHOR, A4, parts, "yellow", layer="Claude highlights")
    names = [layer["name"] for layer in cl.page_layers(data)]
    assert names == ["Layer 1", "Claude highlights"]

    data = cl._tombstone(data, ids)
    assert th.highlight_status(data, ids) == "erased on tablet"
    assert th.highlight_status(data, more) == "on tablet"


@pytest.fixture
def cloud(monkeypatch):
    from unittest.mock import patch

    from remarkable_mcp.server import mcp
    from remarkable_mcp.write_tools import register_write_tools

    for var in (
        "REMARKABLE_USE_SSH",
        "REMARKABLE_USE_USB_WEB",
        "REMARKABLE_READ_ONLY",
        "REMARKABLE_USE_LOCAL_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    saved = dict(mcp._tool_manager._tools)
    register_write_tools()
    fake = FakeCloud(_page().tobytes(), dict(CONTENT, pages=["page-a"], redirectionPageMap=[0]))
    try:
        with patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake):
            yield fake
    finally:
        mcp._tool_manager._tools.clear()
        mcp._tool_manager._tools.update(saved)


async def test_highlight_mark_is_written_listed_and_removed(cloud):
    data, _ = await _mark_tool(
        document="Worksheet",
        add=[{"page": 1, "id": "hl", "text_highlight": "jumps over", "color": "green"}],
    )
    (mark,) = data["marks"]
    assert mark["kind"] == "text_highlight" and mark["text"] == "jumps over"
    assert mark["color"] == "green" and mark["state"] == "on tablet"
    ink = cloud.stored("page-a.rm")
    assert [g.item.value.text for g in _glyphs(ink)] == ["jumps", "over"]
    (stored,) = pdf_marks.stored_marks(cloud.stored(".pdf"))
    assert "pdf" not in stored and len(stored["highlight"]["items"]) == 2

    await _mark_tool(document="Worksheet", remove="hl")
    assert all(g.item.value is None for g in _glyphs(cloud.stored("page-a.rm")))


async def test_bad_highlight_requests(cloud):
    data, _ = await _mark_tool(
        document="Worksheet", add=[{"page": 1, "text_highlight": "not on the page"}]
    )
    assert "appears not at all" in data["_error"]["message"]
    data, _ = await _mark_tool(
        document="Worksheet", add=[{"page": 1, "text_highlight": "fox", "color": "red"}]
    )
    assert "highlighter" in data["_error"]["message"] or "color" in data["_error"]["message"]
    assert cloud.commits == 0

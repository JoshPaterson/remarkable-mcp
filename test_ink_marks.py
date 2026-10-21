"""Erasable marks: shapes, paths, leaders, highlights and text written as ink."""

import io
import uuid

import pymupdf
import pytest
from rmscene import read_blocks
from rmscene.scene_stream import SceneLineItemBlock

from remarkable_mcp import cover_layers as cl
from remarkable_mcp import ink_marks, pdf_marks
from test_mark_pdf import A4, CONTENT, FakeCloud, _mark_tool  # noqa: F401

AUTHOR = uuid.UUID("8efeea22-0545-4297-a2c6-c5b0e5e17c43")


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("remarkable_mcp.api.REMARKABLE_CONFIG_DIR", tmp_path)
    (tmp_path / cl.AUTHOR_FILE_NAME).write_text(f"{AUTHOR}\n")


def _lines(data):
    return [b for b in read_blocks(io.BytesIO(data)) if isinstance(b, SceneLineItemBlock)]


def test_text_path_lays_out_lines_and_wraps():
    data, width, height = ink_marks.text_path(["Hi", "there"], 12, False, False, False)
    assert data.startswith("M") and 0 < width < 40 and 20 < height < 30
    lines = ink_marks.wrap("one two three four five six", 12, False, 60)
    assert len(lines) > 1 and all(ink_marks._advance(line, 12, False) <= 60 for line in lines)


def test_bold_text_is_a_thicker_line():
    plain = ink_marks.text_as_path({"text": "Hi", "at": [0.1, 0.1]}, A4)
    bold = ink_marks.text_as_path({"text": "Hi", "at": [0.1, 0.1], "bold": True}, A4)
    assert bold["width"] == pytest.approx(plain["width"] * ink_marks.BOLD_WIDTH)
    assert plain["size"] > 0 and "region" not in plain


def test_strokes_come_from_the_rendered_mark():
    mark = pdf_marks.render_mark({"shape": "arrow", "from": [0.1, 0.1], "to": [0.3, 0.2]}, A4, None)
    strokes = ink_marks.part_strokes(mark, A4)
    points = [p for s in strokes for p in s["points"]]
    assert strokes and min(p[0] for p in points) == pytest.approx(0.1 * A4[0], abs=2)
    assert max(p[0] for p in points) == pytest.approx(0.3 * A4[0], abs=3)


def test_highlight_rows_fill_the_region():
    rows = ink_marks.highlight_strokes([0.1, 0.1, 0.5, 0.15], A4)
    total = sum(r["width"] for r in rows)
    assert total == pytest.approx(0.05 * A4[1]) and all(
        r["width"] <= ink_marks.MAX_HIGHLIGHT_ROW for r in rows
    )


@pytest.mark.parametrize(
    "request_, message",
    [
        ({"latex": "x", "at": [0.5, 0.5]}, "Only text, shape and path"),
        ({"path": "M 0 0 L 1 1", "at": [0.5, 0.5], "fill": "red"}, "cannot be filled"),
        ({"shape": "check", "at": [0.5, 0.5], "color": "#ff0000"}, "pen color"),
        ({"shape": "highlight", "region": [0.1, 0.1, 0.2, 0.2], "color": "red"}, "highlighter"),
    ],
)
def test_erasable_refuses_what_ink_cannot_draw(request_, message):
    with pytest.raises(pdf_marks.MarkError, match=message):
        ink_marks.erasable_request(dict(request_, erasable=True), None, A4)


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
    doc = pymupdf.open()
    doc.new_page(width=A4[0], height=A4[1]).insert_text((72, 100), "Answer: ____")
    fake = FakeCloud(doc.tobytes(), dict(CONTENT, pages=["page-a"], redirectionPageMap=[0]))
    try:
        with patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake):
            yield fake
    finally:
        mcp._tool_manager._tools.clear()
        mcp._tool_manager._tools.update(saved)


async def test_erasable_marks_are_ink_not_pdf(cloud):
    original = pdf_marks.original_pdf(cloud.stored(".pdf"))
    data, images = await _mark_tool(
        document="Worksheet",
        color="red",
        add=[
            {"page": 1, "id": "q1", "shape": "check", "at": [0.5, 0.12], "erasable": True},
            {"page": 1, "id": "n", "text": "Check the sign", "at": [0.55, 0.12], "erasable": True},
            {
                "page": 1,
                "id": "hl",
                "shape": "highlight",
                "region": [0.1, 0.1, 0.3, 0.13],
                "erasable": True,
                "layer": "Claude marks",
            },
        ],
        dry_run=True,
    )
    assert images and cloud.commits == 0

    data, _ = await _mark_tool(
        document="Worksheet",
        color="red",
        add=[
            {"page": 1, "id": "q1", "shape": "check", "at": [0.5, 0.12], "erasable": True},
            {"page": 1, "id": "n", "text": "Check the sign", "at": [0.55, 0.12], "erasable": True},
            {
                "page": 1,
                "id": "hl",
                "shape": "highlight",
                "region": [0.1, 0.1, 0.3, 0.13],
                "erasable": True,
                "layer": "Claude marks",
            },
        ],
    )
    by_id = {m["id"]: m for m in data["marks"]}
    assert by_id["n"]["kind"] == "text" and by_id["n"]["text"] == "Check the sign"
    assert all(m["erasable"] and m["state"] == "on tablet" for m in by_id.values())
    assert by_id["hl"]["layer"] == "Claude marks" and by_id["hl"]["color"] == "yellow"

    pdf = cloud.stored(".pdf")
    assert all("pdf" not in m for m in pdf_marks.stored_marks(pdf))
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert not doc[0].get_drawings()  # nothing drawn into the PDF
    ink = cloud.stored("page-a.rm")
    lines = _lines(ink)
    assert {int(b.item.value.color) for b in lines} == {7, 3}  # red pen, yellow highlighter
    assert [layer["name"] for layer in cl.page_layers(ink)] == ["Layer 1", "Claude marks"]

    note_items = next(
        m["ink"]["items"] for m in pdf_marks.stored_marks(cloud.stored(".pdf")) if m["id"] == "n"
    )
    await _mark_tool(document="Worksheet", remove=["n"])
    data, _ = await _mark_tool(document="Worksheet")
    assert {m["id"] for m in data["marks"]} == {"q1", "hl"}
    assert ink_marks.ink_status(cloud.stored("page-a.rm"), note_items) == "erased on tablet"
    assert pdf_marks.original_pdf(cloud.stored(".pdf")) == original


async def test_erasable_near_note_has_ink_leader(cloud):
    data, _ = await _mark_tool(
        document="Worksheet",
        color="red",
        add=[
            {
                "page": 1,
                "id": "far",
                "text": "see here",
                "near": [0.2, 0.12],
                "leader": True,
                "erasable": True,
            }
        ],
    )
    (mark,) = data["marks"]
    assert mark["erasable"] and mark["state"] == "on tablet"


def test_dots_are_drawn_as_small_filled_circles():
    mark = pdf_marks.render_mark(
        {"shape": "line", "from": [0.1, 0.1], "to": [0.3, 0.1], "dot": True}, A4, None
    )
    strokes = ink_marks.part_strokes(mark, A4)
    dot = [s for s in strokes if len(s["points"]) == 9]
    assert len(dot) == 1
    xs = [p[0] for p in dot[0]["points"]]
    assert max(xs) - min(xs) > 0.5  # a real loop, not a single point

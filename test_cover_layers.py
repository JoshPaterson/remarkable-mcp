"""Cover layers: white ink on a layer of its own hides marks on the tablet."""

import base64
import io
import uuid

import pytest
from rmscene import read_blocks
from rmscene.scene_stream import SceneInfo, SceneLineItemBlock

from remarkable_mcp import cover_layers as cl
from remarkable_mcp import ink_marks, pdf_marks, rm_ink
from remarkable_mcp.pdf_marks import MarkError
from remarkable_mcp.strokes import build_line_block
from test_mark_pdf import A4, FakeCloud, _mark_tool, cloud, original  # noqa: F401

AUTHOR = uuid.UUID("8efeea22-0545-4297-a2c6-c5b0e5e17c43")
TABLET = uuid.UUID("bd653915-0000-4000-8000-000000000001")


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("remarkable_mcp.api.REMARKABLE_CONFIG_DIR", tmp_path)
    (tmp_path / rm_ink.AUTHOR_FILE_NAME).write_text(f"{AUTHOR}\n")
    return tmp_path


def _blocks(data):
    return list(read_blocks(io.BytesIO(data)))


def _lines(data):
    return [b for b in _blocks(data) if isinstance(b, SceneLineItemBlock)]


def _tablet_page():
    """A page the tablet made: Layer 1 with one black stroke by the tablet."""
    data = rm_ink.first_page_rm(TABLET)
    stroke = {"points": [[0.2, 0.5], [0.4, 0.5]], "color": "black"}
    block = build_line_block(
        rm_ink.crdt(rm_ink.LAYER_1), rm_ink.crdt((1, 40)), stroke, *(v / 0.3177 for v in A4)
    )
    return data + rm_ink.encode([block])


def _rows():
    return [[[0.1, 0.1], [0.3, 0.1]], [[0.1, 0.11], [0.3, 0.11]]]


def test_layer_name():
    assert cl.layer_name(True) == "Mark cover"
    assert cl.layer_name(" Answer key ") == "Answer key"
    assert cl.layer_name(None) is None and cl.layer_name(False) is None
    for bad in ("", "x" * 41, 3):
        with pytest.raises(cl.CoverError):
            cl.layer_name(bad)


def test_author_id_is_made_once(tmp_path):
    first = rm_ink.author_uuid(tmp_path / "new")
    assert rm_ink.author_uuid(tmp_path / "new") == first
    assert rm_ink.known_author_uuid(tmp_path / "missing") is None


def test_first_ink_selects_layer_1_under_a_bottom_cover():
    data, placed = cl.edit_page(
        None, AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows(), "shown": False}]
    )
    layers = rm_ink.page_layers(data)
    assert [(layer["name"], layer["visible"]) for layer in layers] == [
        ("Mark cover", False),
        ("Layer 1", True),
    ]
    info = next(b for b in _blocks(data) if isinstance(b, SceneInfo))
    assert (info.current_layer.value.part1, info.current_layer.value.part2) == rm_ink.LAYER_1
    assert not any(t == rm_ink.TEXT_BLOCK for t, _ in rm_ink.raw_blocks(data))
    lines = _lines(data)
    assert len(lines) == 2 and all(int(b.item.value.color) == cl.WHITE for b in lines)
    assert placed[0]["layer"] == layers[0]["id"]
    assert cl.cover_status(data, placed[0]["strokes"]) == cl.SHOWN


def test_cover_on_a_tablet_page_keeps_its_ink_and_author():
    page = _tablet_page()
    data, placed = cl.edit_page(
        page, AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows(), "shown": True}]
    )
    number = rm_ink.author_number(data, AUTHOR)
    assert number == 2 and rm_ink.author_number(data, TABLET) == 1
    assert placed[0]["layer"][0] == number
    assert [layer["name"] for layer in rm_ink.page_layers(data)] == ["Mark cover", "Layer 1"]
    assert cl.cover_status(data, placed[0]["strokes"]) == cl.HIDDEN
    counters = [b.item.item_id.part2 for b in _lines(data)]
    assert len(set(counters)) == len(counters) and min(counters[1:]) > 40
    black = [b for b in _lines(data) if int(b.item.value.color) == 0]
    assert len(black) == 1


def test_layers_are_reused_by_name_and_strokes_removed_in_place():
    data, (first,) = cl.edit_page(
        _tablet_page(), AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows()}]
    )
    data, (second, answers) = cl.edit_page(
        data,
        AUTHOR,
        A4,
        add=[
            {"layer": "Mark cover", "rows": _rows()},
            {"layer": "Answer key", "rows": _rows(), "shown": True},
        ],
    )
    assert second["layer"] == first["layer"] and answers["layer"] != first["layer"]
    assert [layer["name"] for layer in rm_ink.page_layers(data)] == [
        "Answer key",
        "Mark cover",
        "Layer 1",
    ]
    size = len(_lines(data))
    data, _ = cl.edit_page(data, AUTHOR, A4, remove=first["strokes"])
    assert len(_lines(data)) == size
    assert cl.cover_status(data, first["strokes"]) == cl.ERASED
    assert cl.cover_status(data, second["strokes"]) == cl.SHOWN
    assert cl.cover_status(data, answers["strokes"]) == cl.HIDDEN


def test_user_strokes_on_a_cover_layer_are_counted():
    data, (placed,) = cl.edit_page(None, AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows()}])
    stroke = {"points": [[0.5, 0.5], [0.6, 0.5]], "color": "black"}
    data, _ = rm_ink.with_author(data, TABLET)
    block = build_line_block(
        rm_ink.crdt(placed["layer"]), rm_ink.crdt((2, 900)), stroke, *(v / 0.3177 for v in A4)
    )
    data += rm_ink.encode([block])
    assert cl.foreign_strokes_on_covers(data, AUTHOR) == 1


def _user_stroke_on(data, layer):
    data, _ = rm_ink.with_author(data, TABLET)
    stroke = {"points": [[0.5, 0.5], [0.6, 0.5]], "color": "black"}
    block = build_line_block(
        rm_ink.crdt(layer), rm_ink.crdt((9, 900)), stroke, *(v / 0.3177 for v in A4)
    )
    return data + rm_ink.encode([block])


def _select(data, layer):
    """``data`` with its SceneInfo selecting ``layer``."""
    import dataclasses

    from rmscene.tagged_block_common import LwwValue

    parts = [data[:43]]
    for btype, raw in rm_ink.raw_blocks(data):
        (block,) = _blocks(data[:43] + raw) or [None]
        if isinstance(block, SceneInfo):
            stamp = rm_ink.crdt((block.current_layer.timestamp.part1, 950))
            block = dataclasses.replace(block, current_layer=LwwValue(stamp, rm_ink.crdt(layer)))
            raw = rm_ink.encode([block])
        parts.append(raw)
    return b"".join(parts)


def _erasable_ink_layer():
    """A page with the MCP's erasable ink on its own layer, and its id."""
    strokes = [{"points": [(100, 300), (200, 300)], "width": 1.5}]
    data, (stroke,) = ink_marks.add_strokes(
        None, AUTHOR, A4, strokes, "fineliner", "red", layer="Claude marks"
    )
    return data, rm_ink.item_states(data, [stroke])[tuple(stroke)][0]


def test_user_strokes_on_an_ink_layer_are_not_counted_as_covered():
    data, layer = _erasable_ink_layer()
    data = _user_stroke_on(data, layer)
    assert cl.foreign_strokes_on_covers(data, AUTHOR) == 0


def test_cover_layers_are_known_from_stored_ids_after_their_ink_is_erased():
    data, (placed,) = cl.edit_page(None, AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows()}])
    data = _user_stroke_on(rm_ink.tombstone(data, placed["strokes"]), placed["layer"])
    assert cl.foreign_strokes_on_covers(data, AUTHOR) == 0
    assert cl.foreign_strokes_on_covers(data, AUTHOR, placed["strokes"]) == 1


def test_erasable_ink_skips_a_selected_cover_layer():
    data, (placed,) = cl.edit_page(None, AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows()}])
    data = _select(data, placed["layer"])
    assert rm_ink.selected_layer(data) == placed["layer"]
    strokes = [{"points": [(100, 300), (200, 300)], "width": 1.5}]
    data, (stroke,) = ink_marks.add_strokes(data, AUTHOR, A4, strokes, "fineliner", "red")
    assert rm_ink.item_states(data, [stroke])[tuple(stroke)][0] == rm_ink.LAYER_1


def test_ink_and_cover_layers_of_the_same_name_stay_apart():
    data, layer = _erasable_ink_layer()
    data, (placed,) = cl.edit_page(
        data, AUTHOR, A4, add=[{"layer": "Claude marks", "rows": _rows()}]
    )
    assert placed["layer"] != layer
    assert [x["name"] for x in rm_ink.page_layers(data)] == [
        "Claude marks",
        "Layer 1",
        "Claude marks",
    ]
    data, _ = cl.edit_page(None, AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows()}])
    strokes = [{"points": [(100, 300), (200, 300)], "width": 1.5}]
    with pytest.raises(rm_ink.InkError, match="cover layer"):
        ink_marks.add_strokes(data, AUTHOR, A4, strokes, "fineliner", "red", layer="Mark cover")


def test_shape_rows_cover_the_mark_not_its_box():
    mark = pdf_marks.render_mark({"text": "Check the sign", "at": [0.1, 0.1], "size": 12}, A4, None)
    rows = cl.shape_rows(base64.b64decode(mark["pdf"]), mark["box"], A4)
    x0, y0, x1, y1 = mark["box"]
    pad_x, pad_y = (cl.PAD_PT + 1) / A4[0], (cl.PAD_PT + 1) / A4[1]
    points = [p for row in rows for p in row]
    assert rows and all(x0 - pad_x <= x <= x1 + pad_x for x, _ in points)
    assert all(y0 - pad_y <= y <= y1 + pad_y for _, y in points)
    # Gaps between words stay uncovered: several runs share a row.
    by_row = {}
    for row in rows:
        by_row.setdefault(round(row[0][1], 5), []).append(row)
    assert max(len(v) for v in by_row.values()) > 1


def test_highlights_cannot_be_covered():
    with pytest.raises(MarkError, match="Highlights"):
        pdf_marks._cover({"shape": "highlight", "cover": True})
    with pytest.raises(MarkError, match="covered applies"):
        pdf_marks._cover({"text": "x", "covered": True})


async def test_cloud_cover_written_listed_and_removed(cloud):  # noqa: F811
    data, _ = await _mark_tool(
        document="Worksheet",
        add=[
            {"page": 1, "id": "q1-note", "text": "Check the units", "at": [0.4, 0.3]},
            {"page": 1, "id": "q2-note", "text": "Good", "at": [0.4, 0.4], "cover": True},
        ],
    )
    assert cloud.commits == 1
    covered = next(m for m in data["marks"] if m["id"] == "q2-note")
    assert covered["cover"] == {"layer": "Mark cover", "state": cl.SHOWN}
    assert "cover" not in next(m for m in data["marks"] if m["id"] == "q1-note")
    ink = cloud.stored("page-a.rm")
    assert [layer["name"] for layer in rm_ink.page_layers(ink)] == ["Mark cover", "Layer 1"]
    stored = {m["id"]: m for m in pdf_marks.stored_marks(cloud.stored(".pdf"))}
    strokes = stored["q2-note"]["cover"]["strokes"]
    assert strokes and "pending" not in stored["q2-note"]["cover"]
    assert cl.cover_status(ink, strokes) == cl.SHOWN

    data, _ = await _mark_tool(document="Worksheet")
    assert next(m for m in data["marks"] if m["id"] == "q2-note")["cover"]["state"] == cl.SHOWN

    data, _ = await _mark_tool(document="Worksheet", remove="q2-note")
    assert cloud.commits == 2
    assert cl.cover_status(cloud.stored("page-a.rm"), strokes) == cl.ERASED


async def test_cloud_covered_layer_starts_shown(cloud):  # noqa: F811
    data, _ = await _mark_tool(
        document="Worksheet",
        add=[
            {
                "page": 2,
                "id": "answer",
                "latex": "x = 4",
                "at": [0.4, 0.3],
                "cover": "Answer key",
                "covered": True,
            }
        ],
    )
    assert data["marks"][0]["cover"] == {"layer": "Answer key", "state": cl.HIDDEN}
    layers = rm_ink.page_layers(cloud.stored("page-b.rm"))
    assert [(layer["name"], layer["visible"]) for layer in layers] == [
        ("Answer key", True),
        ("Layer 1", True),
    ]


async def test_dry_run_with_cover_writes_no_ink(cloud):  # noqa: F811
    data, images = await _mark_tool(
        document="Worksheet",
        add=[{"page": 1, "id": "n", "text": "Hi", "at": [0.4, 0.3], "cover": True}],
        dry_run=True,
    )
    assert cloud.commits == 0 and images
    assert data["marks"][0]["cover"] == {"layer": "Mark cover"}


def test_read_lists_cover_state_from_the_archive():
    import json
    import zipfile

    from remarkable_mcp.tools import _mark_line, _mark_summaries

    mark = pdf_marks.render_mark({"text": "Good", "at": [0.4, 0.4]}, A4, None)
    mark.update(id="q2-note", pdf_page=0)
    data, (placed,) = cl.edit_page(
        None, AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows(), "shown": True}]
    )
    mark["cover"] = {"layer": "Mark cover", "strokes": [list(s) for s in placed["strokes"]]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc.content", json.dumps({"pages": ["page-a"]}))
        zf.writestr("doc/page-a.rm", data)
    (summary,) = _mark_summaries([mark], buf.getvalue())
    assert summary["cover"] == {"layer": "Mark cover", "state": cl.HIDDEN}
    assert _mark_line(summary).endswith("[cover 'Mark cover': hidden on tablet]")

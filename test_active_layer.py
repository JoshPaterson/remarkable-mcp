"""The MCP's writes keep the layer the user has selected on a page."""

import uuid
from pathlib import Path

from rmscene.scene_stream import SceneInfo

from remarkable_mcp import cover_layers as cl
from remarkable_mcp import notebooks, rm_ink
from remarkable_mcp import text_highlights as th

AUTHOR = uuid.UUID("8efeea22-0545-4297-a2c6-c5b0e5e17c43")
A4 = (595.0, 842.0)
TABLET = Path(__file__).parent / "fixtures" / "tablet"
SINGLE_LAYER_PAGE = (
    TABLET
    / "highlight_colors"
    / "5f4a9d73-f5ce-42d9-876d-706c7acd4d38"
    / "7586e252-4ca4-4a55-816a-6e483368d71a.rm"
)
LAYERS = TABLET / "layers_and_tags" / "d77fd0bf-0050-4971-899f-fa1a15d08f17"
# Layers 1-3, Layer 3 selected.
SELECTED_PAGE = LAYERS / "36b60bbc-6f99-4f02-9dda-f3e41dca1073.rm"
# Layers 1-3, selecting a layer that no longer exists.
STALE_PAGE = LAYERS / "3c640b7c-7c50-4bef-9ee6-29a647fc1594.rm"
LAYER_3 = (1, 51)
PARTS = [{"start": 10, "length": 5, "text": "hello", "rect": (100, 200, 140, 212)}]


def _cover(data, layer="Mark cover"):
    rows = [[[0.1, 0.1], [0.3, 0.1]]]
    out, _ = cl.edit_page(data, AUTHOR, A4, add=[{"layer": layer, "rows": rows, "shown": True}])
    return out


def _scene_info_raw(data):
    return [raw for btype, raw in rm_ink.raw_blocks(data) if btype == rm_ink.SCENE_INFO_BLOCK]


def _scene_info(data):
    return next(b for b in rm_ink.parse(data) if isinstance(b, SceneInfo))


def _counters(data, skip):
    blocks = rm_ink.parse(data)
    return [int(i.part2) for i in rm_ink.iter_crdt_ids(blocks) if rm_ink.key(i) != skip]


def test_cover_on_an_unselected_page_selects_layer_1():
    data = SINGLE_LAYER_PAGE.read_bytes()
    assert rm_ink.selected_layer(data) is None
    out = _cover(data)
    assert [layer["name"] for layer in rm_ink.page_layers(out)] == ["Mark cover", "Layer 1"]
    assert rm_ink.selected_layer(out) == rm_ink.LAYER_1
    stamp = rm_ink.key(_scene_info(out).current_layer.timestamp)
    assert stamp[0] == rm_ink.author_number(out, AUTHOR)
    assert stamp[1] > max(_counters(out, stamp))
    (old,), (new,) = _scene_info_raw(data), _scene_info_raw(out)
    assert new[4:8] == old[4:8] and len(new) == len(old)


def test_a_live_selection_is_kept_byte_for_byte():
    data = SELECTED_PAGE.read_bytes()
    selected = rm_ink.selected_layer(data)
    assert selected is not None
    out = _cover(data)
    assert out != data
    assert _scene_info_raw(out) == _scene_info_raw(data)
    assert rm_ink.selected_layer(out) == selected


def test_a_stale_selection_is_pinned_to_the_newest_user_layer():
    data = STALE_PAGE.read_bytes()
    assert rm_ink.selected_layer(data) is None
    assert [layer["id"] for layer in rm_ink.page_layers(data)][-1] == LAYER_3
    out = _cover(data)
    assert rm_ink.selected_layer(out) == LAYER_3
    old = _scene_info(data).current_layer.timestamp
    new = _scene_info(out).current_layer.timestamp
    assert new.part2 > old.part2


def test_a_named_highlight_layer_on_top_is_not_selected():
    data = SINGLE_LAYER_PAGE.read_bytes()
    out, _ = th.add_highlights(data, AUTHOR, A4, PARTS, "yellow", layer="Claude highlights")
    names = [layer["name"] for layer in rm_ink.page_layers(out)]
    assert names == ["Layer 1", "Claude highlights"]
    assert rm_ink.selected_layer(out) == rm_ink.LAYER_1


def test_a_selected_mcp_layer_is_kept():
    first = _cover(SINGLE_LAYER_PAGE.read_bytes())
    writer = rm_ink.PageWriter(first, AUTHOR)
    cover = writer.find_layer("Mark cover")
    number = rm_ink.author_number(first, AUTHOR)
    chosen = rm_ink.select_layer(first, cover, number, rm_ink.next_counter(rm_ink.parse(first)))
    assert rm_ink.selected_layer(chosen) == cover
    out = _cover(chosen, layer="Other cover")
    assert out != chosen
    assert _scene_info_raw(out) == _scene_info_raw(chosen)
    assert rm_ink.selected_layer(out) == cover


def test_hiding_a_layer_keeps_a_live_selection():
    data = SELECTED_PAGE.read_bytes()
    writer = rm_ink.PageWriter(data, AUTHOR)
    assert writer.set_layer_visible(writer.find_layer("Layer 1"), False)
    assert _scene_info_raw(writer.data) == _scene_info_raw(data)


def test_no_change_leaves_the_page_as_it_was():
    data = SINGLE_LAYER_PAGE.read_bytes()
    writer = rm_ink.PageWriter(data, AUTHOR)
    assert not writer.set_layer_visible(rm_ink.LAYER_1, True)
    writer.tombstone([])
    assert writer.data is data


def test_reading_the_data_twice_gives_the_same_file():
    writer = rm_ink.PageWriter(SINGLE_LAYER_PAGE.read_bytes(), AUTHOR)
    writer.add_layer("Mark cover", True)
    assert writer.data == writer.data


def test_a_page_without_scene_info_gets_one_after_its_page_info():
    data = notebooks.blank_page_rm_bytes()
    assert not _scene_info_raw(data)
    out = _cover(data)
    types = [btype for btype, _ in rm_ink.raw_blocks(out)]
    assert types.count(rm_ink.SCENE_INFO_BLOCK) == 1
    assert types.index(rm_ink.SCENE_INFO_BLOCK) == types.index(rm_ink.PAGE_INFO_BLOCK) + 1
    assert rm_ink.selected_layer(out) == rm_ink.LAYER_1
    stamp = rm_ink.key(_scene_info(out).current_layer.timestamp)
    assert stamp[0] == rm_ink.author_number(out, AUTHOR)


def test_a_first_ink_file_keeps_its_selection():
    out = _cover(None)
    assert _scene_info_raw(out) == _scene_info_raw(rm_ink.first_page_rm(AUTHOR))


def test_ink_on_an_unselected_page_goes_on_the_layer_it_selects():
    page = rm_ink.PageWriter(STALE_PAGE.read_bytes(), AUTHOR)
    layer = page.ink_layer(None)
    page.add_lines(layer, [{"points": [[0.1, 0.5], [0.3, 0.5]], "width": 1.0}], A4, "fineliner", 0)
    assert layer == LAYER_3
    assert rm_ink.selected_layer(page.data) == LAYER_3

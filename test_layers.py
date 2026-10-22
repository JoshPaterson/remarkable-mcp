"""remarkable_layers: show and hide page layers."""

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from rmscene.scene_stream import TreeNodeBlock

from remarkable_mcp import cover_layers as cl
from remarkable_mcp import rm_ink, tablet_json
from remarkable_mcp.server import mcp
from remarkable_mcp.sync import _hash_entries, _serialize_doc_index
from test_mark_pdf import DOC_ID, FakeCloud, _mark_tool, cloud, original  # noqa: F401

AUTHOR = uuid.UUID("8efeea22-0545-4297-a2c6-c5b0e5e17c43")
TABLET = Path(__file__).parent / "fixtures" / "tablet"
HIDDEN_LAYER_PAGE = (
    TABLET
    / "layers_and_tags"
    / "d77fd0bf-0050-4971-899f-fa1a15d08f17"
    / "36b60bbc-6f99-4f02-9dda-f3e41dca1073.rm"
)


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("remarkable_mcp.api.REMARKABLE_CONFIG_DIR", tmp_path)
    (tmp_path / rm_ink.AUTHOR_FILE_NAME).write_text(f"{AUTHOR}\n")
    return tmp_path


def _visible(data):
    return {layer["name"]: layer["visible"] for layer in rm_ink.page_layers(data)}


def _node(data, layer):
    for block in rm_ink.parse(data):
        if isinstance(block, TreeNodeBlock) and rm_ink.key(block.group.node_id) == layer:
            return block
    return None


# --- rm_ink -------------------------------------------------------------------


def test_showing_a_hidden_tablet_layer():
    data = HIDDEN_LAYER_PAGE.read_bytes()
    assert _visible(data) == {"Layer 1": True, "Layer 2": True, "Layer 3": False}
    writer = rm_ink.PageWriter(data, AUTHOR)
    layer = writer.find_layer("Layer 3")
    old = _node(data, layer).group.visible.timestamp
    assert writer.set_layer_visible(layer, True)
    assert _visible(writer.data) == {"Layer 1": True, "Layer 2": True, "Layer 3": True}
    stamp = _node(writer.data, layer).group.visible.timestamp
    number = rm_ink.author_number(writer.data, AUTHOR)
    assert stamp.part1 == number and stamp.part2 > old.part2
    assert stamp.part2 == rm_ink.next_counter(rm_ink.parse(data))


def test_already_in_state_writes_nothing():
    data = HIDDEN_LAYER_PAGE.read_bytes()
    writer = rm_ink.PageWriter(data, AUTHOR)
    assert not writer.set_layer_visible(writer.find_layer("Layer 1"), True)
    assert not writer.set_layer_visible(writer.find_layer("Layer 3"), False)
    assert writer.data == data


def test_toggle_rewrites_only_the_node_block():
    """Every other block is kept byte for byte (the author table gains the
    MCP); the node block keeps its header versions, id and label."""
    data = HIDDEN_LAYER_PAGE.read_bytes()
    writer = rm_ink.PageWriter(data, AUTHOR)
    layer = writer.find_layer("Layer 2")
    writer.set_layer_visible(layer, False)
    before, after = rm_ink.raw_blocks(data), rm_ink.raw_blocks(writer.data)
    assert [t for t, _ in before] == [t for t, _ in after]
    for (btype, old), (_, new) in zip(before, after):
        if btype == rm_ink.AUTHORS_BLOCK:
            continue
        node = _node(rm_ink._header() + old, layer) if btype == rm_ink.NODE_BLOCK else None
        if node is None:
            assert old == new
            continue
        assert new[4:8] == old[4:8]
        changed = rm_ink._parse_one(new).group
        assert changed.label == node.group.label and changed.node_id == node.group.node_id
        assert changed.visible.value is False


def test_toggle_back_and_forth_stays_newest():
    data = HIDDEN_LAYER_PAGE.read_bytes()
    writer = rm_ink.PageWriter(data, AUTHOR)
    layer = writer.find_layer("Layer 3")
    writer.set_layer_visible(layer, True)
    first = _node(writer.data, layer).group.visible.timestamp
    second = rm_ink.PageWriter(writer.data, AUTHOR)
    assert second.set_layer_visible(layer, False)
    stamp = _node(second.data, layer).group.visible.timestamp
    assert stamp.part2 > first.part2
    assert _visible(second.data)["Layer 3"] is False


def test_unknown_layer_is_an_ink_error():
    writer = rm_ink.PageWriter(HIDDEN_LAYER_PAGE.read_bytes(), AUTHOR)
    with pytest.raises(rm_ink.InkError):
        writer.set_layer_visible((9, 9), True)


def test_hiding_a_cover_layer_flips_the_cover_state():
    data, (placed,) = cl.edit_page(
        None,
        AUTHOR,
        (595.0, 842.0),
        add=[{"layer": "Answer key", "rows": [[[0.1, 0.1], [0.3, 0.1]]], "shown": True}],
    )
    assert cl.cover_status(data, placed["strokes"]) == cl.HIDDEN
    writer = rm_ink.PageWriter(data, AUTHOR)
    assert writer.set_layer_visible(placed["layer"], False)
    assert cl.cover_status(writer.data, placed["strokes"]) == cl.SHOWN


# --- The tool -----------------------------------------------------------------


async def _layers(**arguments):
    result = await mcp.call_tool("remarkable_layers", arguments)
    return json.loads(result.content[0].text)


def _cover_state(data, mark_id):
    return next(m for m in data["marks"] if m["id"] == mark_id)["cover"]["state"]


async def _two_covered_pages():
    await _mark_tool(
        document="Worksheet",
        add=[
            {"page": 1, "id": "a1", "text": "One", "at": [0.4, 0.3], "cover": True},
            {"page": 2, "id": "a2", "text": "Two", "at": [0.4, 0.3], "cover": True},
            {
                "page": 2,
                "id": "key",
                "text": "x = 4",
                "at": [0.4, 0.5],
                "cover": "Answer key",
                "covered": True,
            },
        ],
    )


async def test_hide_reveals_a_covered_mark_and_show_covers_it(cloud):  # noqa: F811
    await _two_covered_pages()
    commits = cloud.commits
    marks, _ = await _mark_tool(document="Worksheet")
    assert _cover_state(marks, "key") == cl.HIDDEN

    data = await _layers(document="Worksheet", hide="Answer key")
    assert data["changed"] == [{"page": 2, "layer": "Answer key", "visible": False}]
    assert data["unchanged"] == 0 and data["transport"] == "cloud"
    assert "not_found" not in data
    assert cloud.commits == commits + 1
    assert _visible(cloud.stored("page-b.rm"))["Answer key"] is False
    meta = json.loads(cloud.stored(".metadata"))
    assert meta["lastModified"] != "1"
    marks, _ = await _mark_tool(document="Worksheet")
    assert _cover_state(marks, "key") == cl.SHOWN

    data = await _layers(document="Worksheet", show=["Answer key"])
    assert data["changed"] == [{"page": 2, "layer": "Answer key", "visible": True}]
    marks, _ = await _mark_tool(document="Worksheet")
    assert _cover_state(marks, "key") == cl.HIDDEN


async def test_show_across_pages_and_pages_filter(cloud):  # noqa: F811
    await _two_covered_pages()
    data = await _layers(document="Worksheet", show="Mark cover", pages=1)
    assert data["changed"] == [{"page": 1, "layer": "Mark cover", "visible": True}]
    assert _visible(cloud.stored("page-b.rm"))["Mark cover"] is False

    data = await _layers(document="Worksheet", show="Mark cover", hide="Answer key")
    assert sorted(data["changed"], key=lambda c: c["layer"]) == [
        {"page": 2, "layer": "Answer key", "visible": False},
        {"page": 2, "layer": "Mark cover", "visible": True},
    ]
    assert data["unchanged"] == 1
    marks, _ = await _mark_tool(document="Worksheet")
    assert _cover_state(marks, "a1") == cl.HIDDEN
    assert _cover_state(marks, "a2") == cl.HIDDEN
    assert _cover_state(marks, "key") == cl.SHOWN


async def test_noop_writes_nothing(cloud):  # noqa: F811
    await _two_covered_pages()
    commits = cloud.commits
    data = await _layers(document="Worksheet", show="Layer 1", hide="Mark cover", pages="1-2")
    assert data["changed"] == [] and data["unchanged"] == 4
    assert cloud.commits == commits


async def test_not_found_names(cloud):  # noqa: F811
    await _two_covered_pages()
    commits = cloud.commits
    data = await _layers(document="Worksheet", hide=["answer key", "Mark cover"])
    assert data["not_found"] == ["answer key"]
    assert data["did_you_mean"] == ["Answer key"]

    data = await _layers(document="Worksheet", hide="Answer keys")
    assert data["_error"]["type"] == "layer_not_found"
    assert "Answer key" in data["_error"]["did_you_mean"]
    data = await _layers(document="Worksheet", hide="Answer key", pages=[1])
    assert data["_error"]["type"] == "layer_not_found"
    assert cloud.commits == commits


async def test_invalid_requests(cloud):  # noqa: F811
    await _two_covered_pages()
    commits = cloud.commits
    data = await _layers(document="Worksheet", show="Mark cover", hide=["Mark cover"])
    assert data["_error"]["type"] == "invalid_parameter"
    data = await _layers(document="Worksheet")
    assert data["_error"]["type"] == "invalid_parameter"
    data = await _layers(document="Worksheet", hide="Mark cover", pages="2-x")
    assert data["_error"]["type"] == "invalid_parameter"
    data = await _layers(document="Worksheet", hide="Mark cover", pages=3)
    assert data["_error"]["type"] == "invalid_parameter"
    assert "outside the document" in data["_error"]["message"]
    data = await _layers(document="Course", hide="Mark cover")
    assert data["_error"]["type"] == "invalid_parameter"
    assert cloud.commits == commits


def _notebook_cloud():
    """A cloud holding one notebook: page-a with the tablet's hidden Layer 3,
    page-b without ink."""
    content = {"fileType": "notebook", "formatVersion": 1, "pages": ["page-a", "page-b"]}
    fake = FakeCloud(b"", content=content)
    files = [
        fake._blob(tablet_json.dumps(content).encode(), f"{DOC_ID}.content"),
        fake._blob(
            tablet_json.dumps({"lastModified": "1", "visibleName": "Worksheet"}).encode(),
            f"{DOC_ID}.metadata",
        ),
        fake._blob(HIDDEN_LAYER_PAGE.read_bytes(), f"{DOC_ID}/page-a.rm"),
    ]
    doc_hash = _hash_entries(files)
    fake.blobs[doc_hash] = _serialize_doc_index(files)
    fake.entries = [{"hash": doc_hash, "type": "0", "id": DOC_ID, "subfiles": 3, "size": 1}]
    return fake


async def test_notebook_layers(cloud):  # noqa: F811
    notebook = _notebook_cloud()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=notebook):
        data = await _layers(document="Worksheet", show="Layer 3", hide="Layer 2")
    assert data["changed"] == [
        {"page": 1, "layer": "Layer 2", "visible": False},
        {"page": 1, "layer": "Layer 3", "visible": True},
    ]
    assert notebook.commits == 1
    assert _visible(notebook.stored("page-a.rm")) == {
        "Layer 1": True,
        "Layer 2": False,
        "Layer 3": True,
    }
    assert json.loads(notebook.stored(".content"))["fileType"] == "notebook"

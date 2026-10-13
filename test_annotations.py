"""Tests for annotations extracted from v6 .rm scene items."""

import io
import json
import uuid
import zipfile
from pathlib import Path

from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import (
    AuthorIdsBlock,
    MigrationInfoBlock,
    PageInfoBlock,
    SceneGlyphItemBlock,
    SceneGroupItemBlock,
    SceneLineItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
    write_blocks,
)
from rmscene.tagged_block_common import CrdtId, LwwValue

from remarkable_mcp.extract import (
    _render_rm_v6_to_svg,
    get_page_layers_from_document_zip,
    v6_page_layers,
)

ROOT_ID = CrdtId(0, 1)
HIGHLIGHTER = 18  # Pen.HIGHLIGHTER_2


def rgba(argb: int) -> tuple:
    """ARGB uint32 -> rmscene (r, g, b, a) tuple."""
    return ((argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF, (argb >> 24) & 0xFF)


def glyph(text: str, color: int, argb, y: float, x: float = 0.0) -> si.GlyphRange:
    return si.GlyphRange(
        start=None,
        length=len(text),
        text=text,
        color=si.PenColor(color),
        rectangles=[si.Rectangle(x=x, y=y, w=100.0, h=20.0)],
        color_rgba=rgba(argb) if argb is not None else None,
    )


def stroke(color: int, tool: int, points, argb=None) -> si.Line:
    return si.Line(
        color=si.PenColor(color),
        tool=si.Pen(tool),
        points=[
            si.Point(x=float(x), y=float(y), speed=0, direction=0, width=8, pressure=100)
            for x, y in points
        ],
        thickness_scale=1.0,
        starting_length=0.0,
        color_rgba=rgba(argb) if argb is not None else None,
    )


def build_rm(layers) -> bytes:
    """Build a v6 .rm page.

    ``layers`` is a list of ``(label, visible, items)``; items are GlyphRange or
    Line values placed on that layer.
    """
    counter = iter(range(10, 10_000))

    def next_id() -> CrdtId:
        return CrdtId(0, next(counter))

    blocks = [
        AuthorIdsBlock(author_uuids={1: uuid.UUID(int=1)}),
        MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
    ]
    node_ids = [next_id() for _ in layers]
    for node_id in node_ids:
        blocks.append(
            SceneTreeBlock(tree_id=node_id, node_id=CrdtId(0, 0), is_update=True, parent_id=ROOT_ID)
        )
    for node_id, (label, visible, _) in zip(node_ids, layers):
        blocks.append(
            TreeNodeBlock(
                group=si.Group(
                    node_id=node_id,
                    label=LwwValue(next_id(), label),
                    visible=LwwValue(next_id(), visible),
                )
            )
        )
    left = CrdtId(0, 0)
    for node_id in node_ids:
        item_id = next_id()
        blocks.append(
            SceneGroupItemBlock(
                parent_id=ROOT_ID,
                item=CrdtSequenceItem(
                    item_id=item_id,
                    left_id=left,
                    right_id=CrdtId(0, 0),
                    deleted_length=0,
                    value=node_id,
                ),
            )
        )
        left = item_id
    for node_id, (_, _, items) in zip(node_ids, layers):
        left = CrdtId(0, 0)
        for value in items:
            item_id = next_id()
            block_type = (
                SceneGlyphItemBlock if isinstance(value, si.GlyphRange) else SceneLineItemBlock
            )
            blocks.append(
                block_type(
                    parent_id=node_id,
                    item=CrdtSequenceItem(
                        item_id=item_id,
                        left_id=left,
                        right_id=CrdtId(0, 0),
                        deleted_length=0,
                        value=value,
                    ),
                )
            )
            left = item_id
    buf = io.BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()


def write_document_zip(path: Path, pages: dict, content: dict) -> Path:
    """Write a document archive with ``pages`` mapping page id -> .rm bytes."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("doc.content", json.dumps(content))
        for page_id, data in pages.items():
            zf.writestr(f"doc/{page_id}.rm", data)
    return path


# --- layers -----------------------------------------------------------------

BALLPOINT = 15  # Pen.BALLPOINT_2


def three_layer_page() -> bytes:
    """Like S0.2-06 page 3: layers 1 and 2 visible, layer 3 hidden."""
    return build_rm(
        [
            (
                "Layer 1",
                True,
                [
                    stroke(0, BALLPOINT, [(0, 100), (100, 100)]),
                    stroke(6, BALLPOINT, [(0, 200), (100, 200)]),
                    glyph("layer one passage", 3, 0xFF000000, y=500),
                ],
            ),
            ("Layer 2", True, [stroke(7, BALLPOINT, [(0, 300), (100, 300)])]),
            (
                "Layer 3",
                False,
                [
                    stroke(4, BALLPOINT, [(0, 400), (100, 400)]),
                    stroke(5, HIGHLIGHTER, [(0, 600), (100, 610)]),
                ],
            ),
        ]
    )


def test_unlabelled_layers_named_by_position(tmp_path):
    rm_file = tmp_path / "page.rm"
    rm_file.write_bytes(
        build_rm(
            [
                ("", True, [stroke(0, BALLPOINT, [(0, 0), (1, 1)])]),
                ("Notes", True, []),
                ("", True, []),
            ]
        )
    )

    names = [la["name"] for la in v6_page_layers(rm_file)]

    assert names == ["Layer 1", "Notes", "Layer 3"]


def test_render_draws_visible_layers_by_default(tmp_path):
    rm_file = tmp_path / "page.rm"
    rm_file.write_bytes(three_layer_page())

    visible = _render_rm_v6_to_svg(rm_file)
    everything = _render_rm_v6_to_svg(rm_file, layers="all")
    only_two = _render_rm_v6_to_svg(rm_file, layers="layer 2")

    green = 'stroke="#00A000"'
    red = 'stroke="#E00000"'
    assert red in visible and 'stroke="black"' in visible
    assert green not in visible
    assert 'stroke="#FF69B4"' not in visible  # highlighter on hidden layer
    assert green in everything
    assert red in only_two
    assert 'stroke="black"' not in only_two and green not in only_two


def test_get_page_layers_from_document_zip(tmp_path):
    zip_path = write_document_zip(
        tmp_path / "doc.zip",
        {"p3": three_layer_page()},
        {"cPages": {"pages": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]}},
    )

    assert get_page_layers_from_document_zip(zip_path, 3) == [
        {"name": "Layer 1", "visible": True},
        {"name": "Layer 2", "visible": True},
        {"name": "Layer 3", "visible": False},
    ]
    assert get_page_layers_from_document_zip(zip_path, 1) == []


async def _image(zip_bytes: bytes, **arguments) -> dict:
    from unittest.mock import Mock, patch

    from remarkable_mcp.server import mcp

    client = Mock()
    doc = Mock()
    doc.VissibleName = "Layers"
    doc.ID = "doc"
    doc.Parent = ""
    doc.ModifiedClient = None
    doc.is_folder = False
    doc.tags = []
    client.get_meta_items.return_value = [doc]
    client.download.return_value = zip_bytes
    with patch("remarkable_mcp.tools.get_rmapi", return_value=client):
        result = await mcp.call_tool(
            "remarkable_image",
            {"document": "Layers", "output_format": "svg", "compatibility": True, **arguments},
        )
    return json.loads(result.content[0].text)


async def test_image_layers_parameter(tmp_path):
    zip_bytes = write_document_zip(
        tmp_path / "doc.zip",
        {"p1": three_layer_page()},
        {"fileType": "notebook", "cPages": {"pages": [{"id": "p1"}]}},
    ).read_bytes()

    default = await _image(zip_bytes)
    everything = await _image(zip_bytes, layers="all")
    missing = await _image(zip_bytes, layers="Layer 9")

    assert 'stroke="#00A000"' not in default["svg"]
    assert 'stroke="#00A000"' in everything["svg"]
    assert missing["_error"]["type"] == "layer_not_found"
    assert "Layer 3" in missing["_error"]["suggestion"]

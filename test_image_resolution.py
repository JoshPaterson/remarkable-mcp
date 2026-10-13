"""remarkable_image dpi and region parameters."""

import base64
import io
import json
import uuid
import zipfile
from unittest.mock import Mock, patch

from PIL import Image
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import (
    AuthorIdsBlock,
    MigrationInfoBlock,
    PageInfoBlock,
    SceneGroupItemBlock,
    SceneLineItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
    write_blocks,
)
from rmscene.tagged_block_common import CrdtId

from remarkable_mcp.server import mcp

# 1404 stroke units * 0.3177 pt/unit = 446 pt = 6.2 in wide.
PAGE_WIDTH_IN = 1404 * 0.3177 / 72
PAGE_HEIGHT_IN = 1872 * 0.3177 / 72


def _page() -> bytes:
    root, layer = CrdtId(0, 1), CrdtId(0, 11)
    line = si.Line(
        color=si.PenColor.BLACK,
        tool=si.Pen.FINELINER_2,
        points=[
            si.Point(x=float(x), y=900.0, speed=0, direction=0, width=8, pressure=100)
            for x in (-200, 0, 200)
        ],
        thickness_scale=1.0,
        starting_length=0.0,
    )
    blocks = [
        AuthorIdsBlock(author_uuids={1: uuid.UUID(int=1)}),
        MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
        SceneTreeBlock(tree_id=layer, node_id=CrdtId(0, 0), is_update=True, parent_id=root),
        TreeNodeBlock(group=si.Group(node_id=layer)),
        SceneGroupItemBlock(
            parent_id=root,
            item=CrdtSequenceItem(CrdtId(0, 12), CrdtId(0, 0), CrdtId(0, 0), 0, layer),
        ),
        SceneLineItemBlock(
            parent_id=layer,
            item=CrdtSequenceItem(CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, line),
        ),
    ]
    buf = io.BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "doc.content",
            json.dumps({"fileType": "notebook", "cPages": {"pages": [{"id": "p1"}]}}),
        )
        zf.writestr("doc/p1.rm", _page())
    return buf.getvalue()


async def _image(**arguments) -> dict:
    client = Mock()
    doc = Mock()
    doc.VissibleName = "Notebook"
    doc.ID = "doc"
    doc.Parent = ""
    doc.ModifiedClient = None
    doc.is_folder = False
    doc.tags = []
    client.get_meta_items.return_value = [doc]
    client.download.return_value = _zip_bytes()
    with (
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools.download_raw_file", return_value=None),
    ):
        result = await mcp.call_tool(
            "remarkable_image", {"document": "Notebook", "compatibility": True, **arguments}
        )
    return json.loads(result.content[0].text)


def _size(data: dict) -> tuple:
    return Image.open(io.BytesIO(base64.b64decode(data["image_base64"]))).size


async def test_dpi_sizes_full_page_from_physical_extent():
    data = await _image(dpi=300)

    width, height = _size(data)
    assert abs(width - PAGE_WIDTH_IN * 300) <= 2
    assert abs(height - PAGE_HEIGHT_IN * 300) <= 2
    assert data["dpi"] == 300


async def test_region_crops_page_fractions():
    full = _size(await _image(dpi=200))
    data = await _image(dpi=200, region=[0.25, 0.5, 0.75, 1.0])

    width, height = _size(data)
    assert abs(width - full[0] / 2) <= 1
    assert abs(height - full[1] / 2) <= 1
    assert data["region"] == [0.25, 0.5, 0.75, 1.0]


async def test_invalid_dpi_and_region_rejected():
    assert (await _image(dpi=5000))["_error"]["type"] == "invalid_parameter"
    assert (await _image(region=[0.5, 0, 0.4, 1]))["_error"]["type"] == "invalid_parameter"
    assert (await _image(region=[0, 0, 1]))["_error"]["type"] == "invalid_parameter"
    svg = await _image(output_format="svg", dpi=300)
    assert svg["_error"]["type"] == "invalid_parameter"

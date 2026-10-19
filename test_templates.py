"""Notebook page templates: reported in reads and drawn under page content."""

import io
import json
import uuid
import zipfile
from unittest.mock import Mock, patch

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

from remarkable_mcp.extract import _render_rm_v6_to_svg, extract_text_from_document_zip
from remarkable_mcp.server import mcp
from remarkable_mcp.templates import content_page_templates, group_pages


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


def _content(file_type: str = "notebook") -> dict:
    return {
        "fileType": file_type,
        "cPages": {
            "pages": [
                {"id": "p1", "template": {"timestamp": "1:1", "value": "P Dots S"}},
                {"id": "p2", "template": {"timestamp": "1:2", "value": "P Dots S"}},
                {"id": "p3", "template": {"timestamp": "1:3", "value": "Blank"}},
                {"id": "p4", "template": {"timestamp": "1:4", "value": "P Lines medium"}},
            ]
        },
    }


def _write_document(tmp_path, file_type: str = "notebook"):
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc.content").write_text(json.dumps(_content(file_type)))
    rm_file = tmp_path / "doc" / "p1.rm"
    rm_file.write_bytes(_page())
    return rm_file


def test_content_page_templates():
    assert content_page_templates(_content()) == {
        "p1": "P Dots S",
        "p2": "P Dots S",
        "p3": "Blank",
        "p4": "P Lines medium",
    }
    assert content_page_templates({"pages": ["a"]}) == {}


def test_group_pages():
    assert group_pages([(2, "A"), (1, "A"), (3, "B"), (5, "B")]) == [
        (1, 2, "A"),
        (3, 3, "B"),
        (5, 5, "B"),
    ]


def _zip(tmp_path):
    zip_path = tmp_path / "doc.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("doc.content", json.dumps(_content()))
        zf.writestr("doc/p1.rm", _page())
    return zip_path


def test_extraction_reports_non_blank_templates(tmp_path):
    result = extract_text_from_document_zip(_zip(tmp_path))

    assert result["page_templates"] == [
        {"page": 1, "template": "P Dots S"},
        {"page": 2, "template": "P Dots S"},
        {"page": 4, "template": "P Lines medium"},
    ]


async def test_read_lists_page_templates(tmp_path):
    doc = Mock()
    doc.VissibleName = "Test notebook"
    doc.ID = "doc-templates"
    doc.Parent = ""
    doc.is_folder = False
    doc.tags = []
    doc.ModifiedClient = None
    client = Mock()
    client.get_meta_items.return_value = [doc]
    client.download.return_value = _zip(tmp_path).read_bytes()
    with (
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools.get_file_type", return_value="notebook"),
    ):
        result = await mcp.call_tool(
            "remarkable_read", {"document": "Test notebook", "content_type": "annotations"}
        )
    data = json.loads(result.content[0].text)

    assert "--- Page templates ---\nPages 1-2: P Dots S\nPage 4: P Lines medium" in data[
        "content"
    ].replace("\n\n", "\n")
    assert data["page_templates"][0] == {"page": 1, "template": "P Dots S"}


def test_render_does_not_draw_templates(tmp_path):
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc.content").write_text(json.dumps(_content()))
    rm_file = tmp_path / "doc" / "p1.rm"
    rm_file.write_bytes(_page())

    svg = _render_rm_v6_to_svg(rm_file)

    assert "<path" in svg
    assert "<circle" not in svg

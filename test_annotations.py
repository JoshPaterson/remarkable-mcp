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

from remarkable_mcp.extract import extract_text_from_document_zip

ROOT_ID = CrdtId(0, 1)
FINELINER = 17  # Pen.FINELINER_2


def rgba(argb: int) -> tuple:
    """ARGB uint32 -> rmscene (r, g, b, a) tuple."""
    return ((argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF, (argb >> 24) & 0xFF)


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


# --- remarkable_read --------------------------------------------------------


async def _read_annotations(extraction: dict) -> dict:
    from unittest.mock import Mock, patch

    from remarkable_mcp.server import mcp

    client = Mock()
    doc = Mock()
    doc.VissibleName = "Annotated PDF"
    doc.ID = "pdf-123"
    doc.Parent = ""
    doc.ModifiedClient = None
    doc.is_folder = False
    doc.tags = []
    client.get_meta_items.return_value = [doc]
    client.download.return_value = b"unused"
    with (
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools.get_file_type", return_value="pdf"),
        patch("remarkable_mcp.tools.extract_text_from_document_zip", return_value=extraction),
    ):
        result = await mcp.call_tool(
            "remarkable_read", {"document": "Annotated PDF", "content_type": "annotations"}
        )
    return json.loads(result.content[0].text)


# --- page tags --------------------------------------------------------------


def test_document_extraction_maps_page_tags_to_page_numbers(tmp_path):
    page = build_rm([("Layer 1", True, [stroke(0, FINELINER, [(0, 0), (10, 10)])])])
    zip_path = write_document_zip(
        tmp_path / "doc.zip",
        {"p1": page},
        {
            "cPages": {"pages": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]},
            "tags": [{"name": "test-doc", "timestamp": 1}],
            "pageTags": [
                {"name": "test-page", "pageId": "p3", "timestamp": 2},
                {"name": "review", "pageId": "p1", "timestamp": 3},
                {"name": "orphan", "pageId": "gone", "timestamp": 4},
            ],
        },
    )

    result = extract_text_from_document_zip(zip_path)

    assert result["page_tags"] == [
        {"page": 1, "name": "review"},
        {"page": 3, "name": "test-page"},
    ]
    by_page = {ap["page"]: ap for ap in result["annotated_pages"]}
    assert by_page[1]["page_tags"] == ["review"]
    assert by_page[1]["has_handwriting"] is True
    assert by_page[3]["page_tags"] == ["test-page"]
    assert by_page[3]["page_id"] == "p3"
    assert by_page[3]["has_handwriting"] is False


def test_page_tags_use_legacy_pages_order(tmp_path):
    zip_path = write_document_zip(
        tmp_path / "doc.zip",
        {},
        {"pages": ["a", "b"], "pageTags": [{"name": "t", "pageId": "b"}]},
    )

    assert extract_text_from_document_zip(zip_path)["page_tags"] == [{"page": 2, "name": "t"}]


async def test_read_annotations_shows_page_tags():
    data = await _read_annotations(
        {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": None,
            "pages": 3,
            "page_ids": ["p1", "p2", "p3"],
            "annotated_pages": [
                {
                    "page": 3,
                    "page_id": "p3",
                    "has_handwriting": True,
                    "highlights": [],
                    "page_tags": ["test-page"],
                }
            ],
            "ocr_backend": None,
            "tags": [{"name": "test-doc", "timestamp": 1}],
            "page_tags": [{"page": 3, "name": "test-page"}],
        }
    )

    assert "Page 3: handwritten notes; page tags: test-page" in data["content"]
    assert data["page_tags"] == [{"page": 3, "name": "test-page"}]
    assert data["tags"] == [{"name": "test-doc", "timestamp": 1}]

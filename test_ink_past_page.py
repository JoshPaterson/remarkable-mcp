"""Ink written below a page's paper_size is kept in image and export renders."""

import io
import json
import uuid
import zipfile

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

from remarkable_mcp.extract import (
    INK_EXTENSION_MARGIN,
    render_page_full_page_from_document_zip,
)

PAGE_W, PAGE_H = 1404, 1872


def _page(y_bottom: int) -> bytes:
    root, layer = CrdtId(0, 1), CrdtId(0, 11)
    line = si.Line(
        color=si.PenColor.BLACK,
        tool=si.Pen.FINELINER_2,
        points=[
            si.Point(x=0.0, y=float(y), speed=0, direction=0, width=8, pressure=100)
            for y in range(100, y_bottom + 1, 50)
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


def _zip(tmp_path, y_bottom: int):
    path = tmp_path / "doc.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("doc/p0.rm", _page(y_bottom))
        zf.writestr(
            "doc.content",
            json.dumps({"fileType": "notebook", "cPages": {"pages": [{"id": "p0"}]}}),
        )
    return path


def _ink_rows(png: bytes) -> tuple:
    image = Image.open(io.BytesIO(png)).convert("L")
    mid = image.width // 2
    rows = [
        y
        for y in range(image.height)
        if min(image.getpixel((x, y)) for x in range(mid - 5, mid + 5)) < 128
    ]
    return image.size, rows[0], rows[-1]


def test_page_extends_down_to_ink_below_it(tmp_path):
    png, (width, height) = render_page_full_page_from_document_zip(
        _zip(tmp_path, 4400), 1, background_color="#FFFFFF"
    )

    assert (width, height) == (PAGE_W, 4400 + INK_EXTENSION_MARGIN)
    size, top, bottom = _ink_rows(png)
    # Same scale as an ordinary page: one pixel per stroke unit at 1404 wide.
    assert size == (PAGE_W, 4400 + INK_EXTENSION_MARGIN)
    assert abs(top - 100) <= 3 and abs(bottom - 4400) <= 3


def test_page_within_paper_size_unchanged(tmp_path):
    png, frame = render_page_full_page_from_document_zip(
        _zip(tmp_path, 1500), 1, background_color="#FFFFFF"
    )

    assert frame == (PAGE_W, PAGE_H)
    assert _ink_rows(png)[0] == (PAGE_W, PAGE_H)


def test_canvas_frame_stays_at_paper_size(tmp_path):
    png, frame = render_page_full_page_from_document_zip(
        _zip(tmp_path, 4400), 1, background_color="#FFFFFF", extend_to_ink=False
    )

    assert frame == (PAGE_W, PAGE_H)
    assert _ink_rows(png)[0] == (PAGE_W, PAGE_H)


def test_export_page_holds_all_ink(tmp_path):
    import fitz

    from remarkable_mcp.exporters import ExportMetadata, write_archive_pdf_export

    metadata = ExportMetadata(
        document_id="doc",
        title="Long page",
        path="/Long page",
        source_type="notebook",
        page_count=1,
    )
    destination = tmp_path / "out.pdf"
    write_archive_pdf_export(_zip(tmp_path, 4400), destination, metadata)

    with fitz.open(destination) as pdf:
        rect = pdf[0].rect
    assert abs(rect.height / rect.width - (4400 + INK_EXTENSION_MARGIN) / PAGE_W) < 0.01

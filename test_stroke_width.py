"""Pen stroke widths in rendered SVG (issue #189)."""

import io
import re
import struct
import uuid

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

from remarkable_mcp.extract import _render_rm_v5_to_svg, _render_rm_v6_to_svg

ROOT_ID = CrdtId(0, 1)
LAYER_ID = CrdtId(0, 11)


def _line(tool: int, widths, y: float, color: int = 0) -> si.Line:
    return si.Line(
        color=si.PenColor(color),
        tool=si.Pen(tool),
        points=[
            si.Point(x=10.0 * i, y=y, speed=0, direction=0, width=w, pressure=100)
            for i, w in enumerate(widths)
        ],
        thickness_scale=1.0,
        starting_length=0.0,
    )


def _v6_page(tmp_path, lines) -> str:
    blocks = [
        AuthorIdsBlock(author_uuids={1: uuid.UUID(int=1)}),
        MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
        SceneTreeBlock(tree_id=LAYER_ID, node_id=CrdtId(0, 0), is_update=True, parent_id=ROOT_ID),
        TreeNodeBlock(group=si.Group(node_id=LAYER_ID)),
        SceneGroupItemBlock(
            parent_id=ROOT_ID,
            item=CrdtSequenceItem(CrdtId(0, 12), CrdtId(0, 0), CrdtId(0, 0), 0, LAYER_ID),
        ),
    ]
    left = CrdtId(0, 0)
    for i, line in enumerate(lines):
        item_id = CrdtId(0, 20 + i)
        blocks.append(
            SceneLineItemBlock(
                parent_id=LAYER_ID,
                item=CrdtSequenceItem(item_id, left, CrdtId(0, 0), 0, line),
            )
        )
        left = item_id
    buf = io.BytesIO()
    write_blocks(buf, blocks)
    rm_file = tmp_path / "page.rm"
    rm_file.write_bytes(buf.getvalue())
    return _render_rm_v6_to_svg(rm_file)


def _widths(svg: str) -> list:
    return [float(w) for w in re.findall(r'stroke-width="([0-9.]+)"', svg)]


FINELINER = 17
MARKER = 16
BALLPOINT = 15
HIGHLIGHTER = 18


def test_v6_point_width_is_quarter_page_units(tmp_path):
    svg = _v6_page(tmp_path, [_line(FINELINER, [8, 8, 8], y=100)])

    assert _widths(svg) == [2.0]


def test_v6_pens_of_different_sizes_render_differently(tmp_path):
    svg = _v6_page(
        tmp_path,
        [
            _line(FINELINER, [8, 8, 8], y=100),
            _line(FINELINER, [12, 12, 12], y=200),
            _line(MARKER, [40, 40, 40], y=300),
        ],
    )

    assert _widths(svg) == [2.0, 3.0, 10.0]


def test_v6_variable_width_stroke_keeps_width_changes(tmp_path):
    svg = _v6_page(tmp_path, [_line(BALLPOINT, [6, 6, 10, 10, 14], y=100)])

    assert _widths(svg) == [1.5, 2.5, 3.5]
    # Each piece starts where the previous one ended.
    assert svg.count("<path") == 3
    assert 'd="M 10.0 100.0 L 20.0 100.0 L 30.0 100.0"' in svg


def test_v6_highlighter_width_unchanged(tmp_path):
    svg = _v6_page(tmp_path, [_line(HIGHLIGHTER, [120, 120], y=100, color=3)])

    assert _widths(svg) == [40.0]
    assert 'opacity="0.35"' in svg


def test_v5_widths_are_page_units(tmp_path):
    def stroke(pen, width):
        header = struct.pack("<IIIIfI", pen, 0, 0, 0, 0.0, 2)
        segs = b"".join(struct.pack("<ffffff", x, 50.0, 0, 0, width, 0.5) for x in (0.0, 10.0))
        return header + segs

    data = b"reMarkable .lines file, version=5          "
    data += struct.pack("<I", 1) + struct.pack("<I", 2)
    data += stroke(17, 2.0) + stroke(17, 3.5)
    rm_file = tmp_path / "v5.rm"
    rm_file.write_bytes(data)

    assert _widths(_render_rm_v5_to_svg(rm_file)) == [2.0, 3.5]

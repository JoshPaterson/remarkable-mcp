"""Tests for highlight colors extracted from v6 .rm scene items."""

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

from remarkable_mcp.colors import color_info, render_color
from remarkable_mcp.exporters import ExportMetadata, write_markdown_export
from remarkable_mcp.extract import (
    _page_annotation_details,
    _render_rm_v6_to_svg,
    extract_text_from_document_zip,
)

ROOT_ID = CrdtId(0, 1)

# (label, color id, ARGB) as written by current firmware for each highlighter.
HIGHLIGHTER_PALETTE = [
    ("yellow", 3, 0xFF000000),
    ("green", 4, 0xFF000000),
    ("pink", 5, 0xFF000000),
    ("gray", 8, 0xFF000000),
    ("blue", 9, 0xFFBEEAFE),
    ("orange", 9, 0xFFFFC38C),
]


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


def palette_page(y0: float = 0.0) -> bytes:
    # Written in reverse so the result order must come from page position.
    items = [
        glyph(f"{name} passage", color, argb, y=y0 + 50.0 * i)
        for i, (name, color, argb) in enumerate(HIGHLIGHTER_PALETTE)
    ]
    return build_rm([("Layer 1", True, list(reversed(items)))])


# --- color naming -----------------------------------------------------------


def test_color_info_names_standard_ids_without_hex():
    assert color_info(3, rgba(0xFF000000)) == {
        "color": "yellow",
        "color_id": 3,
        "color_hex": None,
    }
    assert color_info(si.PenColor.PINK)["color"] == "pink"
    assert color_info(7)["color"] == "red"


def test_color_info_names_custom_highlighter_colors_from_argb():
    assert color_info(9, rgba(0xFFBEEAFE)) == {
        "color": "blue",
        "color_id": 9,
        "color_hex": "#beeafe",
    }
    assert color_info(9, rgba(0xFFFFC38C))["color"] == "orange"
    assert color_info(9, rgba(0xFF123456))["color"] == "#123456"
    assert color_info(9)["color"] == "highlight"


def test_render_color_uses_argb_only_for_custom_color():
    assert render_color(9, rgba(0xFFBEEAFE)) == "#beeafe"
    assert render_color(3, rgba(0xFF000000)) == "#FFD700"
    assert render_color(9) == "#FFD700"


# --- extraction -------------------------------------------------------------


def test_page_highlights_carry_colors_in_page_order(tmp_path):
    rm_file = tmp_path / "page.rm"
    rm_file.write_bytes(palette_page())

    details = _page_annotation_details(rm_file)

    assert [(h["text"], h["color"]) for h in details["highlights"]] == [
        (f"{name} passage", name) for name, _, _ in HIGHLIGHTER_PALETTE
    ]
    assert details["highlights"][4]["color_hex"] == "#beeafe"
    assert details["highlights"][5]["color_hex"] == "#ffc38c"
    assert details["has_strokes"] is False


def test_document_extraction_reports_highlight_details_per_page(tmp_path):
    zip_path = write_document_zip(
        tmp_path / "doc.zip",
        {"p1": palette_page(), "p2": palette_page()},
        {"cPages": {"pages": [{"id": "p1"}, {"id": "p2"}]}},
    )

    result = extract_text_from_document_zip(zip_path)

    assert result["highlights"][:2] == ["yellow passage", "green passage"]
    first_page = result["annotated_pages"][0]
    assert first_page["highlights"][0] == "yellow passage"
    assert first_page["highlight_details"][4]["color"] == "blue"
    assert [h["page"] for h in result["highlight_details"]] == [1] * 6 + [2] * 6
    assert result["highlight_details"][5]["color"] == "orange"


def test_svg_render_uses_argb_for_custom_highlights(tmp_path):
    rm_file = tmp_path / "page.rm"
    rm_file.write_bytes(palette_page())

    svg = _render_rm_v6_to_svg(rm_file)

    assert 'fill="#beeafe"' in svg
    assert 'fill="#ffc38c"' in svg
    assert 'fill="#FF69B4"' in svg


def test_markdown_export_labels_highlight_colors(tmp_path):
    extraction = {
        "highlights": ["yellow passage", "legacy passage"],
        "highlight_details": [
            {"page": 2, "text": "yellow passage", "color": "yellow", "color_id": 3}
        ],
        "annotated_pages": [
            {
                "page": 2,
                "page_id": "p2",
                "has_handwriting": False,
                "highlights": ["yellow passage"],
                "highlight_details": [{"text": "yellow passage", "color": "yellow"}],
            }
        ],
    }
    destination = tmp_path / "out.md"
    write_markdown_export(
        destination,
        ExportMetadata(
            document_id="doc",
            title="Doc",
            path="/Doc",
            source_type="pdf",
            page_count=2,
            modified=None,
        ),
        source_text=None,
        extraction=extraction,
        include_ocr=False,
    )
    text = destination.read_text()

    assert "Physical page 2; page ID `p2`: 1 highlight (yellow)" in text
    assert "### Highlight 1 (yellow, page 2)" in text
    assert "### Highlight 2\n" in text


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


async def test_read_annotations_prefixes_highlights_with_color():
    details = [
        {"text": "Sidereal month", "color": "yellow", "color_id": 3, "color_hex": None},
        {"text": "Apsidal period", "color": "pink", "color_id": 5, "color_hex": None},
    ]
    data = await _read_annotations(
        {
            "typed_text": [],
            "highlights": [d["text"] for d in details],
            "highlight_details": [{"page": 1, **d} for d in details],
            "handwritten_text": None,
            "pages": 1,
            "page_ids": ["p1"],
            "annotated_pages": [
                {
                    "page": 1,
                    "page_id": "p1",
                    "has_handwriting": False,
                    "highlights": [d["text"] for d in details],
                    "highlight_details": details,
                }
            ],
            "ocr_backend": None,
            "tags": [],
        }
    )

    assert "Page 1: 2 highlights" in data["content"]
    assert "  • [yellow] Sidereal month" in data["content"]
    assert "  • [pink] Apsidal period" in data["content"]
    assert [(h["page"], h["color"]) for h in data["highlights"]] == [(1, "yellow"), (1, "pink")]


# --- highlighter strokes ----------------------------------------------------

HIGHLIGHTER = 18  # Pen.HIGHLIGHTER_2
FINELINER = 17  # Pen.FINELINER_2
ERASER = 6


def test_highlighter_strokes_reported_with_color_and_bbox(tmp_path):
    rm_file = tmp_path / "page.rm"
    rm_file.write_bytes(
        build_rm(
            [
                (
                    "Layer 1",
                    True,
                    [
                        stroke(3, HIGHLIGHTER, [(10, 400), (200, 410)]),
                        stroke(5, HIGHLIGHTER, [(-50, 100), (150, 120)]),
                        stroke(9, HIGHLIGHTER, [(0, 700), (90, 705)], argb=0xFFFFC38C),
                        stroke(6, ERASER, [(0, 0), (5, 5)]),
                    ],
                )
            ]
        )
    )

    details = _page_annotation_details(rm_file)

    assert [h["color"] for h in details["highlighter_strokes"]] == ["pink", "yellow", "orange"]
    # Padded by half the pen width (point width 8 -> 2 stroke units).
    assert details["highlighter_strokes"][0]["bbox"] == [-51.0, 99.0, 151.0, 121.0]
    assert details["has_strokes"] is False


def test_pen_strokes_stay_handwriting(tmp_path):
    rm_file = tmp_path / "page.rm"
    rm_file.write_bytes(build_rm([("Layer 1", True, [stroke(0, FINELINER, [(0, 0), (10, 10)])])]))

    details = _page_annotation_details(rm_file)

    assert details["has_strokes"] is True
    assert details["highlighter_strokes"] == []


def test_document_extraction_indexes_highlighter_only_pages(tmp_path):
    page = build_rm([("Layer 1", True, [stroke(5, HIGHLIGHTER, [(0, 10), (50, 12)])])])
    zip_path = write_document_zip(
        tmp_path / "doc.zip",
        {"p2": page},
        {"cPages": {"pages": [{"id": "p1"}, {"id": "p2"}]}},
    )

    result = extract_text_from_document_zip(zip_path)

    assert [ap["page"] for ap in result["annotated_pages"]] == [2]
    assert result["annotated_pages"][0]["has_handwriting"] is False
    stroke_entry = result["highlighter_strokes"][0]
    assert stroke_entry["page"] == 2
    assert stroke_entry["color"] == "pink"
    assert "bbox" not in stroke_entry
    # x is centered on the 1404-unit page; 0.3177 pt per unit.
    assert stroke_entry["region"] == [0.499, 0.005, 0.536, 0.007]
    assert stroke_entry["bbox_mm"] == [78.6, 1.0, 84.4, 1.5]


async def test_read_annotations_summarizes_highlighter_strokes():
    strokes = [
        {"color": "pink", "color_id": 5, "color_hex": None, "region": [0.1, 0.2, 0.5, 0.25]},
        {"color": "yellow", "color_id": 3, "color_hex": None, "region": [0.1, 0.4, 0.4, 0.45]},
    ]
    data = await _read_annotations(
        {
            "typed_text": [],
            "highlights": [],
            "highlight_details": [],
            "highlighter_strokes": [{"page": 2, **h} for h in strokes],
            "handwritten_text": None,
            "pages": 2,
            "page_ids": ["p1", "p2"],
            "annotated_pages": [
                {
                    "page": 2,
                    "page_id": "p2",
                    "has_handwriting": True,
                    "highlights": [],
                    "highlight_details": [],
                    "highlighter_strokes": strokes,
                }
            ],
            "ocr_backend": None,
            "tags": [],
        }
    )

    assert "Page 2: handwritten notes, 2 highlighter strokes (pink, yellow)" in data["content"]
    assert "  ~ [yellow] highlighter stroke, region [0.1, 0.4, 0.4, 0.45]" in data["content"]
    assert [h["color"] for h in data["highlighter_strokes"]] == ["pink", "yellow"]


# --- highlight positions ----------------------------------------------------


def _two_line_glyph() -> si.GlyphRange:
    text = "a highlight that wraps onto a second line"
    return si.GlyphRange(
        start=None,
        length=len(text),
        text=text,
        color=si.PenColor(5),
        rectangles=[
            si.Rectangle(x=100.0, y=500.0, w=300.0, h=24.0),
            si.Rectangle(x=-600.0, y=530.0, w=110.0, h=24.0),
        ],
    )


def test_highlight_positions_per_piece_and_overall(tmp_path):
    zip_path = write_document_zip(
        tmp_path / "doc.zip",
        {"p1": build_rm([("Layer 1", True, [_two_line_glyph()])])},
        {"pages": ["p1"]},
    )

    highlight = extract_text_from_document_zip(zip_path)["highlight_details"][0]

    assert "rects" not in highlight
    assert len(highlight["rects_mm"]) == 2
    # 0.3177 pt per unit; x is centered on the 1404-unit page.
    assert highlight["rects_mm"][0] == [89.9, 56.0, 123.5, 58.7]
    assert highlight["rects_mm"][1] == [11.4, 59.4, 23.8, 62.1]
    assert highlight["bbox_mm"] == [11.4, 56.0, 123.5, 62.1]
    assert highlight["region"] == [0.073, 0.267, 0.785, 0.296]


async def test_read_annotations_shows_highlight_region():
    details = [
        {
            "text": "Estimate the remainder for a Taylor series",
            "color": "pink",
            "region": [0.177, 0.206, 0.479, 0.219],
        },
        {
            "text": "Therefore, at the",
            "color": "blue",
            "region": [0.096, 0.674, 0.27, 0.687],
        },
    ]
    data = await _read_annotations(
        {
            "typed_text": [],
            "highlights": [d["text"] for d in details],
            "highlight_details": [{"page": 2, **d} for d in details],
            "handwritten_text": None,
            "pages": 2,
            "page_ids": ["p1", "p2"],
            "annotated_pages": [
                {
                    "page": 2,
                    "page_id": "p2",
                    "has_handwriting": False,
                    "highlights": [d["text"] for d in details],
                    "highlight_details": details,
                }
            ],
            "ocr_backend": None,
            "tags": [],
        }
    )

    assert (
        "  • [pink] Estimate the remainder for a Taylor series"
        "   region [0.177, 0.206, 0.479, 0.219]" in data["content"]
    )
    assert "  • [blue] Therefore, at the   region [0.096, 0.674, 0.27, 0.687]" in data["content"]


def test_highlighter_stroke_position_follows_text_anchor(tmp_path):
    """Strokes in a group anchored to typed text are placed like the renderer places them."""
    from unittest.mock import patch

    rm_file = tmp_path / "page.rm"
    rm_file.write_bytes(
        build_rm([("Layer 1", True, [stroke(5, HIGHLIGHTER, [(-50, 100), (150, 120)])])])
    )
    layer_node = CrdtId(0, 10)

    with patch(
        "remarkable_mcp.extract._v6_anchor_offsets",
        return_value={layer_node: (-464.0, 300.0)},
    ):
        details = _page_annotation_details(rm_file)

    assert details["highlighter_strokes"][0]["bbox"] == [-515.0, 399.0, -313.0, 421.0]

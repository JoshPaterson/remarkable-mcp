"""Typed text in the tablet's letter widths, fitted to highlight boxes over it."""

import io
import re

from rmscene import read_blocks
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import SceneGlyphItemBlock
from rmscene.tagged_block_common import CrdtId

from remarkable_mcp import notebooks as nb
from remarkable_mcp.extract import _typed_text_width, _v6_text_elements_with_bounds

TEXT = "Test Text testing highlighted text"


def _blocks(text: str = TEXT) -> list:
    return list(read_blocks(io.BytesIO(nb.page_rm_bytes(text))))


def _line(elements: list) -> tuple:
    """``(x, baseline, stretch)`` of the first typed line."""
    match = re.search(
        r'class="typed-text"[^>]*transform="translate\(([-\d.]+) ([-\d.]+)\)'
        r"(?: scale\(([-\d.]+) 1\))?",
        elements[0],
    )
    return float(match.group(1)), float(match.group(2)), float(match.group(3) or 1.0)


def _with_highlight(blocks: list, x: float, width: float, baseline: float) -> list:
    glyph = si.GlyphRange(
        start=None,
        length=16,
        text="highlighted text",
        color=si.PenColor(3),
        rectangles=[si.Rectangle(x=x, y=baseline - 37, w=width, h=44)],
    )
    item = CrdtSequenceItem(CrdtId(1, 500), CrdtId(0, 0), CrdtId(0, 0), 0, glyph)
    return blocks + [SceneGlyphItemBlock(parent_id=CrdtId(0, 11), item=item)]


def test_typed_text_drawn_as_outlines_at_text_box_origin():
    blocks = _blocks()
    text = next(b.value for b in blocks if type(b).__name__ == "RootTextBlock")
    elements, _, _ = _v6_text_elements_with_bounds(blocks)

    x, _, stretch = _line(elements)
    assert x == text.pos_x
    assert stretch == 1.0


def test_highlighted_words_centered_in_their_box():
    blocks = _blocks()
    text = next(b.value for b in blocks if type(b).__name__ == "RootTextBlock")
    _, baseline, _ = _line(_v6_text_elements_with_bounds(blocks)[0])
    words_start = text.pos_x + _typed_text_width("Test Text testing ", 30, False)
    words_width = _typed_text_width("highlighted text", 30, False)

    # The tablet's box is padded around the words: 8 units each side here,
    # starting 3 units later than Noto Sans puts them.
    box_x = words_start + 3 - 8
    elements, _, _ = _v6_text_elements_with_bounds(
        _with_highlight(blocks, box_x, words_width + 16, baseline)
    )

    x, new_baseline, stretch = _line(elements)
    assert stretch == 1.0
    assert abs(x - (text.pos_x + 3)) < 0.1
    # The line stays at the height the text layout gives it.
    assert new_baseline == baseline


def test_line_squeezed_when_words_overrun_their_box():
    blocks = _blocks()
    text = next(b.value for b in blocks if type(b).__name__ == "RootTextBlock")
    _, baseline, _ = _line(_v6_text_elements_with_bounds(blocks)[0])
    prefix = _typed_text_width("Test Text testing ", 30, False)
    words_width = _typed_text_width("highlighted text", 30, False)
    box_x = text.pos_x + prefix

    elements, _, _ = _v6_text_elements_with_bounds(
        _with_highlight(blocks, box_x, words_width * 0.9, baseline)
    )

    x, _, stretch = _line(elements)
    assert abs(stretch - 0.9) < 1e-3
    # The squeezed words fill the box, centered.
    assert abs(x + stretch * (prefix + words_width / 2) - (box_x + words_width * 0.45)) < 0.1


def test_highlight_on_another_line_leaves_text_alone():
    blocks = _blocks()
    _, baseline, _ = _line(_v6_text_elements_with_bounds(blocks)[0])

    elements, _, _ = _v6_text_elements_with_bounds(
        _with_highlight(blocks, 0.0, 100.0, baseline + 500)
    )

    assert _line(elements)[2] == 1.0
    assert "scale(" not in elements[0].split(">")[0]

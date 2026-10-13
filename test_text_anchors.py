"""Typed-text anchor positions on notebook pages.

Measured from a tablet-written notebook page with text at pos_y 234 and three
paragraphs: the tablet's highlight rectangle over the third paragraph, and the
dot-grid template in captures of the page, place the top-of-text anchor at
pos_y + 63 and the bottom-of-text anchor after the last paragraph.
"""

import io
import re

from rmscene import read_blocks
from rmscene.tagged_block_common import CrdtId
from rmscene.text import TextDocument

from remarkable_mcp import notebooks as nb
from remarkable_mcp.extract import (
    _ANCHOR_TEXT_BOTTOM,
    _ANCHOR_TEXT_TOP,
    _v6_text_elements_with_bounds,
)


def test_text_anchors_follow_device_layout():
    blocks = list(
        read_blocks(io.BytesIO(nb.page_rm_bytes("\n\nTest Text testing highlighted text")))
    )
    text = next(b.value for b in blocks if type(b).__name__ == "RootTextBlock")
    elements, _, anchor_pos = _v6_text_elements_with_bounds(blocks)

    top = anchor_pos[CrdtId(*_ANCHOR_TEXT_TOP)]
    bottom = anchor_pos[CrdtId(*_ANCHOR_TEXT_BOTTOM)]
    assert top == text.pos_y + 63
    paragraphs = len(TextDocument.from_scene_item(text).contents)
    assert bottom == top + paragraphs * 70
    # Paragraph k's baseline is one line below its anchor; for the third
    # paragraph that falls inside the tablet's highlight box for the line
    # (y 470..514 when pos_y is 234).
    assert 470 - 234 < 63 + 3 * 70 < 514 - 234
    match = re.search(r'translate\([-\d.]+ ([-\d.]+)\)|y="([-\d.]+)"', elements[-1])
    baseline = float(match.group(1) or match.group(2))
    assert baseline == top + paragraphs * 70

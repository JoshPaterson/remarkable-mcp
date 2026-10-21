"""Text highlights the tablet can erase: highlighter items on a page's text.

The tablet stores a text highlight (PDF text selected with the highlighter)
as a glyph-range item in the page's ink file, on a layer, one item per line:
the line's text, its offset in the page's text, a color and a rectangle. The
MCP writes the same items under its own author id (see ``rm_ink``), so
they look and behave like the user's own: they are listed with the user's
highlights, can be erased, and match the tablet's rectangles. See
docs/native-ink.md for the format and how it was calibrated.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from remarkable_mcp import colors, rm_ink

GLYPH_BLOCK = 0x03
# Rectangle padding beyond PyMuPDF's loose character boxes, as a share of
# the font size, to match the tablet's own highlights.
LEFT_PAD = 0.16
RIGHT_PAD = 0.10

# Highlighter colors: the tablet's color id and the color_rgba it stores.
COLORS: Dict[str, Tuple[int, Tuple[int, int, int, int]]] = {
    c.name: (c.color_id, c.rgba) for c in colors.HIGHLIGHTERS
}
DEFAULT_COLOR = "yellow"


class HighlightError(rm_ink.InkError):
    """Text to highlight cannot be found or the request is invalid."""


def _page_chars(page, flags: int = 0) -> Tuple[str, List[Optional[dict]]]:
    """The page's text as ``get_text()`` gives it, and each character's
    ``{"bbox", "size"}`` (None for the line breaks)."""
    text, chars = [], []
    for block in page.get_text("rawdict", flags=flags)["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for ch in span["chars"]:
                    text.append(ch["c"])
                    chars.append({"bbox": ch["bbox"], "size": span["size"]})
            text.append("\n")
            chars.append(None)
    return "".join(text), chars


def find_text(page, query: str, occurrence: int = 1) -> List[Dict[str, Any]]:
    """The lines of the ``occurrence``-th match of ``query`` on a PDF page
    (whitespace and line breaks in the page match any whitespace in the
    query), as ``{"start", "length", "text", "rect"}``: ``start`` in the
    tablet's counting and ``rect`` in points, as the tablet draws it."""
    import pymupdf

    words = query.split()
    if not words:
        raise HighlightError("The text to highlight is empty.")
    text, loose = _page_chars(page)
    _, tight = _page_chars(page, pymupdf.TEXT_ACCURATE_BBOXES)
    pattern = re.compile(r"\s+".join(re.escape(w) for w in words))
    matches = list(pattern.finditer(text))
    if len(matches) < occurrence:
        found = f"only {len(matches)} times" if matches else "not at all"
        raise HighlightError(f"'{query}' appears {found} on the page.")
    match = matches[occurrence - 1]
    parts, start = [], match.start()
    for i in range(match.start(), match.end() + 1):
        if i == match.end() or text[i] == "\n":
            piece = text[start:i].strip()
            if piece:
                first = start + (len(text[start:i]) - len(text[start:i].lstrip()))
                sel = range(first, first + len(piece))
                lb = [loose[j]["bbox"] for j in sel]
                tb = [tight[j]["bbox"] for j in sel]
                size = max(loose[j]["size"] for j in sel)
                rect = (
                    min(b[0] for b in lb) - LEFT_PAD * size,
                    (min(b[1] for b in lb) + min(b[1] for b in tb)) / 2,
                    max(b[2] for b in lb) + RIGHT_PAD * size,
                    max(b[3] for b in tb),
                )
                parts.append(
                    {
                        "start": first + text[:first].count("\n"),
                        "length": len(piece),
                        "text": piece,
                        "rect": rect,
                    }
                )
            start = i + 1
    return parts


def add_highlights(
    data: Optional[bytes],
    author: uuid.UUID,
    size: Tuple[float, float],
    parts: Sequence[Dict[str, Any]],
    color: str,
    layer: Optional[str] = None,
) -> Tuple[bytes, List[List[int]]]:
    """``data`` (a page's ink file, None for none) with a highlight item for
    each of ``parts`` (see ``find_text``); returns the file and item ids."""
    color_id, rgba = COLORS[color]
    page = rm_ink.PageWriter(data, author)
    ids = page.add_glyphs(page.ink_layer(layer), parts, size, color_id, rgba)
    return page.data, ids


def add_mark_highlight(page: rm_ink.PageWriter, mark: Dict[str, Any], size) -> None:
    """Write a pending text highlight mark (on a page of ``size`` points) and
    store its item ids in the mark."""
    ink = mark["highlight"]
    color_id, rgba = COLORS[mark["color_name"]]
    layer = page.ink_layer(ink.get("layer"))
    ids = page.add_glyphs(layer, ink["parts"], size, color_id, rgba)
    mark["highlight"] = {"layer": ink.get("layer"), "items": ids}

"""Text highlights the tablet can erase: highlighter items on a page's text.

The tablet stores a text highlight (PDF text selected with the highlighter)
as a glyph-range item in the page's ink file, on a layer, one item per line:
the line's text, its offset in the page's text, a color and a rectangle. The
MCP writes the same items under its own author id (see ``cover_layers``), so
they look and behave like the user's own: they are listed with the user's
highlights, can be erased, and match the tablet's rectangles. See
docs/native-ink.md for the format and how it was calibrated.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from remarkable_mcp import cover_layers as cl

GLYPH_BLOCK = 0x03
# Rectangle padding beyond PyMuPDF's loose character boxes, as a share of
# the font size, to match the tablet's own highlights.
LEFT_PAD = 0.16
RIGHT_PAD = 0.10

# Highlighter colors: the tablet's color id and the color_rgba it stores.
COLORS: Dict[str, Tuple[int, Tuple[int, int, int, int]]] = {
    "yellow": (3, (0, 0, 0, 255)),
    "green": (4, (0, 0, 0, 255)),
    "pink": (5, (0, 0, 0, 255)),
    "gray": (8, (0, 0, 0, 255)),
    "blue": (9, (190, 234, 254, 255)),
    "orange": (9, (255, 195, 140, 255)),
}
DEFAULT_COLOR = "yellow"


class HighlightError(ValueError):
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


def _layer_for(data: bytes, author: uuid.UUID, name: Optional[str]) -> Tuple[bytes, Tuple]:
    """The layer to put highlights on: the layer named ``name`` (made on top
    if the page has none), else the selected layer, else the first layer."""
    from rmscene.scene_stream import SceneInfo

    layers = cl.page_layers(data)
    if name is not None:
        for layer in layers:
            if layer["name"] == name:
                return data, layer["id"]
        return cl._add_layer(data, author, name, True, top=True)
    live = {layer["id"] for layer in layers}
    for block in cl._parse(data):
        if isinstance(block, SceneInfo) and block.current_layer is not None:
            current = cl._key(block.current_layer.value)
            if current in live:
                return data, current
    number = cl.author_number(data, author)
    own = [layer["id"] for layer in layers if layer["id"][0] != number]
    if not own:
        raise HighlightError("The page has no layer to highlight on.")
    return data, own[0]


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
    from rmscene.scene_items import GlyphRange, PenColor, Rectangle
    from rmscene.scene_stream import CrdtSequenceItem, SceneGlyphItemBlock
    from rmscene.tagged_block_common import CrdtId

    from remarkable_mcp.extract import _points_per_unit, _v6_paper_size

    color_id, rgba = COLORS[color]
    if data is None:
        data = cl.first_page_rm(author)
    data, layer_id = _layer_for(data, author, layer)
    data, number = cl._with_author(data, author)
    blocks = cl._parse(data)
    counter = cl._next_counter(blocks)
    ppu = _points_per_unit(_v6_paper_size(blocks))
    width = size[0]
    new, ids = [], []
    for part in parts:
        x0, y0, x1, y1 = part["rect"]
        glyph = GlyphRange(
            start=part["start"],
            length=part["length"],
            text=part["text"],
            color=PenColor(color_id),
            color_rgba=rgba,
            rectangles=[
                Rectangle((x0 - width / 2) / ppu, y0 / ppu, (x1 - x0) / ppu, (y1 - y0) / ppu)
            ],
        )
        item = CrdtSequenceItem(
            item_id=CrdtId(number, counter),
            left_id=CrdtId(0, 0),
            right_id=CrdtId(0, 0),
            deleted_length=0,
            value=glyph,
        )
        new.append(SceneGlyphItemBlock(parent_id=cl._crdt(layer_id), item=item))
        ids.append([number, counter])
        counter += 1
    return data + cl._encode(*new), ids


def highlight_status(data: Optional[bytes], item_ids: Iterable[Sequence[int]]) -> str:
    """ "on tablet", "erased on tablet" (all items erased), "partly erased on
    tablet" or "missing"."""
    from rmscene.scene_stream import SceneGlyphItemBlock

    wanted = {tuple(int(v) for v in i) for i in item_ids}
    if data is None or not wanted:
        return "missing"
    live = erased = 0
    for block in cl._parse(data):
        if isinstance(block, SceneGlyphItemBlock) and cl._key(block.item.item_id) in wanted:
            if block.item.value is None:
                erased += 1
            else:
                live += 1
    if not live and not erased:
        return "missing"
    if not live:
        return "erased on tablet"
    return "partly erased on tablet" if erased else "on tablet"


# --- Text highlight marks --------------------------------------------------


def write_mark_highlights(
    before: Sequence[Dict[str, Any]],
    after: Sequence[Dict[str, Any]],
    removed: Iterable[str],
    *,
    page_ids: Sequence[str],
    page_map: Sequence[Optional[int]],
    sizes: Sequence[Tuple[float, float]],
    read_ink,
    author: uuid.UUID,
) -> Dict[str, bytes]:
    """Write new text highlight marks to their pages' ink and delete those of
    removed marks; pending marks in ``after`` get their item ids. Returns the
    changed ink files by page id."""
    from remarkable_mcp.pdf_marks import tablet_page

    removed = set(removed)
    jobs: Dict[int, Dict[str, list]] = {}
    for mark in before:
        ink = mark.get("highlight")
        if mark["id"] in removed and isinstance(ink, dict) and ink.get("items"):
            jobs.setdefault(mark["pdf_page"], {"remove": [], "add": []})["remove"] += ink["items"]
    for mark in after:
        ink = mark.get("highlight")
        if isinstance(ink, dict) and ink.get("pending"):
            jobs.setdefault(mark["pdf_page"], {"remove": [], "add": []})["add"].append(mark)
    changed: Dict[str, bytes] = {}
    for pdf_page, job in jobs.items():
        page = tablet_page(pdf_page, page_map)
        if page is None or page > len(page_ids):
            continue
        page_id = page_ids[page - 1]
        data = read_ink(page_id)
        if data is not None and b"version=6" not in data[:43]:
            raise HighlightError(f"Page {page}'s ink is in an older format.")
        if data is not None and job["remove"]:
            data = cl._tombstone(data, job["remove"])
        for mark in job["add"]:
            ink = mark["highlight"]
            data, ids = add_highlights(
                data,
                author,
                sizes[pdf_page],
                ink["parts"],
                mark["color_name"],
                ink.get("layer"),
            )
            mark["highlight"] = {"layer": ink.get("layer"), "items": ids}
        changed[page_id] = data
    return changed


def mark_highlight_states(
    marks: Sequence[Dict[str, Any]],
    *,
    page_ids: Sequence[str],
    page_map: Sequence[Optional[int]],
    read_ink,
) -> Dict[str, str]:
    """Each text highlight mark's state on the tablet (``highlight_status``)."""
    from remarkable_mcp.pdf_marks import tablet_page

    states: Dict[str, str] = {}
    pages: Dict[int, Optional[bytes]] = {}
    for mark in marks:
        ink = mark.get("highlight")
        if not isinstance(ink, dict) or not ink.get("items"):
            continue
        page = tablet_page(mark["pdf_page"], page_map)
        if page is None or page > len(page_ids):
            continue
        if page not in pages:
            pages[page] = read_ink(page_ids[page - 1])
        states[mark["id"]] = highlight_status(pages[page], ink["items"])
    return states

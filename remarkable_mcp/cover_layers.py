"""Cover layers: hide marks on the tablet with white ink on a layer.

A mark drawn into the PDF sits under every ink layer. A cover layer is the
bottom layer of a page holding white strokes over some marks: while the user
shows it the marks are hidden, and hiding it shows them again. The user's ink
is on the layers above, so it stays visible.

The MCP writes under its own author id (one per installation), so its ink
never collides with ink from the tablet or other devices. Strokes are removed
by replacing their blocks with tombstones, as the tablet does. See
docs/native-ink.md for the format and what was verified on the tablet.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from remarkable_mcp import rm_ink

DEFAULT_LAYER = "Mark cover"
MAX_LAYER_NAME = 40
# Covers follow a mark's shape: rows of white strokes 2.5pt wide, 1.75pt
# apart, over the mark's pixels widened by 1.5pt, so backgrounds such as dot
# grids lose only what is under the mark itself.
STROKE_PT = 2.5
STEP_PT = 1.75
PAD_PT = 1.5
GAP_PT = 1.0
POINT_STEP_PT = 10.0
RASTER_PER_PT = 4
WHITE = 2

Id = rm_ink.Id


class CoverError(rm_ink.InkError):
    """Raised when a cover cannot be written."""


def layer_name(value: Any) -> Optional[str]:
    """The cover layer a mark's ``cover`` option names, or None for no cover."""
    if value is None or value is False:
        return None
    if value is True:
        return DEFAULT_LAYER
    if isinstance(value, str) and value.strip() and len(value.strip()) <= MAX_LAYER_NAME:
        return value.strip()
    raise CoverError(
        f"cover must be true (the '{DEFAULT_LAYER}' layer) or a layer name of up to "
        f"{MAX_LAYER_NAME} characters."
    )


def shape_rows(
    snippet: bytes, box: Sequence[float], size: Tuple[float, float]
) -> List[List[List[float]]]:
    """Horizontal stroke paths (page fractions) covering what a mark draws.

    ``snippet`` is the mark's one-page PDF, placed over ``box`` on a page of
    ``size`` points. Its pixels are widened by ``PAD_PT``; each band of
    ``STEP_PT`` gets a stroke over each run of covered pixels.
    """
    import pymupdf
    from PIL import Image, ImageFilter

    with pymupdf.open("pdf", snippet) as doc:
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(RASTER_PER_PT, RASTER_PER_PT), alpha=True)
        page_w = doc[0].rect.width
    alpha = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples).getchannel("A")
    pad = int(round(PAD_PT * RASTER_PER_PT))
    grown = Image.new("L", (pix.width + 2 * pad, pix.height + 2 * pad), 0)
    grown.paste(alpha.point(lambda v: 255 if v > 8 else 0), (pad, pad))
    grown = grown.filter(ImageFilter.MaxFilter(2 * pad + 1))
    width, height = grown.size
    mask = grown.tobytes()

    # Points per raster pixel on the page; the snippet fills the box's width.
    scale = (box[2] - box[0]) * size[0] / page_w / RASTER_PER_PT
    origin = (box[0] * size[0] - pad * scale, box[1] * size[1] - pad * scale)
    band = max(1, int(round(STEP_PT / scale)))
    gap = int(round(GAP_PT / scale))
    radius = STROKE_PT / 2
    rows = []
    for top in range(0, height, band):
        covered = [
            any(mask[y * width + x] for y in range(top, min(top + band, height)))
            for x in range(width)
        ]
        runs: List[List[int]] = []
        for x, on in enumerate(covered):
            if not on:
                continue
            if runs and x - runs[-1][1] <= gap + 1:
                runs[-1][1] = x
            else:
                runs.append([x, x])
        y_pt = origin[1] + (top + min(band, height - top) / 2) * scale
        for x0, x1 in runs:
            left = origin[0] + x0 * scale + radius
            right = origin[0] + (x1 + 1) * scale - radius
            if right < left:
                left = right = (left + right) / 2
            count = max(1, int((right - left) / POINT_STEP_PT) + 1)
            rows.append(
                [
                    [(left + i * (right - left) / count) / size[0], y_pt / size[1]]
                    for i in range(count + 1)
                ]
            )
    return rows


def edit_page(
    data: Optional[bytes],
    author: uuid.UUID,
    size: Tuple[float, float],
    *,
    add: Sequence[Dict[str, Any]] = (),
    remove: Iterable[Sequence[int]] = (),
) -> Tuple[bytes, List[Dict[str, Any]]]:
    """Change the covers on one page's ink.

    ``data`` is the page's ``.rm`` file (None when it has no ink yet) and
    ``size`` its PDF page size in points. Strokes in ``remove`` are deleted.
    Each of ``add`` is ``{"layer": name, "rows": [paths], "shown": bool}``
    (see ``shape_rows``);
    a layer the MCP has not made on this page is created at the bottom,
    shown or hidden as ``shown`` says. Returns the new file and, for each of
    ``add``, ``{"layer": layer id, "strokes": [stroke ids]}``.
    """
    page = rm_ink.PageWriter(data, author)
    page.tombstone(remove)
    placed: List[Dict[str, Any]] = []
    layers: Dict[str, Id] = {}
    for request in add:
        name = request["layer"]
        if name not in layers:
            found = page.find_layer(name, own=True)
            if found is None:
                found = page.add_layer(name, bool(request.get("shown")))
            layers[name] = found
        rows = [{"points": row, "width": STROKE_PT} for row in request["rows"]]
        ids = page.add_lines(layers[name], rows, size, "fineliner", WHITE)
        placed.append({"layer": layers[name], "strokes": ids})
    return page.data, placed


# --- Reading covers ----------------------------------------------------------

HIDDEN = "hidden on tablet"
SHOWN = "shown on tablet"
ERASED = "cover erased on tablet"
MISSING = "cover missing"


def cover_status(data: Optional[bytes], stroke_ids: Sequence[Sequence[int]]) -> str:
    """Whether a mark's cover strokes hide it on the tablet now."""
    states = rm_ink.item_states(data, stroke_ids)
    if not states:
        return MISSING
    live_parents = {layer for layer, live in states.values() if live}
    if not live_parents:
        return ERASED
    visible = {layer["id"]: layer["visible"] for layer in rm_ink.page_layers(data)}
    return HIDDEN if any(visible.get(p) for p in live_parents) else SHOWN


def foreign_strokes_on_covers(data: Optional[bytes], author: Optional[uuid.UUID]) -> int:
    """Live strokes by other authors (the user) on the MCP's cover layers.

    They hide with the marks while the cover layer is shown.
    """
    from rmscene.scene_stream import SceneLineItemBlock

    if data is None or author is None:
        return 0
    number = rm_ink.author_number(data, author)
    if number is None:
        return 0
    ours = {layer["id"] for layer in rm_ink.page_layers(data) if layer["id"][0] == number}
    return sum(
        1
        for block in rm_ink.parse(data)
        if isinstance(block, SceneLineItemBlock)
        and block.item.value is not None
        and rm_ink.key(block.parent_id) in ours
        and int(block.item.item_id.part1) != number
    )


# --- Covers of marks ---------------------------------------------------------


def mark_rows(mark: Dict[str, Any], size: Tuple[float, float]) -> List[List[List[float]]]:
    """Stroke paths covering a mark and its leader (see ``shape_rows``)."""
    import base64

    rows = []
    for part in (mark, mark.get("leader")):
        if isinstance(part, dict) and part.get("pdf"):
            rows += shape_rows(base64.b64decode(part["pdf"]), part["box"], size)
    return rows


def write_mark_covers(
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
    """Write the covers of new marks and delete those of removed ones.

    ``before`` and ``after`` are the mark lists around an edit; marks in
    ``after`` whose cover is pending get their stroke ids. ``read_ink(page_id)``
    returns a page's ``.rm`` file or None. Returns the changed files by page id.
    """
    from remarkable_mcp.pdf_marks import tablet_page

    removed = set(removed)
    jobs: Dict[int, Dict[str, list]] = {}
    for mark in before:
        cover = mark.get("cover")
        if mark["id"] in removed and isinstance(cover, dict) and cover.get("strokes"):
            jobs.setdefault(mark["pdf_page"], {"remove": [], "add": []})["remove"] += cover[
                "strokes"
            ]
    for mark in after:
        cover = mark.get("cover")
        if isinstance(cover, dict) and cover.get("pending"):
            jobs.setdefault(mark["pdf_page"], {"remove": [], "add": []})["add"].append(mark)
    changed: Dict[str, bytes] = {}
    for pdf_page, job in jobs.items():
        page = tablet_page(pdf_page, page_map)
        if page is None or page > len(page_ids):
            continue
        page_id = page_ids[page - 1]
        data = read_ink(page_id)
        if data is not None and not rm_ink.is_v6(data):
            raise CoverError(f"Page {page}'s ink is in an older format that cannot be covered.")
        requests = [
            {
                "layer": m["cover"]["layer"],
                "rows": mark_rows(m, sizes[pdf_page]),
                "shown": m["cover"]["shown"],
            }
            for m in job["add"]
        ]
        new, placed = edit_page(data, author, sizes[pdf_page], add=requests, remove=job["remove"])
        for mark, result in zip(job["add"], placed):
            mark["cover"] = {
                "layer": mark["cover"]["layer"],
                "strokes": [list(s) for s in result["strokes"]],
            }
        changed[page_id] = new
    return changed


def mark_cover_states(
    marks: Sequence[Dict[str, Any]],
    *,
    page_ids: Sequence[str],
    page_map: Sequence[Optional[int]],
    read_ink,
) -> Tuple[Dict[str, str], List[str]]:
    """``(states, notes)``: each covered mark's state on the tablet (see
    ``cover_status``), and notes about the user's strokes on cover layers."""
    from remarkable_mcp.pdf_marks import tablet_page

    states: Dict[str, str] = {}
    pages: Dict[int, Optional[bytes]] = {}
    for mark in marks:
        cover = mark.get("cover")
        if not isinstance(cover, dict) or not cover.get("strokes"):
            continue
        page = tablet_page(mark["pdf_page"], page_map)
        if page is None or page > len(page_ids):
            continue
        if page not in pages:
            pages[page] = read_ink(page_ids[page - 1])
        states[mark["id"]] = cover_status(pages[page], cover["strokes"])
    notes = []
    author = rm_ink.known_author_uuid()
    for page, data in sorted(pages.items()):
        count = foreign_strokes_on_covers(data, author)
        if count:
            notes.append(
                f"Page {page}: {count} of the user's strokes are on a cover layer and "
                "hide with the marks while it is shown."
            )
    return states, notes

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

import dataclasses
import io
import struct
import uuid
import warnings
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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

ROOT = (0, 1)
DETACHED = (0, 0)
LAYER_1 = (0, 11)
TREE_BLOCK, NODE_BLOCK, GLYPH_ITEM_BLOCK, GROUP_ITEM_BLOCK, LINE_BLOCK = (
    0x01,
    0x02,
    0x03,
    0x04,
    0x05,
)
TEXT_BLOCK, PAGE_INFO_BLOCK, AUTHORS_BLOCK = 0x07, 0x0A, 0x09

AUTHOR_FILE_NAME = "mcp-author"

Id = Tuple[int, int]


class CoverError(ValueError):
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


def author_uuid(directory: Optional[Path] = None) -> uuid.UUID:
    """This installation's author id, created on first use."""
    from remarkable_mcp.api import REMARKABLE_CONFIG_DIR

    path = (directory or REMARKABLE_CONFIG_DIR) / AUTHOR_FILE_NAME
    try:
        return uuid.UUID(path.read_text().strip())
    except (OSError, ValueError):
        pass
    value = uuid.uuid4()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n")
    return value


def known_author_uuid(directory: Optional[Path] = None) -> Optional[uuid.UUID]:
    """This installation's author id, or None if it has never written ink."""
    from remarkable_mcp.api import REMARKABLE_CONFIG_DIR

    try:
        text = ((directory or REMARKABLE_CONFIG_DIR) / AUTHOR_FILE_NAME).read_text()
        return uuid.UUID(text.strip())
    except (OSError, ValueError):
        return None


# --- Block-level access ------------------------------------------------------


def _header() -> bytes:
    from rmscene import HEADER_V6

    return HEADER_V6


def _raw_blocks(data: bytes) -> List[Tuple[int, bytes]]:
    """``(block type, raw bytes)`` of each top-level block after the header."""
    pos, out = len(_header()), []
    while pos + 8 <= len(data):
        length = struct.unpack_from("<I", data, pos)[0]
        out.append((data[pos + 7], data[pos : pos + 8 + length]))
        pos += 8 + length
    return out


def _encode(*blocks: Any) -> bytes:
    from rmscene import write_blocks

    from remarkable_mcp.strokes import WRITE_VERSION

    buf = io.BytesIO()
    write_blocks(buf, list(blocks), options={"version": WRITE_VERSION})
    return buf.getvalue()[len(_header()) :]


def _parse(data: bytes) -> list:
    from rmscene import read_blocks

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return list(read_blocks(io.BytesIO(data)))


def _parse_one(raw: bytes) -> Any:
    blocks = _parse(_header() + raw)
    return blocks[0] if blocks else None


def _key(crdt_id: Any) -> Id:
    return (int(crdt_id.part1), int(crdt_id.part2))


def _crdt(key: Sequence[int]) -> Any:
    from rmscene.tagged_block_common import CrdtId

    return CrdtId(int(key[0]), int(key[1]))


def _authors(blocks: list) -> Dict[int, uuid.UUID]:
    from rmscene.scene_stream import AuthorIdsBlock

    for block in blocks:
        if isinstance(block, AuthorIdsBlock):
            return dict(block.author_uuids)
    return {}


def author_number(data: bytes, author: uuid.UUID) -> Optional[int]:
    """The file-local number of ``author`` in a page's ink, if listed."""
    for number, value in _authors(_parse(data)).items():
        if value == author:
            return number
    return None


def _with_author(data: bytes, author: uuid.UUID) -> Tuple[bytes, int]:
    """``data`` with ``author`` in its author table, and its number there."""
    from rmscene.scene_stream import AuthorIdsBlock

    authors = _authors(_parse(data))
    for number, value in authors.items():
        if value == author:
            return data, number
    number = next((n for n in range(1, 256) if n not in authors), None)
    if number is None:
        raise CoverError("The page has no free author number for the MCP's ink.")
    table = _encode(AuthorIdsBlock({number: author, **authors}))
    parts, replaced = [_header()], False
    for btype, raw in _raw_blocks(data):
        if btype == AUTHORS_BLOCK and not replaced:
            parts.append(table)
            replaced = True
        else:
            parts.append(raw)
    if not replaced:
        parts.insert(1, table)
    return b"".join(parts), number


def _next_counter(blocks: list) -> int:
    """One more than the highest counter of any id or timestamp in a page.

    Counters are shared by all authors; the author number keeps ids unique.
    """
    from remarkable_mcp.strokes import _iter_crdt_ids

    highest = 0
    for crdt_id in _iter_crdt_ids(blocks):
        highest = max(highest, int(crdt_id.part2))
    for block in blocks:
        for holder in (getattr(block, "group", None), block):
            for attr in ("label", "visible", "current_layer", "background_visible"):
                stamp = getattr(getattr(holder, attr, None), "timestamp", None)
                if stamp is not None:
                    highest = max(highest, int(stamp.part2))
    return highest + 1


def _splice(data: bytes, after_type: Dict[int, bytes], tail: bytes = b"") -> bytes:
    """``data`` with each ``after_type[t]`` inserted after its last block of
    type ``t`` (at the end when there is none), and ``tail`` appended."""
    raw = _raw_blocks(data)
    last = {t: i for i, (t, _) in enumerate(raw)}
    parts = [_header()]
    for i, (btype, block) in enumerate(raw):
        parts.append(block)
        for t, extra in after_type.items():
            if last.get(t) == i:
                parts.append(extra)
    parts += [extra for t, extra in after_type.items() if t not in last]
    parts.append(tail)
    return b"".join(parts)


# --- Pages and layers --------------------------------------------------------


def first_page_rm(author: uuid.UUID) -> bytes:
    """A first ink file for a page: Layer 1, selected, as the tablet makes it.

    It has no typed-text block (with one, the tablet treats the page as a
    text page) and selects Layer 1 in its SceneInfo (without one, the tablet
    selects the newest layer, which would put the user's writing on a cover).
    """
    from rmscene.scene_stream import SceneInfo
    from rmscene.tagged_block_common import CrdtId, LwwValue

    from remarkable_mcp import notebooks
    from remarkable_mcp.strokes import DEFAULT_PAPER_SIZE

    base = notebooks.blank_page_rm_bytes(author_uuid=str(author))
    number = author_number(base, author) or 1
    info = SceneInfo(
        current_layer=LwwValue(CrdtId(number, 2), _crdt(LAYER_1)),
        background_visible=LwwValue(CrdtId(number, 3), True),
        root_document_visible=LwwValue(CrdtId(0, 0), True),
        paper_size=DEFAULT_PAPER_SIZE,
    )
    parts = [_header()]
    for btype, raw in _raw_blocks(base):
        if btype != TEXT_BLOCK:
            parts.append(raw)
        if btype == PAGE_INFO_BLOCK:
            parts.append(_encode(info))
    return b"".join(parts)


def _layer_order(blocks: list) -> List[Id]:
    from remarkable_mcp.extract import _v6_layer_order

    return [_key(node) for node in _v6_layer_order(blocks)]


def page_layers(data: bytes) -> List[Dict[str, Any]]:
    """The live layers of a page, bottom first: ``{"id", "name", "visible"}``."""
    from rmscene.scene_stream import TreeNodeBlock

    blocks = _parse(data)
    nodes: Dict[Id, Any] = {}
    for block in blocks:
        if isinstance(block, TreeNodeBlock):
            nodes[_key(block.group.node_id)] = block.group
    out = []
    for position, node_id in enumerate(_layer_order(blocks), start=1):
        group = nodes.get(node_id)
        label = getattr(getattr(group, "label", None), "value", "") if group else ""
        visible = getattr(getattr(group, "visible", None), "value", True) if group else True
        out.append({"id": node_id, "name": label or f"Layer {position}", "visible": bool(visible)})
    return out


def _find_cover_layer(data: bytes, author: uuid.UUID, name: str) -> Optional[Id]:
    """A live layer named ``name`` that the MCP made on this page."""
    number = author_number(data, author)
    if number is None:
        return None
    for layer in page_layers(data):
        if layer["id"][0] == number and layer["name"] == name:
            return layer["id"]
    return None


def _add_layer(
    data: bytes, author: uuid.UUID, name: str, shown: bool, top: bool = False
) -> Tuple[bytes, Id]:
    """``data`` with a new layer named ``name`` at the bottom of the page (or
    with ``top``, above the others)."""
    from rmscene.crdt_sequence import toposort_items
    from rmscene.scene_items import Group
    from rmscene.scene_stream import (
        CrdtSequenceItem,
        SceneGroupItemBlock,
        SceneTreeBlock,
        TreeNodeBlock,
    )
    from rmscene.tagged_block_common import CrdtId, LwwValue

    data, number = _with_author(data, author)
    blocks = _parse(data)
    counter = _next_counter(blocks)
    layer = CrdtId(number, counter)
    root = _crdt(ROOT)
    items = [b.item for b in blocks if isinstance(b, SceneGroupItemBlock) and b.parent_id == root]
    order = list(toposort_items(items)) if items else []
    first = order[0] if order and not top else CrdtId(0, 0)
    last = order[-1] if order and top else CrdtId(0, 0)
    tree = SceneTreeBlock(tree_id=layer, node_id=CrdtId(0, 0), is_update=True, parent_id=root)
    node = TreeNodeBlock(
        Group(
            node_id=layer,
            label=LwwValue(CrdtId(number, counter + 1), name),
            visible=LwwValue(CrdtId(number, counter + 2), shown),
        )
    )
    item = SceneGroupItemBlock(
        parent_id=root,
        item=CrdtSequenceItem(
            item_id=CrdtId(number, counter + 3),
            left_id=last,
            right_id=first,
            deleted_length=0,
            value=layer,
        ),
    )
    data = _splice(
        data,
        {TREE_BLOCK: _encode(tree), NODE_BLOCK: _encode(node), GROUP_ITEM_BLOCK: _encode(item)},
    )
    return data, _key(layer)


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


def _add_strokes(
    data: bytes, author: uuid.UUID, layer: Id, rows: Sequence[Sequence[Sequence[float]]], size
) -> Tuple[bytes, List[Id]]:
    """``data`` with white strokes along ``rows`` (paths in page fractions) on
    ``layer``, and the strokes' ids."""
    from remarkable_mcp.extract import _points_per_unit, _v6_paper_size
    from remarkable_mcp.strokes import build_line_block, serialize_new_blocks

    data, number = _with_author(data, author)
    blocks = _parse(data)
    counter = _next_counter(blocks)
    ppu = _points_per_unit(_v6_paper_size(blocks))
    extent = (size[0] / ppu, size[1] / ppu)
    width = round(4 * STROKE_PT / ppu)
    new, ids = [], []
    for row in rows:
        stroke_id = (number, counter)
        counter += 1
        stroke = {"points": row, "tool": "fineliner", "color": WHITE, "width": width}
        new.append(build_line_block(_crdt(layer), _crdt(stroke_id), stroke, *extent))
        ids.append(stroke_id)
    return data + serialize_new_blocks(new), ids


def _tombstone(data: bytes, stroke_ids: Iterable[Sequence[int]]) -> bytes:
    """``data`` with the given strokes (or text highlights) deleted in place."""
    targets = {tuple(int(v) for v in s) for s in stroke_ids}
    if not targets:
        return data
    parts = [_header()]
    for btype, raw in _raw_blocks(data):
        block = _parse_one(raw) if btype in (LINE_BLOCK, GLYPH_ITEM_BLOCK) else None
        item = getattr(block, "item", None)
        if item is not None and _key(item.item_id) in targets and item.value is not None:
            item = dataclasses.replace(item, value=None, deleted_length=1)
            parts.append(_encode(dataclasses.replace(block, item=item)))
        else:
            parts.append(raw)
    return b"".join(parts)


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
    if data is None:
        data = first_page_rm(author)
    data = _tombstone(data, remove)
    placed: List[Dict[str, Any]] = []
    layers: Dict[str, Id] = {}
    for request in add:
        name = request["layer"]
        if name not in layers:
            found = _find_cover_layer(data, author, name)
            if found is None:
                data, found = _add_layer(data, author, name, bool(request.get("shown")))
            layers[name] = found
        data, ids = _add_strokes(data, author, layers[name], request["rows"], size)
        placed.append({"layer": layers[name], "strokes": ids})
    return data, placed


# --- Reading covers ----------------------------------------------------------

HIDDEN = "hidden on tablet"
SHOWN = "shown on tablet"
ERASED = "cover erased on tablet"
MISSING = "cover missing"


def cover_status(data: Optional[bytes], stroke_ids: Sequence[Sequence[int]]) -> str:
    """Whether a mark's cover strokes hide it on the tablet now."""
    from rmscene.scene_stream import SceneLineItemBlock

    if data is None or not stroke_ids:
        return MISSING
    blocks = _parse(data)
    wanted = {tuple(int(v) for v in s) for s in stroke_ids}
    live_parents, found = set(), False
    for block in blocks:
        if isinstance(block, SceneLineItemBlock) and _key(block.item.item_id) in wanted:
            found = True
            if block.item.value is not None:
                live_parents.add(_key(block.parent_id))
    if not found:
        return MISSING
    if not live_parents:
        return ERASED
    visible = {layer["id"]: layer["visible"] for layer in page_layers(data)}
    return HIDDEN if any(visible.get(p) for p in live_parents) else SHOWN


def foreign_strokes_on_covers(data: Optional[bytes], author: Optional[uuid.UUID]) -> int:
    """Live strokes by other authors (the user) on the MCP's cover layers.

    They hide with the marks while the cover layer is shown.
    """
    from rmscene.scene_stream import SceneLineItemBlock

    if data is None or author is None:
        return 0
    number = author_number(data, author)
    if number is None:
        return 0
    ours = {layer["id"] for layer in page_layers(data) if layer["id"][0] == number}
    return sum(
        1
        for block in _parse(data)
        if isinstance(block, SceneLineItemBlock)
        and block.item.value is not None
        and _key(block.parent_id) in ours
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
        if data is not None and b"version=6" not in data[:43]:
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
    author = known_author_uuid()
    for page, data in sorted(pages.items()):
        count = foreign_strokes_on_covers(data, author)
        if count:
            notes.append(
                f"Page {page}: {count} of the user's strokes are on a cover layer and "
                "hide with the marks while it is shown."
            )
    return states, notes

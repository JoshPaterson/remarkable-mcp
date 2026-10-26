"""The v6 ink layer: read and change a page's ``.rm`` file block by block.

The MCP writes ink (covers, text highlights, erasable marks) under its own
author id (one per installation), so its items never collide with ink from
the tablet or other devices. Blocks the MCP does not change are copied as
raw bytes; new blocks are inserted or appended, and items are removed by
replacing their blocks with tombstones, as the tablet does. See
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

ROOT = (0, 1)
LAYER_1 = (0, 11)
TREE_BLOCK, NODE_BLOCK, GLYPH_ITEM_BLOCK, GROUP_ITEM_BLOCK, LINE_BLOCK = (
    0x01,
    0x02,
    0x03,
    0x04,
    0x05,
)
TEXT_BLOCK, PAGE_INFO_BLOCK, AUTHORS_BLOCK, SCENE_INFO_BLOCK = 0x07, 0x0A, 0x09, 0x0D

AUTHOR_FILE_NAME = "mcp-author"
# Covers are white fineliner strokes.
WHITE = 2

Id = Tuple[int, int]


class InkError(ValueError):
    """Ink that cannot be written: a bad cover, layer or text to highlight,
    or a page the MCP cannot write to."""


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


def is_v6(data: bytes) -> bool:
    """Whether ``data`` is a v6 ink file, the only format the MCP writes to."""
    return b"version=6" in data[: len(_header())]


def raw_blocks(data: bytes) -> List[Tuple[int, bytes]]:
    """``(block type, raw bytes)`` of each top-level block after the header."""
    pos, out = len(_header()), []
    while pos + 8 <= len(data):
        length = struct.unpack_from("<I", data, pos)[0]
        out.append((data[pos + 7], data[pos : pos + 8 + length]))
        pos += 8 + length
    return out


def encode(blocks: Sequence[Any]) -> bytes:
    """``blocks`` serialized in the tablet's format, without the file header."""
    from rmscene import write_blocks

    from remarkable_mcp.strokes import WRITE_VERSION

    buf = io.BytesIO()
    write_blocks(buf, list(blocks), options={"version": WRITE_VERSION})
    return buf.getvalue()[len(_header()) :]


def parse(data: bytes) -> list:
    """The blocks of an ink file."""
    from rmscene import read_blocks

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return list(read_blocks(io.BytesIO(data)))


def _parse_one(raw: bytes) -> Any:
    blocks = parse(_header() + raw)
    return blocks[0] if blocks else None


def key(crdt_id: Any) -> Id:
    """A CRDT id as a ``(author, counter)`` tuple."""
    return (int(crdt_id.part1), int(crdt_id.part2))


def crdt(value: Sequence[int]) -> Any:
    """An ``(author, counter)`` pair as a CRDT id."""
    from rmscene.tagged_block_common import CrdtId

    return CrdtId(int(value[0]), int(value[1]))


def id_set(ids: Iterable[Sequence[int]]) -> set:
    """Stored item ids (lists or tuples) as a set of ``(author, counter)``."""
    return {(int(i[0]), int(i[1])) for i in ids}


def iter_crdt_ids(blocks: Iterable[object]):
    """Yield every CRDT id referenced by a block list (best effort)."""
    from rmscene.tagged_block_common import CrdtId

    for b in blocks:
        for attr in ("tree_id", "node_id", "parent_id", "block_id"):
            v = getattr(b, attr, None)
            if isinstance(v, CrdtId):
                yield v
        item = getattr(b, "item", None)
        if item is not None:
            iid = getattr(item, "item_id", None)
            if isinstance(iid, CrdtId):
                yield iid
            val = getattr(item, "value", None)
            if isinstance(val, CrdtId):
                yield val
        group = getattr(b, "group", None)
        if group is not None:
            gid = getattr(group, "node_id", None)
            if isinstance(gid, CrdtId):
                yield gid


def _authors(blocks: list) -> Dict[int, uuid.UUID]:
    from rmscene.scene_stream import AuthorIdsBlock

    for block in blocks:
        if isinstance(block, AuthorIdsBlock):
            return dict(block.author_uuids)
    return {}


def author_number_in(blocks: list, author: Optional[uuid.UUID]) -> Optional[int]:
    """The file-local number of ``author`` in parsed ink blocks, if listed."""
    for number, value in _authors(blocks).items():
        if value == author:
            return number
    return None


def author_number(data: bytes, author: uuid.UUID) -> Optional[int]:
    """The file-local number of ``author`` in a page's ink, if listed."""
    return author_number_in(parse(data), author)


def with_author(data: bytes, author: uuid.UUID) -> Tuple[bytes, int]:
    """``data`` with ``author`` in its author table, and its number there."""
    from rmscene.scene_stream import AuthorIdsBlock

    authors = _authors(parse(data))
    for number, value in authors.items():
        if value == author:
            return data, number
    number = next((n for n in range(1, 256) if n not in authors), None)
    if number is None:
        raise InkError("The page has no free author number for the MCP's ink.")
    table = encode([AuthorIdsBlock({number: author, **authors})])
    parts, replaced = [_header()], False
    for btype, raw in raw_blocks(data):
        if btype == AUTHORS_BLOCK and not replaced:
            parts.append(table)
            replaced = True
        else:
            parts.append(raw)
    if not replaced:
        parts.insert(1, table)
    return b"".join(parts), number


def next_counter(blocks: list) -> int:
    """One more than the highest counter of any id or timestamp in a page.

    Counters are shared by all authors; the author number keeps ids unique.
    """
    highest = 0
    for crdt_id in iter_crdt_ids(blocks):
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
    raw = raw_blocks(data)
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


def tombstone(data: bytes, item_ids: Iterable[Sequence[int]]) -> bytes:
    """``data`` with the given strokes or text highlights deleted in place."""
    targets = id_set(item_ids)
    if not targets:
        return data
    parts = [_header()]
    for btype, raw in raw_blocks(data):
        block = _parse_one(raw) if btype in (LINE_BLOCK, GLYPH_ITEM_BLOCK) else None
        item = getattr(block, "item", None)
        if item is not None and key(item.item_id) in targets and item.value is not None:
            item = dataclasses.replace(item, value=None, deleted_length=1)
            parts.append(encode([dataclasses.replace(block, item=item)]))
        else:
            parts.append(raw)
    return b"".join(parts)


# --- Pages and layers --------------------------------------------------------


def _scene_info(layer: Id, number: int, counter: int) -> Any:
    """A SceneInfo as the MCP writes a new one: ``layer`` selected (stamped
    ``(number, counter)``), the background shown, and the standard paper."""
    from rmscene.scene_stream import SceneInfo
    from rmscene.tagged_block_common import CrdtId, LwwValue

    from remarkable_mcp.strokes import DEFAULT_PAPER_SIZE

    return SceneInfo(
        current_layer=LwwValue(CrdtId(number, counter), crdt(layer)),
        background_visible=LwwValue(CrdtId(number, counter + 1), True),
        root_document_visible=LwwValue(CrdtId(0, 0), True),
        paper_size=DEFAULT_PAPER_SIZE,
    )


def first_page_rm(author: uuid.UUID) -> bytes:
    """A first ink file for a page: Layer 1, selected, as the tablet makes it.

    It has no typed-text block (with one, the tablet treats the page as a
    text page) and selects Layer 1 in its SceneInfo (without one, the tablet
    selects the newest layer, which would put the user's writing on a cover).
    """
    from remarkable_mcp import notebooks

    base = notebooks.blank_page_rm_bytes(author_uuid=str(author))
    number = author_number(base, author) or 1
    info = _scene_info(LAYER_1, number, 2)
    parts = [_header()]
    for btype, raw in raw_blocks(base):
        if btype != TEXT_BLOCK:
            parts.append(raw)
        if btype == PAGE_INFO_BLOCK:
            parts.append(encode([info]))
    return b"".join(parts)


def select_layer(data: bytes, layer: Id, number: int, counter: int) -> bytes:
    """``data`` with its SceneInfo selecting ``layer``, stamped ``(number,
    counter)``; a page without SceneInfo gets one after its page info block
    (using counters ``counter`` and ``counter + 1``).

    Each SceneInfo block is rewritten in place: it keeps its header versions
    and every other field. All other blocks are copied as raw bytes.
    """
    from rmscene.scene_stream import SceneInfo
    from rmscene.tagged_block_common import CrdtId, LwwValue

    value = LwwValue(CrdtId(number, counter), crdt(layer))
    raw = raw_blocks(data)
    parts, rewritten = [_header()], False
    for btype, block_raw in raw:
        block = _parse_one(block_raw) if btype == SCENE_INFO_BLOCK else None
        if isinstance(block, SceneInfo):
            new = encode([dataclasses.replace(block, current_layer=value)])
            block_raw = new[:4] + block_raw[4:8] + new[8:]
            rewritten = True
        parts.append(block_raw)
    if rewritten:
        return b"".join(parts)
    info = encode([_scene_info(layer, number, counter)])
    types = [t for t, _ in raw]
    at = types.index(PAGE_INFO_BLOCK) + 1 if PAGE_INFO_BLOCK in types else 0
    blocks = [r for _, r in raw]
    return b"".join([_header(), *blocks[:at], info, *blocks[at:]])


def _layers(blocks: list) -> List[Dict[str, Any]]:
    from rmscene.scene_stream import TreeNodeBlock

    from remarkable_mcp.extract import _v6_layer_order

    nodes: Dict[Id, Any] = {}
    for block in blocks:
        if isinstance(block, TreeNodeBlock):
            nodes[key(block.group.node_id)] = block.group
    out = []
    for position, node in enumerate(_v6_layer_order(blocks), start=1):
        node_id = key(node)
        group = nodes.get(node_id)
        label = getattr(getattr(group, "label", None), "value", "") if group else ""
        visible = getattr(getattr(group, "visible", None), "value", True) if group else True
        out.append({"id": node_id, "name": label or f"Layer {position}", "visible": bool(visible)})
    return out


def page_layers(data: bytes) -> List[Dict[str, Any]]:
    """The live layers of a page, bottom first: ``{"id", "name", "visible"}``."""
    return _layers(parse(data))


def _selected(blocks: list, layers: Sequence[Dict[str, Any]]) -> Optional[Id]:
    from rmscene.scene_stream import SceneInfo

    live = {layer["id"] for layer in layers}
    for block in blocks:
        if isinstance(block, SceneInfo) and block.current_layer is not None:
            current = key(block.current_layer.value)
            if current in live:
                return current
    return None


def selected_layer(data: bytes) -> Optional[Id]:
    """The live layer a page's SceneInfo selects, or None."""
    blocks = parse(data)
    return _selected(blocks, _layers(blocks))


def item_states(data: Optional[bytes], item_ids: Iterable[Sequence[int]]) -> Dict[Id, tuple]:
    """``{id: (layer id, live)}`` for each of ``item_ids`` (strokes or text
    highlights) found in a page's ink; erased items are not live."""
    from rmscene.scene_stream import SceneGlyphItemBlock, SceneLineItemBlock

    wanted = id_set(item_ids)
    if data is None or not wanted:
        return {}
    out: Dict[Id, tuple] = {}
    for block in parse(data):
        if isinstance(block, (SceneLineItemBlock, SceneGlyphItemBlock)):
            item_id = key(block.item.item_id)
            if item_id in wanted:
                out[item_id] = (key(block.parent_id), block.item.value is not None)
    return out


def layer_roles(
    blocks: list,
    number: Optional[int],
    cover_ids: Iterable[Sequence[int]] = (),
    ink_ids: Iterable[Sequence[int]] = (),
) -> Tuple[set, set]:
    """``(cover layers, ink layers)``: the layers the MCP (author ``number``)
    made that hold covers, and those that hold erasable ink or highlights.

    A layer holding any of ``cover_ids`` (the stroke ids of covers, as stored
    in marks) is a cover layer, and one holding any of ``ink_ids`` an ink
    layer. Otherwise a layer's live MCP items decide: white fineliner strokes
    make a cover layer, other strokes and highlights an ink layer. A layer
    whose MCP items are all erased and not in the stored ids has no role.
    """
    from rmscene.scene_items import Pen
    from rmscene.scene_stream import SceneGlyphItemBlock, SceneLineItemBlock

    covers, inks = set(), set()
    if number is None:
        return covers, inks
    stored_covers, stored_inks = id_set(cover_ids), id_set(ink_ids)
    fineliners = {int(Pen.FINELINER_1.value), int(Pen.FINELINER_2.value)}
    for block in blocks:
        if not isinstance(block, (SceneLineItemBlock, SceneGlyphItemBlock)):
            continue
        layer = key(block.parent_id)
        if layer[0] != number:
            continue
        item_id = key(block.item.item_id)
        if item_id in stored_covers:
            covers.add(layer)
        elif item_id in stored_inks:
            inks.add(layer)
        elif block.item.value is not None and item_id[0] == number:
            value = block.item.value
            white_pen = (
                isinstance(block, SceneLineItemBlock)
                and int(value.color) == WHITE
                and int(value.tool) in fineliners
            )
            (covers if white_pen else inks).add(layer)
    return covers, inks - covers


# --- Writing -----------------------------------------------------------------


class PageWriter:
    """Changes to one page's ink, parsed once.

    ``data`` is the page's ``.rm`` file, or None for a page without ink (a
    first ink file is made). The MCP's author is added to the file with its
    first new item or layer. ``data`` holds the file with the changes made.
    ``covers`` and ``inks`` are the MCP's cover and ink layers (see
    ``layer_roles``, given ``cover_ids`` and ``ink_ids``); erasable ink and
    highlights never go on a cover layer, and covers never on an ink layer.

    The MCP never changes which layer the user has selected. A page whose
    SceneInfo selects no live layer (tablet pages with only Layer 1 store no
    selection, and the tablet then selects the newest layer, which would be
    a layer the MCP adds) is written with the selection pinned to the user's
    layer: the newest layer the MCP did not make, as the page was before the
    edit. A live selection, even of an MCP layer, is kept as it is.
    """

    def __init__(
        self,
        data: Optional[bytes],
        author: uuid.UUID,
        cover_ids: Iterable[Sequence[int]] = (),
        ink_ids: Iterable[Sequence[int]] = (),
    ):
        self.author = author
        self._data = first_page_rm(author) if data is None else data
        self._blocks = parse(self._data)
        self._layer_list = _layers(self._blocks)
        self._number = author_number_in(self._blocks, author)
        self._authored = False
        self._counter = next_counter(self._blocks)
        self.covers, self.inks = layer_roles(self._blocks, self._number, cover_ids, ink_ids)
        self._source = self._data
        self._pin = None if self.selected_layer() is not None else self._user_layer()
        self._pinned: Optional[Tuple[bytes, bytes]] = None

    @property
    def data(self) -> bytes:
        """The page's ``.rm`` file with the changes made (the unchanged file
        when nothing changed)."""
        if self._pin is None or self._data == self._source:
            return self._data
        if self._pinned is None or self._pinned[0] != self._data:
            data, number = with_author(self._data, self.author)
            pinned = select_layer(data, self._pin, number, self._take(2))
            self._pinned = (self._data, pinned)
        return self._pinned[1]

    def _user_layer(self) -> Optional[Id]:
        """The newest live layer the MCP did not make, or None."""
        own = [
            layer["id"]
            for layer in self._layer_list
            if self._number is None or layer["id"][0] != self._number
        ]
        return max(own, key=lambda layer: (layer[1], layer[0])) if own else None

    @property
    def number(self) -> Optional[int]:
        """The MCP's author number in the page, None until it writes."""
        return self._number

    def _ensure_author(self) -> int:
        if not self._authored:
            self._data, self._number = with_author(self._data, self.author)
            self._authored = True
        return self._number

    def _take(self, count: int) -> int:
        start = self._counter
        self._counter += count
        return start

    def _points_per_unit(self) -> float:
        from remarkable_mcp.extract import _points_per_unit, _v6_paper_size

        return _points_per_unit(_v6_paper_size(self._blocks))

    @property
    def blocks(self) -> list:
        """The page's blocks, as parsed before items were appended."""
        return self._blocks

    def new_ids(self, count: int) -> List[Id]:
        """``count`` fresh item ids under the MCP's author, from the page's
        shared counter."""
        number = self._ensure_author()
        start = self._take(count)
        return [(number, start + i) for i in range(count)]

    def append(self, blocks: Sequence[Any]) -> None:
        """Append new item blocks (with ids from ``new_ids``) to the page."""
        self._data += encode(blocks)

    def layers(self) -> List[Dict[str, Any]]:
        """The page's live layers, bottom first (see ``page_layers``)."""
        return list(self._layer_list)

    def selected_layer(self) -> Optional[Id]:
        """The live layer the page's SceneInfo selects, or None."""
        return _selected(self._blocks, self._layer_list)

    def find_layer(self, name: str, own: bool = False, exclude: Iterable[Id] = ()) -> Optional[Id]:
        """A live layer named ``name`` that is not in ``exclude`` (with
        ``own``, one the MCP made)."""
        exclude = set(exclude)
        for layer in self._layer_list:
            if layer["name"] != name or layer["id"] in exclude:
                continue
            if own and (self._number is None or layer["id"][0] != self._number):
                continue
            return layer["id"]
        return None

    def cover_layer(self, name: str, shown: bool) -> Id:
        """The MCP's cover layer named ``name``, made at the bottom of the
        page (shown or hidden as ``shown`` says) if it has none."""
        layer = self.find_layer(name, own=True, exclude=self.inks)
        if layer is None:
            layer = self.add_layer(name, shown)
        self.covers.add(layer)
        return layer

    def add_layer(self, name: str, shown: bool, top: bool = False) -> Id:
        """A new layer named ``name`` at the bottom of the page (or with
        ``top``, above the others)."""
        from rmscene.crdt_sequence import toposort_items
        from rmscene.scene_items import Group
        from rmscene.scene_stream import (
            CrdtSequenceItem,
            SceneGroupItemBlock,
            SceneTreeBlock,
            TreeNodeBlock,
        )
        from rmscene.tagged_block_common import CrdtId, LwwValue

        number = self._ensure_author()
        counter = self._take(4)
        layer = CrdtId(number, counter)
        root = crdt(ROOT)
        items = [
            b.item
            for b in self._blocks
            if isinstance(b, SceneGroupItemBlock) and b.parent_id == root
        ]
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
        self._data = _splice(
            self._data,
            {
                TREE_BLOCK: encode([tree]),
                NODE_BLOCK: encode([node]),
                GROUP_ITEM_BLOCK: encode([item]),
            },
        )
        self._blocks = parse(self._data)
        self._layer_list = _layers(self._blocks)
        return key(layer)

    def set_layer_visible(self, layer: Id, shown: bool) -> bool:
        """Show or hide ``layer``; False (and nothing written) when it is
        already in that state.

        The layer's tree node block is rewritten in place, as the tablet does
        when the user toggles a layer: its ``visible`` value is stamped with
        the MCP's author and the page's next counter, newer than every id and
        timestamp in the page. The block keeps its header versions and every
        other field.
        """
        from rmscene.scene_stream import TreeNodeBlock
        from rmscene.tagged_block_common import CrdtId, LwwValue

        current = next((entry for entry in self._layer_list if entry["id"] == layer), None)
        if current is None:
            raise InkError(f"The page has no layer {list(layer)}.")
        if current["visible"] == bool(shown):
            return False
        number = self._ensure_author()
        stamp = CrdtId(number, self._take(1))
        parts, rewritten = [_header()], False
        for btype, raw in raw_blocks(self._data):
            block = _parse_one(raw) if btype == NODE_BLOCK else None
            if isinstance(block, TreeNodeBlock) and key(block.group.node_id) == layer:
                group = dataclasses.replace(block.group, visible=LwwValue(stamp, bool(shown)))
                new = encode([dataclasses.replace(block, group=group)])
                raw = new[:4] + raw[4:8] + new[8:]
                rewritten = True
            parts.append(raw)
        if not rewritten:
            raise InkError(f"The page has no node block for layer {list(layer)}.")
        self._data = b"".join(parts)
        self._blocks = parse(self._data)
        self._layer_list = _layers(self._blocks)
        return True

    def ink_layer(self, name: Optional[str]) -> Id:
        """The layer to put erasable ink or highlights on: the layer named
        ``name`` (made on top if the page has none), else the selected layer,
        else the first layer the MCP did not make. Cover layers are never
        used."""
        if name is not None:
            layer = self.find_layer(name, exclude=self.covers)
            if layer is None:
                if self.find_layer(name) is not None:
                    raise InkError(f"'{name}' is a cover layer; put the ink on another layer.")
                layer = self.add_layer(name, True, top=True)
            self.inks.add(layer)
            return layer
        selected = self.selected_layer()
        if selected is not None and selected not in self.covers:
            return selected
        own = [layer["id"] for layer in self._layer_list if layer["id"][0] != self._number]
        if not own:
            raise InkError("The page has no layer to put ink on.")
        return own[0]

    def add_lines(
        self,
        layer: Id,
        lines: Sequence[Dict[str, Any]],
        size: Tuple[float, float],
        tool: str,
        color: int,
        rgba: Optional[Tuple[int, int, int, int]] = None,
    ) -> List[List[int]]:
        """Strokes on ``layer``, one per ``{"points", "width"}`` of ``lines``
        (points in page fractions, width in points on a page of ``size``
        points), drawn with ``tool`` in pen color id ``color``; ``rgba`` is
        the color a highlighter stroke stores. Returns their ids."""
        from rmscene.scene_items import PenColor

        from remarkable_mcp.strokes import build_line_block

        number = self._ensure_author()
        ppu = self._points_per_unit()
        extent = (size[0] / ppu, size[1] / ppu)
        new, ids = [], []
        for line in lines:
            counter = self._take(1)
            block = build_line_block(
                crdt(layer),
                crdt((number, counter)),
                {
                    "points": line["points"],
                    "tool": tool,
                    "color": color,
                    "width": max(1, round(4 * line["width"] / ppu)),
                },
                *extent,
            )
            if rgba is not None and color == int(PenColor.HIGHLIGHT.value):
                value = dataclasses.replace(block.item.value, color_rgba=rgba)
                item = dataclasses.replace(block.item, value=value)
                block = dataclasses.replace(block, item=item)
            new.append(block)
            ids.append([number, counter])
        self._data += encode(new)
        return ids

    def add_glyphs(
        self,
        layer: Id,
        parts: Sequence[Dict[str, Any]],
        size: Tuple[float, float],
        color: int,
        rgba: Tuple[int, int, int, int],
    ) -> List[List[int]]:
        """Text highlight items on ``layer``, one per ``{"start", "length",
        "text", "rect"}`` of ``parts`` (``rect`` in points on a page of
        ``size`` points). Returns their ids."""
        from rmscene.scene_items import GlyphRange, PenColor, Rectangle
        from rmscene.scene_stream import CrdtSequenceItem, SceneGlyphItemBlock
        from rmscene.tagged_block_common import CrdtId

        number = self._ensure_author()
        ppu = self._points_per_unit()
        width = size[0]
        new, ids = [], []
        for part in parts:
            counter = self._take(1)
            x0, y0, x1, y1 = part["rect"]
            glyph = GlyphRange(
                start=part["start"],
                length=part["length"],
                text=part["text"],
                color=PenColor(color),
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
            new.append(SceneGlyphItemBlock(parent_id=crdt(layer), item=item))
            ids.append([number, counter])
        self._data += encode(new)
        return ids

    def tombstone(self, item_ids: Iterable[Sequence[int]]) -> None:
        """Delete strokes or text highlights in place."""
        self._data = tombstone(self._data, item_ids)

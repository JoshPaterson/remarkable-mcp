"""Images pasted onto a page (v6 ``.rm`` block types 14 and 15).

Current firmware can capture part of a page and paste it as an image. The
image is stored as a PNG next to the page's ``.rm`` file, at
``<doc id>/<page id>/<file name>``, and the page scene refers to it with two
block types that rmscene 0.8 does not decode (it returns them as
``UnreadableBlock``):

- Block type 14, an image catalog: subblock 1 holds a count, then one
  subblock per image with a 16-byte image key followed by an LWW string (the
  file name).
- Block type 15, a scene item laid out like the other scene item blocks
  (parent id, item id, left id, right id, deleted length, value subblock 6).
  The value starts with item type 7, then: subblock 1, an LWW image key;
  id 2; subblock 3, a float count and ``(x, y, u, v)`` vertices in page
  units and texture coordinates; subblock 4, triangle indices; id 5.

Both are parsed with rmscene's ``TaggedBlockReader``.
"""

import io
import logging
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

IMAGE_CATALOG_BLOCK = 14
IMAGE_ITEM_BLOCK = 15
_IMAGE_ITEM_TYPE = 7


@dataclass
class PastedImage:
    """An image item on a page."""

    item_id: Any
    parent_id: Any
    key: bytes
    file_name: Optional[str]
    vertices: List[Tuple[float, float, float, float]]  # (x, y, u, v)

    def corners(self) -> Optional[Tuple[Tuple[float, float], ...]]:
        """Page positions of texture corners (0, 0), (1, 0) and (0, 1)."""

        def at(u: float, v: float) -> Optional[Tuple[float, float]]:
            for x, y, vu, vv in self.vertices:
                if abs(vu - u) < 1e-3 and abs(vv - v) < 1e-3:
                    return x, y
            return None

        origin, right, down = at(0, 0), at(1, 0), at(0, 1)
        if origin is None or right is None or down is None:
            return None
        return origin, right, down

    def bbox(self) -> Optional[List[float]]:
        """``[x0, y0, x1, y1]`` of the image's vertices in page units."""
        if not self.vertices:
            return None
        xs = [v[0] for v in self.vertices]
        ys = [v[1] for v in self.vertices]
        return [min(xs), min(ys), max(xs), max(ys)]


def _reader(block: Any):
    from rmscene.tagged_block_reader import TaggedBlockReader

    reader = TaggedBlockReader(io.BytesIO(block.data))
    reader.current_block = replace(block.info, offset=0, size=len(block.data))
    return reader


def _read_catalog(block: Any) -> Dict[bytes, Tuple[Any, str]]:
    """Image key -> ``(timestamp, file name)`` from a catalog block."""
    reader = _reader(block)
    entries: Dict[bytes, Tuple[Any, str]] = {}
    with reader.read_subblock(1):
        count = reader.data.read_varuint()
        for _ in range(count):
            with reader.read_subblock(0) as entry:
                key = reader.data.read_bytes(16)
                name = reader.read_lww_string(1)
                # Any further fields are skipped to the end of the entry.
                remaining = entry.offset + entry.size - reader.data.tell()
                if remaining > 0:
                    reader.data.read_bytes(remaining)
            entries[key] = (name.timestamp, name.value)
    return entries


def _read_item_header(reader: Any) -> Tuple[Any, Any]:
    """Read an image item's ids; return ``(parent_id, CrdtSequenceItem)``.

    The sequence item's value is None here; callers fill it in.
    """
    from rmscene.crdt_sequence import CrdtSequenceItem

    parent_id = reader.read_id(1)
    item_id = reader.read_id(2)
    left_id = reader.read_id(3)
    right_id = reader.read_id(4)
    deleted_length = reader.read_int(5)
    return parent_id, CrdtSequenceItem(item_id, left_id, right_id, deleted_length, None)


def image_sequence_items(blocks: list) -> List[Tuple[Any, Any]]:
    """``(parent_id, CrdtSequenceItem)`` for every image item block, in order.

    The item's value is True for a live image and None for a deleted one, so
    the items can be ordered with the rest of their parent's sequence.
    """
    items = []
    for block in blocks:
        if type(block).__name__ != "UnreadableBlock":
            continue
        if getattr(getattr(block, "info", None), "block_type", None) != IMAGE_ITEM_BLOCK:
            continue
        try:
            reader = _reader(block)
            parent_id, item = _read_item_header(reader)
            item.value = True if reader.has_subblock(6) else None
        except Exception as exc:
            logger.debug("Skipping unreadable image block: %s", exc)
            continue
        items.append((parent_id, item))
    return items


def _read_item(block: Any) -> Tuple[Any, Any, Optional[Tuple[bytes, list]]]:
    """``(item_id, parent_id, (key, vertices) or None if deleted)``."""
    reader = _reader(block)
    parent_id, sequence_item = _read_item_header(reader)
    item_id = sequence_item.item_id
    if not reader.has_subblock(6):
        return item_id, parent_id, None
    with reader.read_subblock(6) as value:
        if reader.data.read_uint8() != _IMAGE_ITEM_TYPE:
            raise ValueError("unexpected item type in image block")
        with reader.read_subblock(1):
            reader.read_id(1)  # timestamp
            with reader.read_subblock(2):
                key = reader.data.read_bytes(16)
        reader.read_id(2)
        with reader.read_subblock(3):
            count = reader.data.read_varuint()
            floats = [reader.data.read_float32() for _ in range(count)]
        vertices = [tuple(floats[i : i + 4]) for i in range(0, len(floats) - 3, 4)]
        remaining = value.offset + value.size - reader.data.tell()
        if remaining > 0:
            reader.data.read_bytes(remaining)
    return item_id, parent_id, (key, vertices)


def pasted_images(blocks: list) -> Dict[Any, PastedImage]:
    """Live pasted images on a page, keyed by item id, in block order.

    A later block for the same item id replaces an earlier one; an item block
    without a value is a deleted item. Blocks that cannot be parsed are
    skipped.
    """
    catalog: Dict[bytes, Tuple[Any, str]] = {}
    items: Dict[Any, Optional[Tuple[Any, bytes, list]]] = {}
    for block in blocks:
        if type(block).__name__ != "UnreadableBlock":
            continue
        block_type = getattr(getattr(block, "info", None), "block_type", None)
        try:
            if block_type == IMAGE_CATALOG_BLOCK:
                for key, (stamp, name) in _read_catalog(block).items():
                    if key not in catalog or catalog[key][0] <= stamp:
                        catalog[key] = (stamp, name)
            elif block_type == IMAGE_ITEM_BLOCK:
                item_id, parent_id, value = _read_item(block)
                items[item_id] = None if value is None else (parent_id, *value)
        except Exception as exc:
            logger.debug("Skipping unreadable image block: %s", exc)

    images: Dict[Any, PastedImage] = {}
    for item_id, value in items.items():
        if value is None:
            continue
        parent_id, key, vertices = value
        name = catalog.get(key, (None, None))[1]
        images[item_id] = PastedImage(item_id, parent_id, key, name, vertices)
    return images

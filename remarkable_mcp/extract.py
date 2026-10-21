"""
Text extraction helpers for reMarkable documents.
"""

import functools
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as _xml_escape

from remarkable_mcp.colors import color_info, highlight_opacity, render_color
from remarkable_mcp.pages import content_page_ids, deleted_page_ids, has_cpages, live_cpages_entries
from remarkable_mcp.tags import page_tag_entries
from remarkable_mcp.templates import BLANK_TEMPLATE, content_page_templates

logger = logging.getLogger(__name__)


def _rm_to_svg(rm_file_path: Path, output_svg_path: Path, layers: Optional[str] = None) -> bool:
    """Convert a .rm file to SVG using the built-in rmscene renderers.

    The ``rmc`` dependency has been dropped: it transitively pinned
    ``rmscene<0.7.0``, which cannot parse current-firmware ``.rm`` scene
    blocks (e.g. ``SceneInfo`` ``block_type=13``). We render via ``rmscene``
    directly using the in-repo v6/v5 renderers. v6 is tried first since it is
    the current firmware format.

    Writes the SVG content to output_svg_path and returns True on success.
    Returns False if no renderer could handle the file.

    Credit: khalid-hasan (PR #97), ljdutel (#95)
    """
    svg_content = _render_rm_v6_to_svg(rm_file_path, layers) or _render_rm_v5_to_svg(rm_file_path)
    if svg_content is not None:
        output_svg_path.write_text(svg_content)
        return True

    return False


# Highlighter ink is drawn in full color with the Darken blend, as the
# tablet's PDF export draws it: the paper takes the color and dark ink or
# text under it stays dark. Browsers apply this style to SVG output; PNG
# rendering applies it in _svg_string_to_png, since MuPDF ignores it.
HIGHLIGHT_BLEND = 'style="mix-blend-mode:darken"'
_DARKEN_ELEMENT = re.compile(r"<(?:path|rect)\b[^>]*mix-blend-mode:darken[^>]*/>")

# reMarkable tablet screen dimensions (in pixels) - used as fallback
REMARKABLE_WIDTH = 1404
REMARKABLE_HEIGHT = 1872

# PDF points per stroke unit, keyed by the per-page SceneInfo.paper_size grid.
# The 1404x1872 value was calibrated against a device PDF export. The Paper Pro
# value follows its panel DPI and remains a best-effort calibration until a
# ground-truth export reporting that grid is available. Devices that normalize
# Paper Pro scenes to 1404x1872 continue to use the calibrated value.
_DEVICE_POINTS_PER_UNIT = {
    (1404, 1872): 0.3177,
    (1620, 2160): 0.3144,
}
REMARKABLE_PDF_POINTS_PER_UNIT = 0.3177


def _points_per_unit(paper_size: Optional[Tuple[float, float]]) -> float:
    """Return PDF points per stroke unit for a SceneInfo paper grid."""
    if paper_size:
        key = (int(round(paper_size[0])), int(round(paper_size[1])))
        if key in _DEVICE_POINTS_PER_UNIT:
            return _DEVICE_POINTS_PER_UNIT[key]
    return REMARKABLE_PDF_POINTS_PER_UNIT


def _full_page_scale(paper_w: float, paper_h: float, dpi: Optional[float]) -> float:
    """Output pixels per stroke unit for a full-page render.

    With ``dpi``, the page is sized from its physical extent (stroke units ->
    PDF points -> inches); otherwise its long edge is
    ``FULL_PAGE_TARGET_LONG_EDGE`` pixels.
    """
    if dpi:
        return dpi / 72.0 * _points_per_unit((paper_w, paper_h))
    return FULL_PAGE_TARGET_LONG_EDGE / max(paper_w, paper_h)


def _annotation_page_viewbox(
    pdf_w_pt: float,
    pdf_h_pt: float,
    points_per_unit: float,
    *,
    centered_x: bool = True,
) -> Tuple[float, float, float, float]:
    """Return the stroke-space rectangle occupied by a PDF page."""
    view_w = pdf_w_pt / points_per_unit
    view_h = pdf_h_pt / points_per_unit
    view_x = -view_w / 2.0 if centered_x else 0.0
    return view_x, 0.0, view_w, view_h


def _rewrite_svg_root(
    svg_content: str,
    *,
    viewbox: Tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    """Rewrite only root SVG geometry, leaving path stroke widths untouched."""
    import re

    root_match = re.search(r"<svg\b[^>]*>", svg_content, re.IGNORECASE)
    if root_match is None:
        return svg_content

    root = root_match.group(0)
    values = {
        "viewBox": " ".join(f"{value:.2f}" for value in viewbox),
        "width": str(width),
        "height": str(height),
    }
    for name, value in values.items():
        pattern = rf'(?<![-\w]){name}="[^"]*"'
        if re.search(pattern, root):
            root = re.sub(pattern, f'{name}="{value}"', root, count=1)
        else:
            root = root[:-1] + f' {name}="{value}">'

    return svg_content[: root_match.start()] + root + svg_content[root_match.end() :]


# Standard reMarkable background color (light cream/gray)
# Can be overridden via REMARKABLE_BACKGROUND_COLOR environment variable
_DEFAULT_BACKGROUND_COLOR = "#FBFBFB"


def get_background_color() -> str:
    """Get the background color, checking env var for override."""
    return os.environ.get("REMARKABLE_BACKGROUND_COLOR", _DEFAULT_BACKGROUND_COLOR)


OCR_BACKENDS = frozenset({"auto", "google", "tesseract"})


def get_ocr_backend() -> str:
    """Return a supported OCR backend, falling back to ``auto`` safely."""
    backend = os.environ.get("REMARKABLE_OCR_BACKEND", "auto").strip().lower()
    if backend not in OCR_BACKENDS:
        supported = ", ".join(sorted(OCR_BACKENDS))
        if backend == "sampling":
            logger.warning(
                "The 'sampling' OCR backend has been removed. "
                "Falling back to 'auto'. Set REMARKABLE_OCR_BACKEND to one of: %s.",
                supported,
            )
        else:
            logger.warning(
                "Unsupported REMARKABLE_OCR_BACKEND value %r. "
                "Falling back to 'auto'. Supported values are: %s.",
                backend,
                supported,
            )
        return "auto"
    return backend


# For backwards compatibility, expose as module constant (evaluated at import)
# Use get_background_color() for runtime evaluation of env var
REMARKABLE_BACKGROUND_COLOR = get_background_color()

# Margin around content when using content-based bounding box (in pixels)
CONTENT_MARGIN = 50

# Target long-edge resolution (px) for a full-page canvas render. The page is
# rasterised at this resolution preserving the page aspect; the displayed image
# is then scaled by the host, so this only sets crispness, not layout.
FULL_PAGE_TARGET_LONG_EDGE = 1872
# Blank space kept below the lowest ink when a page is extended to hold it.
INK_EXTENSION_MARGIN = 50.0

# Cache TTL in seconds (5 minutes)
CACHE_TTL_SECONDS = 300

# Module-level cache for OCR results (full document)
# Key: doc_id
# Value: {"result": extraction_result, "include_ocr": bool, "timestamp": float}
_extraction_cache: Dict[str, Dict[str, Any]] = {}

_cache_lock = threading.RLock()
_cache_generation: Dict[str, int] = {}
_global_cache_generation = 0


def _is_cache_valid(cached: Dict[str, Any], version: Optional[str] = None) -> bool:
    """Check if a cached entry is still valid.

    An entry is valid within its TTL, and only for the document version it was
    built from: ``version`` identifies the document's current content (for
    cloud documents, its index hash), so content uploaded after the entry was
    cached is never served from it. A ``version`` of None matches any entry.
    """
    if version is not None and cached.get("version") != version:
        return False
    if "timestamp" not in cached:
        return True  # Old cache entries without timestamp are valid
    return (time.time() - cached["timestamp"]) < CACHE_TTL_SECONDS


def clear_extraction_cache(doc_id: Optional[str] = None) -> None:
    """
    Clear the extraction cache.

    Args:
        doc_id: If provided, only clear cache for this document.
                If None, clear the entire cache.
    """
    global _global_cache_generation

    with _cache_lock:
        if doc_id:
            _cache_generation[doc_id] = _cache_generation.get(doc_id, 0) + 1
            _extraction_cache.pop(doc_id, None)
        else:
            _global_cache_generation += 1
            _extraction_cache.clear()
            _cache_generation.clear()


def _cache_token(doc_id: str) -> tuple[int, int]:
    with _cache_lock:
        return _global_cache_generation, _cache_generation.get(doc_id, 0)


def _cache_extraction_result_if_current(
    doc_id: str,
    result: Dict[str, Any],
    include_ocr: bool,
    token: tuple[int, int],
    version: Optional[str] = None,
) -> bool:
    """Cache only if no mutation invalidated this document during extraction."""
    with _cache_lock:
        if token != (
            _global_cache_generation,
            _cache_generation.get(doc_id, 0),
        ):
            return False
        _extraction_cache[doc_id] = {
            "result": result,
            "include_ocr": include_ocr,
            "timestamp": time.time(),
            "version": version,
        }
        return True


def get_cached_ocr_result(
    doc_id: str,
    include_ocr: bool = True,
    ocr_backend: Optional[str] = None,
    version: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Get cached OCR result for a document if available and valid.

    Args:
        doc_id: Document ID to look up
        include_ocr: Whether OCR content is required
        ocr_backend: If specified, only return cache if it was produced by this backend.
                     Use "google" or "tesseract". None accepts any backend.
        version: The document's current version (see ``_is_cache_valid``);
                 a result cached from another version is not returned.

    Returns:
        Cached result dict or None if not cached/expired/wrong backend
    """
    with _cache_lock:
        if doc_id in _extraction_cache:
            cached = _extraction_cache[doc_id]
            if (cached["include_ocr"] or not include_ocr) and _is_cache_valid(cached, version):
                if ocr_backend is not None:
                    cached_backend = cached["result"].get("ocr_backend")
                    if cached_backend != ocr_backend:
                        return None
                return cached["result"]
    return None


def cache_ocr_result(
    doc_id: str,
    result: Dict[str, Any],
    include_ocr: bool = True,
    version: Optional[str] = None,
) -> None:
    """
    Cache an OCR result for a document.

    Args:
        doc_id: Document ID
        result: Extraction result dict with keys: typed_text, highlights,
                handwritten_text, pages, page_ids, ocr_backend
        include_ocr: Whether this result includes OCR content
        version: The document version the result was built from
    """
    with _cache_lock:
        _extraction_cache[doc_id] = {
            "result": result,
            "include_ocr": include_ocr,
            "timestamp": time.time(),
            "version": version,
        }


def find_similar_documents(query: str, documents: List, limit: int = 5) -> List[str]:
    """Find documents with similar names for 'did you mean' suggestions."""
    query_lower = query.lower()
    scored = []
    for doc in documents:
        name = doc.VissibleName
        # Use sequence matcher for fuzzy matching
        ratio = SequenceMatcher(None, query_lower, name.lower()).ratio()
        # Boost partial matches
        if query_lower in name.lower():
            ratio += 0.3
        scored.append((name, ratio))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [name for name, score in scored[:limit] if score > 0.3]


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text from a PDF file using PyMuPDF.

    Returns the full text content of the PDF.
    """
    try:
        import fitz  # PyMuPDF

        text_parts = []
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, 1):
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(f"--- Page {page_num} ---\n{page_text.strip()}")

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        return ""


def extract_text_from_epub(epub_path: Path) -> str:
    """
    Extract text from an EPUB file.

    Returns the full text content of the EPUB.
    """
    try:
        from bs4 import BeautifulSoup
        from ebooklib import ITEM_DOCUMENT, epub

        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        text_parts = []

        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                # Get text, preserving some structure
                text = soup.get_text(separator="\n", strip=True)
                if text:
                    text_parts.append(text)

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        return ""


def extract_text_from_rm_file(rm_file_path: Path) -> List[str]:
    """
    Extract typed text from a .rm file using rmscene.

    This extracts text that was typed via Type Folio or on-screen keyboard.
    Does NOT require OCR - text is stored natively in v6 .rm files.
    """
    try:
        from rmscene import read_blocks
        from rmscene.scene_items import Text
        from rmscene.text import TextDocument

        with open(rm_file_path, "rb") as f:
            blocks = list(read_blocks(f))

        # The page's typed text lives on the RootTextBlock, not among the
        # root group's children (those are the ink layers), so scan the raw
        # blocks the same way the renderer does.
        text_item = next(
            (b.value for b in blocks if isinstance(getattr(b, "value", None), Text)),
            None,
        )
        if text_item is None:
            return []

        # TextDocument resolves the CRDT character sequence into ordered,
        # styled paragraphs; iterating Text.items directly yields CRDT ids
        # and fragments in insertion order, not readable lines.
        doc = TextDocument.from_scene_item(text_item)
        return [line for line in (str(para).strip() for para in doc.contents) if line]

    except ImportError:
        return []  # rmscene not available
    except Exception:
        # Log but don't fail - file might be older format
        return []


def _parse_hex_color(hex_color: str) -> tuple:
    """Parse a hex color string to RGBA tuple.

    Supports #RRGGBB (RGB) and #RRGGBBAA (RGBA) formats.

    Args:
        hex_color: Hex color string (e.g., "#FFFFFF" or "#FFFFFF80")

    Returns:
        Tuple of (r, g, b, a) values (0-255)
    """
    if not hex_color.startswith("#"):
        return (255, 255, 255, 255)

    hex_str = hex_color.lstrip("#")
    if len(hex_str) == 6:
        r, g, b = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4))
        return (r, g, b, 255)
    elif len(hex_str) == 8:
        r, g, b, a = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4, 6))
        return (r, g, b, a)
    else:
        return (255, 255, 255, 255)


def _get_svg_content_bounds(svg_path: Path) -> Optional[tuple]:
    """
    Parse SVG file to get the content bounding box from viewBox.

    Args:
        svg_path: Path to the SVG file

    Returns:
        Tuple of (min_x, min_y, width, height) or None if not determinable
    """
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()

        # Try to get viewBox attribute
        viewbox = root.get("viewBox")
        if viewbox:
            parts = viewbox.split()
            if len(parts) == 4:
                return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        # Fallback to width/height attributes
        width = root.get("width")
        height = root.get("height")
        if width and height:
            # Remove 'px' suffix if present
            w = float(width.replace("px", ""))
            h = float(height.replace("px", ""))
            return (0, 0, w, h)

        return None
    except Exception:
        return None


CONTENT_PADDING = 20  # Padding around content bounds for SVG viewBox


def _svg_from_paths(paths: list, all_coords: list) -> Optional[str]:
    """Build SVG string with viewBox computed from actual content bounds."""
    if not paths or not all_coords:
        return None

    xs = [c[0] for c in all_coords]
    ys = [c[1] for c in all_coords]
    min_x = min(xs) - CONTENT_PADDING
    min_y = min(ys) - CONTENT_PADDING
    w = max(xs) - min_x + CONTENT_PADDING
    h = max(ys) - min_y + CONTENT_PADDING

    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{min_x:.0f} {min_y:.0f} {w:.0f} {h:.0f}" '
        f'width="{w:.0f}" height="{h:.0f}">'
        f"{''.join(paths)}</svg>"
    )


# Pen stroke widths in page units. v6 points store width as a fixed-point
# value, four times the page-unit width (rmscene reads v1 float widths as
# ``round(width * 4)``); v5 segments store page units directly.
V6_POINT_WIDTH_SCALE = 0.25
MIN_PEN_STROKE_WIDTH = 0.3
MAX_PEN_STROKE_WIDTH = 100.0


def _pen_width(width: float) -> float:
    return max(MIN_PEN_STROKE_WIDTH, min(width, MAX_PEN_STROKE_WIDTH))


def _pen_stroke_paths(
    points: List[Tuple[float, float, float]], stroke_color: str, attrs: str = ""
) -> List[str]:
    """SVG paths for a pen stroke given ``(x, y, width)`` points in page units.

    Consecutive points of equal width (to 0.05 units) share one path, so a
    constant-width pen yields a single path while pressure-sensitive pens keep
    their width variation along the stroke.
    """

    def path(run: list, width: float) -> str:
        d = f"M {run[0][0]:.1f} {run[0][1]:.1f}"
        d += "".join(f" L {x:.1f} {y:.1f}" for x, y, _ in run[1:])
        return (
            f'<path d="{d}" stroke="{stroke_color}" '
            f'stroke-width="{width:.2f}" '
            f'fill="none" stroke-linecap="round" '
            f'stroke-linejoin="round"{attrs}/>'
        )

    paths = []
    run = [points[0]]
    run_width = round(_pen_width(points[0][2]) * 20) / 20
    for point in points[1:]:
        width = round(_pen_width(point[2]) * 20) / 20
        if width != run_width and len(run) > 1:
            paths.append(path(run, run_width))
            run = [run[-1]]
            run_width = width
        run.append(point)
    paths.append(path(run, run_width))
    return paths


def _render_rm_v5_to_svg(rm_file_path: Path) -> Optional[str]:
    """
    Render a v5 .rm file (reMarkable .lines format) to SVG.

    The v5 binary format stores layers of strokes, where each stroke
    has pen/color metadata and a sequence of (x, y, speed, tilt, width,
    pressure) segments.
    """
    import struct

    # Pen IDs that are highlighters (both old and v5 mappings)
    HIGHLIGHTER_PENS = {5, 18}
    # Pen IDs that are erasers — skip rendering
    ERASER_PENS = {6, 7, 8}

    # Color mapping by pen type
    STROKE_COLORS = {0: "black", 1: "gray", 2: "white"}
    HIGHLIGHT_COLORS = {0: "#FFD700", 1: "#FFD700", 2: "#FFD700"}

    try:
        with open(rm_file_path, "rb") as f:
            header = f.read(43)
            if b"version=5" not in header:
                return None

            nlayers = struct.unpack("<I", f.read(4))[0]
            paths = []
            all_coords = []

            for _ in range(nlayers):
                nstrokes = struct.unpack("<I", f.read(4))[0]

                for _ in range(nstrokes):
                    pen, color, _pad, _w, _unk, nsegments = struct.unpack("<IIIIfI", f.read(24))

                    segments = []
                    for _ in range(nsegments):
                        x, y, speed, tilt, width, pressure = struct.unpack("<ffffff", f.read(24))
                        segments.append((x, y, width, pressure))

                    if not segments:
                        continue

                    # Skip eraser strokes
                    if pen in ERASER_PENS:
                        continue

                    all_coords.extend((s[0], s[1]) for s in segments)
                    if pen not in HIGHLIGHTER_PENS:
                        paths.extend(
                            _pen_stroke_paths(
                                [(s[0], s[1], s[2]) for s in segments],
                                STROKE_COLORS.get(color, "black"),
                            )
                        )
                        continue

                    stroke_color = HIGHLIGHT_COLORS.get(color, "#FFD700")
                    avg_width = sum(s[2] for s in segments) / len(segments)
                    stroke_width = max(10.0, min(avg_width * 2.0, 40.0))
                    opacity = f" {HIGHLIGHT_BLEND}"

                    d = f"M {segments[0][0]:.1f} {segments[0][1]:.1f}"
                    d += "".join(f" L {s[0]:.1f} {s[1]:.1f}" for s in segments[1:])

                    paths.append(
                        f'<path d="{d}" stroke="{stroke_color}" '
                        f'stroke-width="{stroke_width:.1f}" '
                        f'fill="none" stroke-linecap="round" '
                        f'stroke-linejoin="round"{opacity}/>'
                    )

        return _svg_from_paths(paths, all_coords)
    except Exception:
        return None


def _v6_blocks(rm_file_path: Path) -> Optional[list]:
    """Read v6 .rm scene blocks, or None if the file is not v6 / unreadable."""
    try:
        from rmscene import read_blocks
    except ImportError:
        return None
    try:
        with open(rm_file_path, "rb") as f:
            header = f.read(43)
            if b"version=6" not in header:
                return None
            f.seek(0)
            return list(read_blocks(f))
    except Exception:
        return None


def _v6_paper_size(blocks: list) -> Tuple[float, float]:
    """Page extent (W, H) in stroke units, read from SceneInfo.paper_size.

    This is the authoritative stroke-coordinate extent for the page and the
    same value the write path (``strokes.page_geometry``) maps normalized
    coordinates into. Building a full-page render from it therefore makes the
    displayed image share ONE coordinate space with written strokes (so the
    drawing overlay and the write tool land ink in the same place). Falls back
    to the standard reMarkable page when no SceneInfo is present.
    """
    for b in blocks:
        ps = getattr(b, "paper_size", None)
        if ps and len(ps) == 2 and ps[0] and ps[1]:
            try:
                return float(ps[0]), float(ps[1])
            except (TypeError, ValueError):
                continue
    return float(REMARKABLE_WIDTH), float(REMARKABLE_HEIGHT)


def _is_scene_group(item: Any) -> bool:
    """True for an rmscene ``Group`` (a layer or a nested group)."""
    return hasattr(item, "node_id") and hasattr(getattr(item, "children", None), "values")


def _scene_items(node: Any):
    """Yield ``(item, group_node_id)`` for every leaf item below ``node``."""
    for child in node.children.values():
        if _is_scene_group(child):
            yield from _scene_items(child)
        else:
            yield child, node.node_id


def _lww(value: Any, default: Any) -> Any:
    """Value of an rmscene ``LwwValue`` (or a plain value)."""
    value = getattr(value, "value", value)
    return default if value is None else value


def _v6_scene(rm_file_path: Path) -> Optional[Tuple[list, Any]]:
    """``(blocks, scene_tree)`` for a v6 page, or None if it cannot be parsed."""
    blocks = _v6_blocks(rm_file_path)
    if blocks is None:
        return None
    tree = _v6_tree(blocks)
    return None if tree is None else (blocks, tree)


def _v6_tree(blocks: list) -> Optional[Any]:
    """Build an rmscene ``SceneTree`` from v6 blocks, or None on failure."""
    try:
        from rmscene.scene_stream import build_tree
        from rmscene.scene_tree import SceneTree

        tree = SceneTree()
        build_tree(tree, blocks)
    except Exception:
        return None
    return tree


def _v6_layer_order(blocks: list) -> List[Any]:
    """Node ids of a page's live layers, in tablet order.

    Layers are the groups placed directly under the scene root, ordered as a
    CRDT sequence. Moving a layer deletes its old group item and inserts a new
    one, so the sequence is ordered with its deleted items in place (rmscene's
    tree drops them, which breaks the order). Deleting a layer writes a
    scene-tree entry moving the layer's node under (0, 0); the last entry for a
    node wins.
    """
    try:
        from rmscene.crdt_sequence import toposort_items
        from rmscene.scene_stream import SceneGroupItemBlock, SceneTreeBlock
        from rmscene.tagged_block_common import CrdtId
    except ImportError:
        return []
    root = CrdtId(0, 1)
    detached = CrdtId(0, 0)

    parents: dict = {}
    items = []
    for block in blocks:
        if isinstance(block, SceneTreeBlock):
            node = block.node_id if block.node_id != detached else block.tree_id
            parents[node] = block.parent_id
        elif isinstance(block, SceneGroupItemBlock) and block.parent_id == root:
            items.append(block.item)

    values = {item.item_id: item.value for item in items}
    order = []
    for item_id in toposort_items(items):
        node = values.get(item_id)
        if node is None or node in order or parents.get(node, root) == detached:
            continue
        order.append(node)
    return order


def _scene_layers(tree: Any, blocks: list) -> List[Dict[str, Any]]:
    """Live layers of a page, in tablet order.

    Each layer is ``{"name", "visible", "group"}``; layers without a label are
    named ``Layer N`` by position, as the tablet shows them.
    """
    layers = []
    for node_id in _v6_layer_order(blocks):
        try:
            group = tree[node_id]
        except (KeyError, TypeError):
            continue
        if not _is_scene_group(group):
            continue
        label = _lww(getattr(group, "label", None), "")
        layers.append(
            {
                "name": str(label) if label else f"Layer {len(layers) + 1}",
                "visible": bool(_lww(getattr(group, "visible", None), True)),
                "group": group,
            }
        )
    return layers


def _page_scene_items(tree: Any, layers: List[Dict[str, Any]]):
    """Yield ``(item, group_node_id)`` for items on live layers or at the root."""
    for layer in layers:
        yield from _scene_items(layer["group"])
    root = tree.root
    for child in root.children.values():
        if not _is_scene_group(child):
            yield child, root.node_id


# Layer selections for rendering: visible layers only (the default), every
# layer, or a single layer given by name.
LAYERS_VISIBLE = "visible"
LAYERS_ALL = "all"


def _v6_layer_node_filter(blocks: list, layers: Optional[str]) -> Optional[Callable[[Any], bool]]:
    """Predicate on an item's group node id: should it be drawn?

    Returns None when every item should be drawn: ``layers="all"``, or the page
    has no layer structure rmscene can rebuild. Items whose group is outside
    every layer are drawn unless a single layer is requested.
    """
    selection = (layers or LAYERS_VISIBLE).strip()
    if selection.lower() == LAYERS_ALL:
        return None
    tree = _v6_tree(blocks)
    if tree is None:
        return None
    page_layers = _scene_layers(tree, blocks)
    if not page_layers:
        return None

    def subtree_nodes(group) -> set:
        nodes = {group.node_id}
        for child in group.children.values():
            if _is_scene_group(child):
                nodes |= subtree_nodes(child)
        return nodes

    if selection.lower() == LAYERS_VISIBLE:
        hidden: set = set()
        for layer in page_layers:
            if not layer["visible"]:
                hidden |= subtree_nodes(layer["group"])
        return lambda node_id: node_id not in hidden

    wanted = selection.lower()
    allowed: set = set()
    for layer in page_layers:
        if layer["name"].lower() == wanted:
            allowed |= subtree_nodes(layer["group"])
    return lambda node_id: node_id in allowed


def v6_page_layers(rm_file_path: Path) -> List[Dict[str, Any]]:
    """``[{"name", "visible"}]`` for a v6 page's layers, in tablet order."""
    scene = _v6_scene(rm_file_path)
    if scene is None:
        return []
    blocks, tree = scene
    return [{"name": la["name"], "visible": la["visible"]} for la in _scene_layers(tree, blocks)]


def _v6_group_offsets(blocks: list, anchor_pos: dict) -> dict:
    """Map group node ``CrdtId``s to cumulative ``(dx, dy)`` anchor offsets.

    In the v6 format a group of strokes can be anchored to a typed-text
    character (``Group.anchor_id``): its ink is stored relative to that anchor
    and the device translates it to the anchored line's Y as text reflows.
    ``anchor_pos`` comes from :func:`_v6_text_elements_with_bounds`. Offsets
    accumulate up the parent chain so groups nested under an anchored group
    inherit its translation. Returns ``{}`` when there is nothing to anchor.
    """
    if not anchor_pos:
        return {}

    parents: dict = {}
    own: dict = {}
    for block in blocks:
        # SceneTreeBlock links a node to its parent.
        tree_id = getattr(block, "tree_id", None)
        if tree_id is not None and hasattr(block, "parent_id"):
            parents[tree_id] = block.parent_id
        # TreeNodeBlock carries the group's anchor attributes.
        group = getattr(block, "group", None)
        if group is None:
            continue
        anchor_id = getattr(group, "anchor_id", None)
        if anchor_id is None:
            continue
        anchor_key = getattr(anchor_id, "value", None)
        if anchor_key not in anchor_pos:
            continue
        origin_x = getattr(group, "anchor_origin_x", None)
        dx = float(origin_x.value) if origin_x is not None else 0.0
        own[group.node_id] = (dx, float(anchor_pos[anchor_key]))

    if not own:
        return {}

    offsets: dict = {}

    def resolve(node_id) -> Tuple[float, float]:
        if node_id in offsets:
            return offsets[node_id]
        dx = dy = 0.0
        cur = node_id
        seen = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur in own:
                odx, ody = own[cur]
                dx += odx
                dy += ody
            cur = parents.get(cur)
        offsets[node_id] = (dx, dy)
        return offsets[node_id]

    for node_id in set(parents) | set(own):
        resolve(node_id)
    return offsets


def _page_image_dir(rm_file_path: Path) -> Path:
    """Folder holding a page's pasted images: ``<doc id>/<page id>/``."""
    return rm_file_path.with_suffix("")


def _v6_draw_order(blocks: list) -> Dict[Any, int]:
    """Drawing rank of each scene item, bottom to top.

    The tablet draws a page's layers in order, and within a layer (or nested
    group) draws its items in their CRDT sequence order, so a stroke written
    over an image covers it. Moving an item deletes it and inserts it again
    at the end, which brings it to the top. Deleted items stay in the sequence
    as placeholders so the order around them is kept. Items outside the scene
    tree are not ranked.
    """
    try:
        from rmscene.crdt_sequence import toposort_items
        from rmscene.scene_stream import SceneGroupItemBlock, SceneItemBlock
        from rmscene.tagged_block_common import CrdtId

        from remarkable_mcp.images import image_sequence_items
    except ImportError:
        return {}

    children: Dict[Any, Dict[Any, Any]] = {}
    groups: Dict[Any, Any] = {}
    for block in blocks:
        if isinstance(block, SceneItemBlock):
            children.setdefault(block.parent_id, {})[block.item.item_id] = block.item
            if isinstance(block, SceneGroupItemBlock) and block.item.value is not None:
                groups[block.item.item_id] = block.item.value
    for parent_id, item in image_sequence_items(blocks):
        children.setdefault(parent_id, {})[item.item_id] = item

    rank: Dict[Any, int] = {}
    visited: set = set()

    def visit(node: Any) -> None:
        if node in visited:
            return
        visited.add(node)
        items = children.get(node, {})
        for item_id in toposort_items(items.values()):
            if items[item_id].value is None:
                continue
            if item_id in groups:
                visit(groups[item_id])
            else:
                rank[item_id] = len(rank)

    visit(CrdtId(0, 1))
    return rank


def _v6_image_elements(
    blocks: list,
    image_dir: Optional[Path],
    draw_node: Optional[Callable[[Any], bool]],
    group_offsets: dict,
) -> List[Tuple[Any, list, list]]:
    """``(item_id, elements, coords)`` for each image pasted on a page.

    ``image_dir`` is the page's image folder (``<doc id>/<page id>``). Each
    image is drawn as an SVG ``<image>`` by mapping its texture corners onto
    the page, so scaled or rotated pastes land where the tablet shows them.
    """
    import base64

    from remarkable_mcp.images import pasted_images

    if image_dir is None:
        return []
    drawn: List[Tuple[Any, list, list]] = []
    for image in pasted_images(blocks).values():
        if draw_node is not None and not draw_node(image.parent_id):
            continue
        corners = image.corners()
        if corners is None or not image.file_name:
            continue
        path = image_dir / Path(image.file_name).name
        try:
            data = path.read_bytes()
        except OSError:
            continue
        dx, dy = group_offsets.get(image.parent_id, (0.0, 0.0))
        (x0, y0), (x1, y1), (x2, y2) = corners
        matrix = (
            f"{x1 - x0:.3f} {y1 - y0:.3f} {x2 - x0:.3f} {y2 - y0:.3f} "
            f"{x0 + dx:.3f} {y0 + dy:.3f}"
        )
        href = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        element = (
            f'<image width="1" height="1" preserveAspectRatio="none" '
            f'transform="matrix({matrix})" href="{href}"/>'
        )
        coords = [(x + dx, y + dy) for x, y, _, _ in image.vertices]
        drawn.append((image.item_id, [element], coords))
    return drawn


def _v6_paths_from_blocks(
    blocks: list,
    anchor_pos: Optional[dict] = None,
    layers: Optional[str] = None,
    image_dir: Optional[Path] = None,
) -> Tuple[list, list]:
    """Build SVG ``<path>`` strings + a flat coordinate list from v6 blocks.

    ``anchor_pos`` (from :func:`_v6_text_elements_with_bounds`) enables
    text-anchored ink translation: strokes whose parent group is anchored to a
    typed-text line are shifted to that line's position, matching where the
    device draws them. Without it strokes render at their stored coordinates,
    which for anchored groups is on top of the typed text block.

    ``layers`` selects which layers to draw: ``"visible"`` (default) draws the
    layers shown on the tablet, ``"all"`` includes hidden layers, and any other
    value draws only the layer with that name (case-insensitive).

    ``image_dir`` is the page's image folder; pasted images found there are
    drawn too. Items are drawn in the tablet's stacking order (see
    :func:`_v6_draw_order`); items it cannot rank follow in file order.
    """
    # Integer pen/color values (rmscene exposes ints on blocks).
    HIGHLIGHTER_PENS = {5, 18}  # HIGHLIGHTER_1, HIGHLIGHTER_2
    ERASER_PENS = {6, 8}  # ERASER, ERASER_AREA

    group_offsets = _v6_group_offsets(blocks, anchor_pos) if anchor_pos else {}
    draw_node = _v6_layer_node_filter(blocks, layers)

    draw_rank = _v6_draw_order(blocks)
    # (sort key, svg elements, coordinates) per drawn item
    drawn: list = []

    def add(item_id: Any, elements: list, coords: list) -> None:
        rank = draw_rank.get(item_id)
        key = (0, rank) if rank is not None else (1, len(drawn))
        drawn.append((key, elements, coords))

    for item_id, elements, coords in _v6_image_elements(
        blocks, image_dir, draw_node, group_offsets
    ):
        add(item_id, elements, coords)

    for block in blocks:
        if not hasattr(block, "item") or not hasattr(block.item, "value"):
            continue
        if draw_node is not None and not draw_node(getattr(block, "parent_id", None)):
            continue
        paths: list = []
        all_coords: list = []
        add(getattr(block.item, "item_id", None), paths, all_coords)
        line = block.item.value
        dx, dy = group_offsets.get(getattr(block, "parent_id", None), (0.0, 0.0))

        # Text highlights are GlyphRange items: they carry `rectangles`
        # (highlight boxes over selected text) rather than stroke `points`.
        # Emit a translucent filled rect per rectangle in the highlight colour.
        if getattr(line, "rectangles", None):
            hl_fill = render_color(
                getattr(line, "color", 9), getattr(line, "color_rgba", None), "#FEFD60"
            )
            hl_opacity = highlight_opacity(getattr(line, "color", 9))
            for r in line.rectangles:
                paths.append(
                    f'<rect x="{r.x + dx:.1f}" y="{r.y + dy:.1f}" '
                    f'width="{r.w:.1f}" height="{r.h:.1f}" '
                    f'fill="{hl_fill}" stroke="none"{hl_opacity} {HIGHLIGHT_BLEND}/>'
                )
                all_coords.append((r.x + dx, r.y + dy))
                all_coords.append((r.x + dx + r.w, r.y + dy + r.h))
            continue

        if not hasattr(line, "points") or not line.points:
            continue

        tool = line.tool if hasattr(line, "tool") else None
        color = line.color if hasattr(line, "color") else 0
        # Convert enums to int if needed
        tool = tool.value if hasattr(tool, "value") else tool
        color = color.value if hasattr(color, "value") else color

        if tool in ERASER_PENS:
            continue

        stroke_color = render_color(color, getattr(line, "color_rgba", None))
        all_coords.extend((p.x + dx, p.y + dy) for p in line.points)

        if tool not in HIGHLIGHTER_PENS:
            paths.extend(
                _pen_stroke_paths(
                    [
                        (p.x + dx, p.y + dy, getattr(p, "width", 8) * V6_POINT_WIDTH_SCALE)
                        for p in line.points
                    ],
                    stroke_color,
                )
            )
            continue

        avg_width = (
            sum(p.width for p in line.points) / len(line.points)
            if all(hasattr(p, "width") for p in line.points)
            else 20.0
        )
        stroke_width = max(10.0, min(avg_width * 2.0, 40.0))
        opacity = f"{highlight_opacity(color)} {HIGHLIGHT_BLEND}"

        d = f"M {line.points[0].x + dx:.1f} {line.points[0].y + dy:.1f}"
        d += "".join(f" L {p.x + dx:.1f} {p.y + dy:.1f}" for p in line.points[1:])

        paths.append(
            f'<path d="{d}" stroke="{stroke_color}" '
            f'stroke-width="{stroke_width:.1f}" '
            f'fill="none" stroke-linecap="round" '
            f'stroke-linejoin="round"{opacity}/>'
        )

    drawn.sort(key=lambda entry: entry[0])
    paths = [element for _, elements, _ in drawn for element in elements]
    all_coords = [point for _, _, coords in drawn for point in coords]
    return paths, all_coords


# reMarkable typed-text layout, in raw stroke units. Paragraph k of the text
# block is anchored at pos_y + _TEXT_TOP_Y + 70 * k and drawn with its baseline
# one line height below that anchor; ink anchored to the top or bottom of the
# text (the special anchor ids) sits at the first anchor or after the last
# paragraph. The top offset is measured from a device-written notebook page:
# the tablet's own highlight rectangle over a typed line, and the dot-grid
# template visible in captures of the page, both place the first anchor at
# pos_y + 63 (rmc uses -88 and an earlier calibration here used -39; both put
# ink anchored to the text top above the page). Values are in the reference
# 1404x1872 space and are scaled by the page's real height (see
# ``_TEXT_REF_PAGE_HEIGHT``) so they adapt to other geometries.
_TEXT_REF_PAGE_HEIGHT = 1872.0
_TEXT_TOP_Y = 63.0
_TEXT_DEFAULT_LINE_HEIGHT = 70.0
_TEXT_DEFAULT_FONT_SIZE = 30.0
# Continuation lines of a wrapped paragraph are spaced tighter than the gap
# between paragraphs (device uses ~44 vs ~70 units for plain text).
_TEXT_WRAP_LINE_RATIO = 44.0 / 70.0
# Average glyph advance as a fraction of font size, used only to wrap a
# paragraph to the text-box width (SVG <text> does not wrap on its own).
_TEXT_CHAR_ADVANCE = 0.5


# The tablet sets typed text in reMarkable Sans, a proprietary font. Noto Sans
# (from the pymupdf-fonts package) at the same size comes closest in letter
# widths among open fonts, within about 2% per word; lines carrying a
# highlight are then fitted to the tablet's highlight boxes (see
# _fit_lines_to_highlights). Without the package, text falls back to a generic
# sans-serif ``<text>`` element.
_TYPED_FONTS = {False: "notos", True: "notosbo"}


@functools.lru_cache(maxsize=None)
def _typed_font(bold: bool) -> Optional[Any]:
    """The tablet's typed-text font as a PyMuPDF font, or None if unavailable."""
    try:
        import fitz

        font = fitz.Font(_TYPED_FONTS[bold])
    except Exception:
        return None
    return font if "noto sans" in font.name.lower() else None


def _typed_font_buffer(name: str) -> Optional[bytes]:
    font = _typed_font(name == _TYPED_FONTS[True])
    return font.buffer if font is not None else None


def _typed_text_width(text: str, size: float, bold: bool) -> float:
    """Width of a typed line in stroke units, in the tablet's font when available."""
    font = _typed_font(bold)
    if font is not None:
        return font.text_length(text, fontsize=size)
    return len(text) * size * _TEXT_CHAR_ADVANCE


def _typed_text_outline(text: str, size: float, bold: bool, key: str) -> Optional[str]:
    """One typed line as glyph outlines in the tablet's font, or None.

    PyMuPDF sets the line on a scratch page and exports it with glyphs as
    paths, so PNG and SVG renders keep the letter widths the tablet uses. The
    returned SVG content has its origin at the start of the line's baseline;
    ``key`` makes its glyph ids unique within the page.
    """
    buffer = _typed_font_buffer(_TYPED_FONTS[bold])
    if buffer is None:
        return None
    try:
        import fitz
    except ImportError:
        return None
    # Scratch page with the baseline at (pad, 2 * size), ample room around it.
    pad = size
    doc = fitz.open()
    try:
        page = doc.new_page(width=_typed_text_width(text, size, bold) + 2 * pad, height=3 * size)
        page.insert_font(fontname=_TYPED_FONTS[bold], fontbuffer=buffer)
        page.insert_text(
            (pad, 2 * size), text, fontsize=size, fontname=_TYPED_FONTS[bold], color=(0, 0, 0)
        )
        svg = page.get_svg_image(text_as_path=True)
    except Exception:
        return None
    finally:
        doc.close()
    inner = svg[svg.index(">") + 1 : svg.rindex("</svg>")]
    # Plain href (SVG 2) needs no xlink namespace on the page's root element.
    inner = inner.replace("xlink:href=", "href=")
    inner = re.sub(r'(id="|href="#)', rf"\1typed-{key}-", inner)
    return f'<g transform="translate({-pad:g} {-2 * size:g})">{inner}</g>'


def _wrap_text(
    text: str,
    max_width: float,
    char_advance: float,
    measure: Optional[Callable[[str], float]] = None,
) -> List[str]:
    """Greedily word-wrap ``text`` to ``max_width`` (stroke units).

    SVG ``<text>`` does not wrap, so paragraphs wider than the text box are
    split here the way the device wraps them. Line width comes from
    ``measure`` when given, otherwise it is approximated as
    ``len(line) * char_advance``. A single word longer than the box is left on
    its own line rather than split. Returns ``[text]`` when no wrapping applies.
    """
    if max_width <= 0 or (measure is None and char_advance <= 0):
        return [text]
    width = measure or (lambda line: len(line) * char_advance)
    lines: List[str] = []
    cur = ""
    for word in text.split(" "):
        trial = word if not cur else f"{cur} {word}"
        if not cur or width(trial) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _v6_text_svg_elements(blocks: list) -> list:
    """Build SVG ``<text>`` strings for typed text (a RootTextBlock) on a page.

    Thin wrapper around :func:`_v6_text_elements_with_bounds` for callers (e.g.
    the full-page render) that fix the viewBox to the page extent and so do not
    need the text's bounding coordinates.
    """
    return _v6_text_elements_with_bounds(blocks)[0]


# Special anchor CrdtIds the device uses for ink groups pinned to the page
# itself rather than to a typed character: (0, 281474976710654) anchors at the
# top of the text block, (0, 281474976710655) below its last line.
_ANCHOR_TEXT_TOP = (0, 281474976710654)
_ANCHOR_TEXT_BOTTOM = (0, 281474976710655)


def _v6_text_elements_with_bounds(blocks: list) -> Tuple[list, list, dict]:
    """Build SVG ``<text>`` strings, bounding coords and anchor map for typed text.

    Returns ``(elements, coords, anchor_pos)``. ``coords`` is a flat list of
    ``(x, y)`` extent points (in the page's stroke/screen units, center-origin
    X) so a content-cropped render can size its viewBox to include the text.
    ``anchor_pos`` maps text-character ``CrdtId``s (plus the special
    top/bottom-of-text ids) to the page-space Y of the line holding that
    character: ink groups anchored to a character must be translated by that Y
    (see :func:`_v6_paths_from_blocks`), which is how the device keeps
    handwriting positioned relative to text as the typed block reflows.
    Returns ``([], [], {})`` when the page has no typed text (the common case
    for handwritten notebooks) or when rmscene's text helpers are unavailable.
    """
    try:
        from rmscene.scene_items import ParagraphStyle, Text
        from rmscene.tagged_block_common import CrdtId
        from rmscene.text import TextDocument
    except ImportError:
        return [], [], {}

    text_item = next(
        (b.value for b in blocks if isinstance(getattr(b, "value", None), Text)),
        None,
    )
    if text_item is None:
        return [], [], {}

    # Blank pages we synthesize carry an empty RootTextBlock; skip them so we
    # neither emit empty <text> nodes nor trigger rmscene's empty-item warning.
    try:
        if not any(isinstance(v, str) and v.strip() for v in text_item.items.values()):
            return [], [], {}
    except Exception:
        pass

    line_heights = {
        ParagraphStyle.PLAIN: 70.0,
        ParagraphStyle.HEADING: 150.0,
        ParagraphStyle.BOLD: 70.0,
        ParagraphStyle.BULLET: 35.0,
        ParagraphStyle.BULLET2: 35.0,
        ParagraphStyle.CHECKBOX: 35.0,
        ParagraphStyle.CHECKBOX_CHECKED: 35.0,
    }
    font_sizes = {
        ParagraphStyle.HEADING: 60.0,
        ParagraphStyle.BOLD: 34.0,
    }

    try:
        doc = TextDocument.from_scene_item(text_item)
    except Exception:
        return [], [], {}

    pos_x = float(getattr(text_item, "pos_x", 0.0) or 0.0)
    pos_y = float(getattr(text_item, "pos_y", 0.0) or 0.0)
    box_width = float(getattr(text_item, "width", 0.0) or 0.0)

    # Scale the reference metrics to the page's real height so text on devices
    # that normalize to a different coordinate space (e.g. reMarkable Move)
    # stays proportional. Identity for the standard 1404x1872 page.
    _, paper_h = _v6_paper_size(blocks)
    scale = paper_h / _TEXT_REF_PAGE_HEIGHT if paper_h else 1.0

    elements: list = []
    coords: list = []
    lines: List[Dict[str, Any]] = []
    anchor_pos: dict = {}
    y_offset = _TEXT_TOP_Y * scale
    anchor_pos[CrdtId(*_ANCHOR_TEXT_TOP)] = pos_y + y_offset
    for para in doc.contents:
        style = para.style.value if getattr(para, "style", None) is not None else None
        line_height = line_heights.get(style, _TEXT_DEFAULT_LINE_HEIGHT) * scale
        size = font_sizes.get(style, _TEXT_DEFAULT_FONT_SIZE) * scale
        text = str(para).strip()
        # Every character in this paragraph anchors ink at the paragraph's own
        # Y (the running offset before this paragraph consumes its lines), the
        # same reference point the device and the rmc exporter use.
        para_anchor_y = pos_y + y_offset
        try:
            anchor_pos[para.start_id] = para_anchor_y
            for subpara in para.contents:
                for char_id in getattr(subpara, "i", []) or []:
                    anchor_pos[char_id] = para_anchor_y
        except Exception:
            pass
        if not text:
            # A blank paragraph still consumes a line (e.g. spacing under a title).
            y_offset += line_height
            continue
        heading = style == ParagraphStyle.HEADING
        bold = style == ParagraphStyle.BOLD
        # Headings use a serif face the tablet ships but the renderer lacks;
        # other paragraphs are measured in the tablet's own font.
        if heading:
            measure = None
        else:
            measure = lambda line, size=size, bold=bold: _typed_text_width(  # noqa: E731
                line, size, bold
            )
        # Wrap long paragraphs to the text box; each wrapped line takes a line,
        # with continuation lines spaced tighter than a paragraph break.
        offset = 0
        for idx, line in enumerate(_wrap_text(text, box_width, size * _TEXT_CHAR_ADVANCE, measure)):
            y_offset += line_height if idx == 0 else line_height * _TEXT_WRAP_LINE_RATIO
            offset = text.find(line, offset)
            lines.append(
                {
                    "x": pos_x,
                    "baseline": pos_y + y_offset,
                    "text": line,
                    "size": size,
                    "bold": bold,
                    "heading": heading,
                    "paragraph": text,
                    "start": offset,
                }
            )
            offset += len(line)
    anchor_pos[CrdtId(*_ANCHOR_TEXT_BOTTOM)] = pos_y + y_offset
    _fit_lines_to_highlights(lines, _typed_text_highlights(blocks))

    for index, line in enumerate(lines):
        outline = (
            None
            if line["heading"]
            else _typed_text_outline(line["text"], line["size"], line["bold"], str(index))
        )
        if outline is not None:
            label = _xml_escape(line["text"], {'"': "&quot;"})
            stretch, shift = line.get("fit", (1.0, 0.0))
            fit = f" scale({stretch:.4f} 1)" if stretch != 1.0 else ""
            elements.append(
                f'<g class="typed-text" data-text="{label}" data-size="{line["size"]:.1f}" '
                f'transform="translate({line["x"] + shift:.1f} {line["baseline"]:.1f})'
                f'{fit}">'
                f"{outline}</g>"
            )
            continue
        family = "serif" if line["heading"] else "sans-serif"
        weight = ' font-weight="bold"' if line["heading"] or line["bold"] else ""
        elements.append(
            f'<text x="{line["x"]:.1f}" y="{line["baseline"]:.1f}" '
            f'font-family="{family}" font-size="{line["size"]:.1f}"{weight} '
            f'fill="black" xml:space="preserve">{_xml_escape(line["text"])}</text>'
        )
    for line in lines:
        # The line's extent for crop sizing, capped at the box width.
        width = (
            len(line["text"]) * line["size"] * _TEXT_CHAR_ADVANCE
            if line["heading"]
            else _typed_text_width(line["text"], line["size"], line["bold"])
        )
        if box_width > 0:
            width = min(width, box_width)
        stretch, shift = line.get("fit", (1.0, 0.0))
        baseline = line["baseline"]
        coords.append((line["x"] + shift, baseline - line["size"]))
        coords.append((line["x"] + shift + width * stretch, baseline + line["size"] * 0.3))
    return elements, coords, anchor_pos


def _typed_text_highlights(blocks: list) -> List[Tuple[str, list]]:
    """``(text, rectangles)`` of each live text highlight on a page.

    A later block for the same item replaces an earlier one; a block without a
    value deletes the item.
    """
    latest: Dict[Any, Any] = {}
    for block in blocks:
        item = getattr(block, "item", None)
        if item is not None and hasattr(item, "item_id"):
            latest[item.item_id] = getattr(item, "value", None)
    highlights = []
    for item in latest.values():
        if type(item).__name__ == "GlyphRange" and getattr(item, "text", None):
            rects = [r for r in getattr(item, "rectangles", None) or [] if r.w > 0]
            if rects:
                highlights.append((item.text, rects))
    return highlights


def _fit_lines_to_highlights(lines: List[Dict[str, Any]], highlights: list) -> None:
    """Fit typed lines to the tablet's highlight boxes over them.

    The file stores no line widths, only the text box; a highlight over typed
    text, however, stores the tablet-computed box of the highlighted words.
    For each line carrying such a highlight, sets ``line["fit"] = (stretch,
    shift)``: the line is moved sideways so each highlighted span sits
    centered in its box, as on the tablet, and squeezed only if a span is
    wider than its box. Boxes are padded at the sides, so spans are never
    stretched to fill them. The line's height on the page is left as the text
    layout places it. Lines in another font than the tablet's (no outlines
    available) are left alone.
    """
    for text, rects in highlights:
        for rect in rects:
            center = rect.y + rect.h / 2
            line = next(
                (
                    candidate
                    for candidate in lines
                    if not candidate["heading"]
                    and candidate["baseline"] - candidate["size"] * 1.5
                    <= center
                    <= candidate["baseline"] + candidate["size"] * 0.5
                ),
                None,
            )
            if line is None or _typed_font(line["bold"]) is None:
                continue
            span = _highlight_span_on_line(line, text)
            if span is None:
                continue
            first, last = span

            def width(chars: str, line=line) -> float:
                return _typed_text_width(chars, line["size"], line["bold"])

            start = width(line["text"][:first])
            span_width = width(line["text"][first:last])
            stretch = min(1.0, rect.w / span_width) if span_width > 0 else 1.0
            shift = (rect.x + rect.w / 2 - line["x"]) - stretch * (start + span_width / 2)
            line.setdefault("pairs", []).append((stretch, shift))
    for line in lines:
        pairs = line.pop("pairs", None)
        if pairs:
            stretch = min(p[0] for p in pairs)
            shift = sum(p[1] for p in pairs) / len(pairs)
            line["fit"] = (stretch, shift)


def _highlight_span_on_line(line: Dict[str, Any], text: str) -> Optional[Tuple[int, int]]:
    """Character range of a highlight's text within one wrapped line, or None.

    The highlight's text is located in the line's paragraph, so a highlight
    wrapping onto several lines yields its part on this line.
    """
    paragraph, line_start = line["paragraph"], line["start"]
    line_end = line_start + len(line["text"])
    position = paragraph.find(text.strip())
    while position != -1:
        end = position + len(text.strip())
        first, last = max(position, line_start), min(end, line_end)
        if first < last:
            return first - line_start, last - line_start
        position = paragraph.find(text.strip(), position + 1)
    return None


def _render_rm_v6_to_svg(rm_file_path: Path, layers: Optional[str] = None) -> Optional[str]:
    """
    Render a v6 .rm file to a content-cropped SVG (read-only viewing).

    This handles newer reMarkable firmware that uses the v6 format with
    scene tree blocks, including proper highlighter and color support. The
    viewBox is cropped to the ink bounding box; for a full-page render that
    shares the page's own coordinate space, see ``render_rm_file_full_page_png``.
    """
    blocks = _v6_blocks(rm_file_path)
    if blocks is None:
        return None
    try:
        text_elements, text_coords, anchor_pos = _v6_text_elements_with_bounds(blocks)
        paths, all_coords = _v6_paths_from_blocks(
            blocks, anchor_pos, layers, _page_image_dir(rm_file_path)
        )
        # Typed text is drawn under strokes (handwriting layers on top), and its
        # extent is folded into the crop so a text-only page still renders.
        return _svg_from_paths(text_elements + paths, all_coords + text_coords)
    except Exception:
        return None


def render_rm_file_to_png(
    rm_file_path: Path, background_color: Optional[str] = None, layers: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a .rm file to PNG image bytes.

    Uses the rmscene renderers to convert .rm to SVG, then PyMuPDF to convert to PNG.
    The output is sized based on the SVG content bounds with a margin.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", "transparent", None).
                         None means transparent. Use REMARKABLE_BACKGROUND_COLOR
                         for the standard reMarkable paper color.

    Returns:
        PNG image bytes, or None if rendering failed
    """
    tmp_svg_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)

        # Convert .rm to SVG via the rmscene renderers
        if not _rm_to_svg(rm_file_path, tmp_svg_path, layers):
            return None

        # Get content bounds from SVG
        bounds = _get_svg_content_bounds(tmp_svg_path)
        if bounds:
            # Use content bounds with margin
            _, _, content_width, content_height = bounds
            output_width = int(content_width) + 2 * CONTENT_MARGIN
            output_height = int(content_height) + 2 * CONTENT_MARGIN
        else:
            # Fallback to standard reMarkable dimensions
            output_width = REMARKABLE_WIDTH
            output_height = REMARKABLE_HEIGHT

        return _svg_string_to_png(
            tmp_svg_path.read_text(),
            output_width,
            output_height,
            background_color,
        )
    except Exception:
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)


def _svg_full_page(paths: list, paper_w: float, paper_h: float) -> str:
    """Wrap paths in an SVG whose viewBox spans the WHOLE page in stroke units.

    The page coordinate system is center-origin in X (x in [-W/2, W/2]) and
    top-origin in Y (y in [0, H]) — the same mapping ``strokes._map_point``
    uses (``rm_x = (nx-0.5)*W``, ``rm_y = ny*H``). So a point drawn at
    normalized (nx, ny) over the rendered image lands at exactly the stroke
    coordinate the write tool will use. Empty ``paths`` yields a blank page.
    """
    min_x = -paper_w / 2.0
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{min_x:.0f} 0 {paper_w:.0f} {paper_h:.0f}" '
        f'width="{paper_w:.0f}" height="{paper_h:.0f}">'
        f"{''.join(paths)}</svg>"
    )


def _svg_string_to_png(
    svg: str,
    output_width: int,
    output_height: int,
    background_color: Optional[str],
    backdrop: Any = None,
) -> Optional[bytes]:
    """Rasterize an SVG string to exact-size PNG bytes without system Cairo.

    ``backdrop`` (a PIL image, e.g. a rendered PDF page) is drawn under the
    SVG instead of ``background_color``. Highlighter ink (elements styled
    with ``HIGHLIGHT_BLEND``) darkens the background or backdrop under it,
    and the rest of the ink is drawn over that. With a transparent result
    and no backdrop, highlighter ink is drawn as it is, under the other ink.
    """
    import io

    try:
        import fitz
    except ImportError:
        logger.error(
            "SVG rasterization requires PyMuPDF. Reinstall remarkable-mcp so its "
            "runtime dependencies are available."
        )
        return None

    if output_width < 1 or output_height < 1:
        logger.error(
            "SVG rasterization requires positive output dimensions, got %sx%s.",
            output_width,
            output_height,
        )
        return None

    from PIL import Image as PILImage
    from PIL import ImageChops

    def rasterize(source: str):
        doc = fitz.open(stream=source.encode("utf-8"), filetype="svg")
        try:
            if len(doc) != 1:
                raise ValueError(f"expected one SVG page, found {len(doc)}")
            page = doc[0]
            if page.rect.width <= 0 or page.rect.height <= 0:
                raise ValueError(f"SVG has invalid dimensions: {page.rect}")
            matrix = fitz.Matrix(
                output_width / page.rect.width,
                output_height / page.rect.height,
            )
            pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=True)
        finally:
            doc.close()
        image = PILImage.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGBA")
        if image.size != (output_width, output_height):
            image = image.resize((output_width, output_height), PILImage.Resampling.LANCZOS)
        return image

    def darken(under, ink):
        """``under`` with ``ink`` drawn over it with the Darken blend.

        Each pixel moves toward min(under, ink) by the ink's alpha; where
        ``under`` is transparent the ink is drawn as it is.
        """
        base = under.convert("RGB")
        darkest = ImageChops.darker(base, ink.convert("RGB"))
        blended = PILImage.composite(darkest, base, ink.getchannel("A")).convert("RGBA")
        blended.putalpha(under.getchannel("A"))
        plain = PILImage.alpha_composite(under, ink)
        return PILImage.composite(blended, plain, under.getchannel("A"))

    try:
        # Split the drawing into runs of ordinary ink and of highlighter ink,
        # in drawing order, so each highlighter run darkens what is under it.
        close = svg.rindex("</svg>")
        body_start = svg.index(">", svg.index("<svg")) + 1
        runs: List[Tuple[bool, str]] = []
        pos = body_start
        for match in _DARKEN_ELEMENT.finditer(svg, body_start, close):
            runs.append((False, svg[pos : match.start()]))
            runs.append((True, match.group()))
            pos = match.end()
        runs.append((False, svg[pos:close]))
        merged: List[Tuple[bool, str]] = []
        for is_highlight, text in runs:
            if not text.strip():
                continue
            if merged and merged[-1][0] == is_highlight:
                merged[-1] = (is_highlight, merged[-1][1] + text)
            else:
                merged.append((is_highlight, text))

        size = (output_width, output_height)
        opaque = backdrop is not None
        if backdrop is None and background_color is not None:
            red, green, blue, alpha = _parse_hex_color(background_color)
            backdrop = PILImage.new("RGBA", size, (red, green, blue, alpha))
            opaque = alpha == 255
        if backdrop is not None:
            image = backdrop.convert("RGBA")
            if image.size != size:
                image = image.resize(size, PILImage.Resampling.LANCZOS)
        else:
            image = PILImage.new("RGBA", size, (0, 0, 0, 0))

        head, tail = svg[:body_start], svg[close:]
        for is_highlight, text in merged or [(False, "")]:
            layer = rasterize(head + text + tail)
            image = darken(image, layer) if is_highlight else PILImage.alpha_composite(image, layer)
        if opaque:
            image = image.convert("RGB")

        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception as exc:
        logger.error(
            "PyMuPDF could not rasterize SVG to PNG (%sx%s): %s",
            output_width,
            output_height,
            exc,
        )
        return None


def _extend_to_ink(paper_h: float, coords: list) -> float:
    """Page height grown to hold ink written below the page.

    On the tablet a page can be scrolled and written on past its
    ``paper_size``; the page then extends downward. Returns ``paper_h`` when
    all ink fits, otherwise the lowest ink plus ``INK_EXTENSION_MARGIN``.
    """
    if not coords:
        return paper_h
    bottom = max(c[1] for c in coords)
    if bottom <= paper_h:
        return paper_h
    return float(math.ceil(bottom + INK_EXTENSION_MARGIN))


def _v6_page_height(blocks: list, anchor_pos: Optional[dict], paper_h: float) -> float:
    """Page height including ink written below it, on any layer.

    Hidden layers count too, so the page frame (and ``region`` fractions)
    stays the same whichever layers are drawn.
    """
    _, coords = _v6_paths_from_blocks(blocks, anchor_pos, "all")
    return _extend_to_ink(paper_h, coords)


def render_rm_file_full_page_png(
    rm_file_path: Path,
    background_color: Optional[str] = None,
    layers: Optional[str] = None,
    dpi: Optional[float] = None,
    extend_to_ink: bool = True,
) -> Optional[Tuple[bytes, Tuple[float, float]]]:
    """Render a v6 .rm page to a FULL-PAGE PNG (not cropped to ink).

    Unlike ``render_rm_file_to_png`` (which crops the viewBox to the ink
    bounding box), this renders the whole page using a viewBox derived from the
    page's own ``SceneInfo.paper_size``. That makes the displayed image map
    linearly to the page's coordinate system, so the interactive drawing
    overlay places strokes exactly where the write tool will, and blank pages
    render as a blank page instead of returning ``None``.

    With ``extend_to_ink`` the page grows downward to include ink written
    below ``paper_size`` (see ``_extend_to_ink``), at the same scale; the
    origin and top of the page are unchanged. The interactive canvas passes
    False to keep the strict ``paper_size`` frame.

    Returns ``(png_bytes, (page_w, page_h))`` or ``None`` if the file is not
    v6 or rendering dependencies are unavailable (callers should fall back).
    """
    blocks = _v6_blocks(rm_file_path)
    if blocks is None:
        return None
    try:
        text_elements, _, anchor_pos = _v6_text_elements_with_bounds(blocks)
        paths, _ = _v6_paths_from_blocks(blocks, anchor_pos, layers, _page_image_dir(rm_file_path))
        paper_w, paper_h = _v6_paper_size(blocks)
    except Exception:
        return None

    page_h = _v6_page_height(blocks, anchor_pos, paper_h) if extend_to_ink else paper_h
    # Typed text is drawn first so handwritten strokes layer on top of it,
    # matching the device's compositing order.
    svg = _svg_full_page(text_elements + paths, paper_w, page_h)
    scale = _full_page_scale(paper_w, paper_h, dpi)
    output_width = max(1, round(paper_w * scale))
    output_height = max(1, round(page_h * scale))

    png = _svg_string_to_png(svg, output_width, output_height, background_color)
    if png is None:
        return None
    return png, (paper_w, page_h)


def render_rm_file_to_svg(
    rm_file_path: Path, background_color: Optional[str] = None, layers: Optional[str] = None
) -> Optional[str]:
    """
    Render a .rm file to SVG string.

    Uses the rmscene renderers to convert .rm to SVG, optionally adding a background.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        SVG content as string, or None if rendering failed
    """
    import subprocess
    import tempfile

    tmp_svg_path = None

    try:
        # Create temp file for SVG output
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)

        # Convert .rm to SVG via the rmscene renderers
        if not _rm_to_svg(rm_file_path, tmp_svg_path, layers):
            return None

        # Read SVG content
        svg_content = tmp_svg_path.read_text()

        # Add background rectangle if color specified
        if background_color:
            svg_content = _add_svg_background(svg_content, background_color)

        return svg_content

    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)


def _add_svg_background(svg_content: str, background_color: str) -> str:
    """Add a background rectangle to an SVG.

    Inserts a rect element as the first child of the SVG to act as background.

    Args:
        svg_content: Original SVG content
        background_color: Background color (e.g., "#FFFFFF")

    Returns:
        SVG content with background added
    """
    import re

    # Find the opening <svg> tag and its attributes
    svg_match = re.search(r"(<svg[^>]*>)", svg_content, re.IGNORECASE)
    if not svg_match:
        return svg_content

    svg_tag = svg_match.group(1)

    # Extract viewBox or width/height for the background rect dimensions
    viewbox_match = re.search(r'viewBox="([^"]*)"', svg_tag)
    if viewbox_match:
        viewbox = viewbox_match.group(1)
        parts = viewbox.split()
        if len(parts) == 4:
            x, y, width, height = parts
            bg_rect = (
                f'<rect x="{x}" y="{y}" width="{width}" '
                f'height="{height}" fill="{background_color}"/>'
            )
        else:
            # Fallback to full page
            bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'
    else:
        # No viewBox, use 100% dimensions
        bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'

    # Insert background rect right after the opening svg tag
    insert_pos = svg_match.end()
    return svg_content[:insert_pos] + bg_rect + svg_content[insert_pos:]


def _read_content_json(tmpdir_path: Path) -> Optional[dict]:
    """Parse the first ``.content`` file in an extracted document, or None."""
    for content_file in tmpdir_path.glob("*.content"):
        try:
            data = json.loads(content_file.read_text())
        except Exception:
            return None
        return data if isinstance(data, dict) else None
    return None


def _get_ordered_rm_files(tmpdir_path: Path) -> List[Path]:
    """Extract and order .rm files from an extracted document directory.

    Reads the .content file to determine page order and returns .rm files
    sorted accordingly. Falls back to filesystem order if no page order found.

    Args:
        tmpdir_path: Path to the extracted document directory

    Returns:
        List of .rm file paths in correct page order
    """
    # Get page order from .content file
    data = _read_content_json(tmpdir_path)
    page_order = content_page_ids(data)
    deleted = deleted_page_ids(data)

    rm_files = [f for f in tmpdir_path.glob("**/*.rm") if f.stem not in deleted]

    # Sort rm_files by page order if available
    if page_order:
        rm_by_id = {}
        for rm_file in rm_files:
            page_id = rm_file.stem
            rm_by_id[page_id] = rm_file

        ordered_rm_files = []
        for page_id in page_order:
            if page_id in rm_by_id:
                ordered_rm_files.append(rm_by_id[page_id])
        # Add any remaining files not in page order
        for rm_file in rm_files:
            if rm_file not in ordered_rm_files:
                ordered_rm_files.append(rm_file)
        return ordered_rm_files

    return rm_files


def _get_page_order(tmpdir_path: Path) -> List[str]:
    """Return the ordered list of page ids from the .content file.

    formatVersion 2 stores them under ``cPages.pages[].id``; formatVersion 1 as
    a flat ``pages`` list of ids. Pages deleted on the tablet are skipped.
    Returns an empty list if unavailable. This is the full page order
    (including pages that have no strokes), unlike
    ``_get_ordered_rm_files`` which is compacted to pages that do.
    """
    return content_page_ids(_read_content_json(tmpdir_path))


def _select_rm_file_for_page(tmpdir_path: Path, rm_files: List[Path], page: int) -> Optional[Path]:
    """Return the .rm stroke file for a 1-based page, matched by page id.

    ``_get_ordered_rm_files`` is compacted (only pages that actually have
    strokes), so indexing it by page number pairs the wrong ink with a page
    whenever an earlier page is un-annotated. Map through the full ``.content``
    page order and match by id instead. Returns None when the page has no
    strokes (a page with a PDF underlay but no annotation), or when the page is
    out of range; callers should treat None as "no annotation layer".
    """
    page_order = _get_page_order(tmpdir_path)
    if page_order:
        if 1 <= page <= len(page_order):
            page_id = page_order[page - 1]
            return next((p for p in rm_files if p.stem == page_id), None)
        return None
    # No page order available (unusual): fall back to positional selection.
    if 1 <= page <= len(rm_files):
        return rm_files[page - 1]
    return None


def get_page_layers_from_document_zip(zip_path: Path, page: int = 1) -> List[Dict[str, Any]]:
    """``[{"name", "visible"}]`` for a page's layers; empty if it has none."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)
        rm_files = _get_ordered_rm_files(tmpdir_path)
        rm_file = _select_rm_file_for_page(tmpdir_path, rm_files, page)
        if rm_file is None:
            return []
        return v6_page_layers(rm_file)


def render_page_from_document_zip_svg(
    zip_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    layers: Optional[str] = None,
) -> Optional[str]:
    """
    Render a specific page from a reMarkable document zip to SVG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        SVG content as string, or None if rendering failed or page doesn't exist
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)

        # Select the page's .rm by id (see _select_rm_file_for_page); None means
        # the page has no annotation layer.
        target_rm_file = _select_rm_file_for_page(tmpdir_path, rm_files, page)
        if target_rm_file is None:
            return None

        return render_rm_file_to_svg(
            target_rm_file, background_color=background_color, layers=layers
        )


def render_page_from_document_zip(
    zip_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    layers: Optional[str] = None,
) -> Optional[bytes]:
    """
    Render a specific page from a reMarkable document zip to PNG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        PNG image bytes, or None if rendering failed or page doesn't exist
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)

        # Select the page's .rm by id (see _select_rm_file_for_page); None means
        # the page has no annotation layer.
        target_rm_file = _select_rm_file_for_page(tmpdir_path, rm_files, page)
        if target_rm_file is None:
            return None

        return render_rm_file_to_png(
            target_rm_file, background_color=background_color, layers=layers
        )


def _document_paper_size(rm_files: List[Path]) -> Tuple[float, float]:
    """Best-effort page extent for a document by scanning its .rm files.

    Used to render a blank page (one with no .rm of its own) at the same size
    as the rest of the document. Falls back to the standard reMarkable page.
    """
    for p in rm_files:
        blocks = _v6_blocks(p)
        if blocks:
            return _v6_paper_size(blocks)
    return float(REMARKABLE_WIDTH), float(REMARKABLE_HEIGHT)


def render_page_full_page_from_document_zip(
    zip_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    layers: Optional[str] = None,
    dpi: Optional[float] = None,
    extend_to_ink: bool = True,
) -> Optional[Tuple[bytes, Tuple[float, float]]]:
    """Full-page render of a page, addressed by cPages index.

    This is the render used by the interactive canvas. It differs from
    ``render_page_from_document_zip`` in two ways that matter for write-back:

    1. Pages are addressed by the ``.content`` cPages order — the SAME index
       the write tool (``_page_ids_from_content``) uses — so "page N" means the
       same page in the viewer and the writer even when blank pages (which may
       have no ``.rm`` file) are present.
    2. The page is rendered full-bleed using its own ``SceneInfo.paper_size``,
       so the overlay's normalized coordinates map exactly onto stroke space.
       The canvas passes ``extend_to_ink=False`` to keep that frame; reads and
       exports extend the page downward to include ink written below it.

    Returns ``(png_bytes, (page_w, page_h))`` or ``None`` (caller falls back
    to PDF rasterization or an error).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        return render_page_full_page_from_extracted_document(
            tmpdir_path,
            page=page,
            background_color=background_color,
            layers=layers,
            dpi=dpi,
            extend_to_ink=extend_to_ink,
        )


def render_page_full_page_from_extracted_document(
    tmpdir_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    layers: Optional[str] = None,
    dpi: Optional[float] = None,
    extend_to_ink: bool = True,
) -> Optional[Tuple[bytes, Tuple[float, float]]]:
    """Render a full page from an already extracted document archive."""
    rm_files = _get_ordered_rm_files(tmpdir_path)
    page_order = _get_page_order(tmpdir_path)

    if page_order:
        if page < 1 or page > len(page_order):
            return None
        rm_file = _select_rm_file_for_page(tmpdir_path, rm_files, page)
    else:
        # No page metadata: fall back to filesystem order of .rm files.
        if page < 1 or page > len(rm_files):
            return None
        rm_file = rm_files[page - 1]

    if rm_file is not None and rm_file.exists():
        return render_rm_file_full_page_png(
            rm_file,
            background_color=background_color,
            layers=layers,
            dpi=dpi,
            extend_to_ink=extend_to_ink,
        )

    # The page exists in cPages but has no .rm layer yet (a blank page):
    # render a blank full page at the document's paper size so the viewer
    # still shows it. (The write tool will return no_page_layer until the
    # page has a drawable layer / has been added via remarkable_add_page.)
    paper_w, paper_h = _document_paper_size(rm_files)
    svg = _svg_full_page([], paper_w, paper_h)
    scale = _full_page_scale(paper_w, paper_h, dpi)
    png = _svg_string_to_png(
        svg, max(1, round(paper_w * scale)), max(1, round(paper_h * scale)), background_color
    )
    if png is None:
        return None
    return png, (paper_w, paper_h)


def document_zip_has_pdf_underlay(zip_path: Path) -> bool:
    """Check if a reMarkable document zip contains a PDF underlay.

    Args:
        zip_path: Path to the document zip file

    Returns:
        True if the zip contains a .pdf file
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return any(name.endswith(".pdf") for name in zf.namelist())
    except Exception:
        return False


def _read_cpages_entries(tmpdir_path: Path) -> List[Dict[str, Any]]:
    """Read cPages.pages entries from the .content metadata file.

    Args:
        tmpdir_path: Path to the extracted document directory

    Returns:
        List of cPages page entries for pages the tablet shows (deleted pages
        skipped), or empty list if not found
    """
    return live_cpages_entries(_read_content_json(tmpdir_path))


def _pdf_page_index_for_cpages_entry(entry: Dict[str, Any]) -> Optional[int]:
    """Get the 0-based PDF page index from a cPages entry's redir field.

    The redir.value field in a cPages entry maps the reMarkable page to
    the original PDF page number (0-based).

    Args:
        entry: A single cPages page entry dict

    Returns:
        0-based PDF page index, or None if no redirect exists or it is negative
        (a page with no PDF underlay)
    """
    redir = entry.get("redir", {})
    if isinstance(redir, dict) and "value" in redir:
        try:
            index = int(redir["value"])
        except (ValueError, TypeError):
            return None
        return index if index >= 0 else None
    return None


def _read_redirection_page_map(tmpdir_path: Path) -> List[Any]:
    """Read the legacy (formatVersion 1) redirectionPageMap from .content.

    formatVersion 1 documents have no cPages structure; instead the reMarkable
    page -> original PDF page mapping is a flat list where the index is the
    0-based reMarkable page and the value is the 0-based PDF page index (-1 for
    user-added pages). Many cloud-imported PDFs use this layout.

    Returns the list, or an empty list if not found.
    """
    content_file = next(tmpdir_path.glob("*.content"), None)
    if content_file is None:
        return []
    try:
        data = json.loads(content_file.read_text())
        rpm = data.get("redirectionPageMap")
        if isinstance(rpm, list):
            return rpm
    except Exception:
        pass
    return []


def _resolve_pdf_page_index(tmpdir_path: Path, page: int) -> Optional[int]:
    """Resolve the 0-based PDF page index for a 1-based reMarkable page.

    Handles both document layouts:
    - formatVersion 2: ``cPages.pages[i].redir.value``
    - formatVersion 1: ``redirectionPageMap[i]`` (legacy; used by many
      cloud-imported PDFs)

    Returns None only when the page genuinely has no PDF underlay (a user-added
    page). This lets render_merged composite annotations onto imported PDFs that
    use either layout, instead of falling back to an annotation-only render.

    When cPages entries exist they are authoritative: an entry without ``redir``
    is a user-added page, and we must NOT fall through to a redirectionPageMap a
    v1->v2 migrated document may still carry — its stale, order-shifted indices
    could composite the wrong PDF page under a user-added page.
    """
    # formatVersion 2 (cPages) — authoritative when present
    entries = _read_cpages_entries(tmpdir_path)
    if entries:
        if 1 <= page <= len(entries):
            return _pdf_page_index_for_cpages_entry(entries[page - 1])
        return None

    # formatVersion 1 (redirectionPageMap)
    rpm = _read_redirection_page_map(tmpdir_path)
    if rpm:
        if 1 <= page <= len(rpm):
            val = rpm[page - 1]
            if isinstance(val, int) and val >= 0:
                return val
        return None

    # Some formatVersion 1 documents (including files uploaded by this server)
    # contain a flat page-id list but omit redirectionPageMap. With no explicit
    # user-added-page markers, the source PDF order is the only authoritative
    # mapping and is therefore identity.
    page_order = _get_page_order(tmpdir_path)
    if page_order and 1 <= page <= len(page_order):
        return page - 1

    return None


def _render_pdf_page_to_png(
    pdf_bytes: bytes, page_index: int, width: int, height: int
) -> Optional[bytes]:
    """Rasterize a single PDF page to PNG bytes using PyMuPDF (fitz).

    Args:
        pdf_bytes: Raw PDF file bytes
        page_index: 0-based page index
        width: Target output width in pixels
        height: Target output height in pixels

    Returns:
        PNG image bytes, or None on failure
    """
    try:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            if page_index < 0 or page_index >= len(doc):
                return None

            pdf_page = doc[page_index]
            # Scale to fill the target dimensions
            mat = fitz.Matrix(width / pdf_page.rect.width, height / pdf_page.rect.height)
            pix = pdf_page.get_pixmap(matrix=mat, alpha=False)
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception:
        return None


def render_tablet_pdf_page_to_png(
    pdf_bytes: bytes,
    page: int = 1,
    target_long_edge: int = 2048,
    dpi: Optional[float] = None,
) -> Optional[bytes]:
    """Rasterize one page of the tablet's native PDF export to PNG bytes.

    This is the portable fallback used when the local stroke renderer cannot
    produce an image. The reMarkable's own firmware renders every notebook (and
    annotated PDF/EPUB) to a PDF served at ``/download/<uuid>/pdf``, so this path
    works regardless of the .rm block format, handles empty pages, and—crucially
    for portability—does not depend on a system graphics library (PyMuPDF
    bundles its own renderer). Credit: ljdutel (#95).

    Args:
        pdf_bytes: Raw bytes of the tablet-exported PDF.
        page: 1-based page number (matches notebook page ordering 1:1).
        target_long_edge: Target pixel size for the longest page edge.
        dpi: Output resolution; overrides ``target_long_edge`` when given.

    Returns:
        PNG image bytes, or None on failure / out-of-range page.
    """
    try:
        import fitz
    except ImportError:
        return None

    # The tablet's PDFs sometimes reference graphics-state resources MuPDF
    # considers malformed; it still rasterizes them correctly, so suppress the
    # noisy non-fatal error output.
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None

    try:
        index = page - 1
        if index < 0 or index >= len(doc):
            return None
        pdf_page = doc[index]
        longest_edge = max(pdf_page.rect.width, pdf_page.rect.height) or 1.0
        zoom = dpi / 72.0 if dpi else target_long_edge / longest_edge
        pix = pdf_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None
    finally:
        doc.close()


def render_mapped_pdf_page_from_document_zip(
    zip_path: Path, page: int = 1, target_long_edge: int = 2048
) -> Tuple[Optional[bytes], bool]:
    """Render a source-PDF underlay using the document's authoritative page map.

    Returns ``(png, has_source_pdf)``. A source PDF with ``png=None`` means the
    requested page has no mapped underlay (for example, a user-added page), so
    callers must not substitute the same ordinal from a native PDF export.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir_path)
        except Exception:
            return None, False

        pdf_files = list(tmpdir_path.glob("**/*.pdf"))
        if not pdf_files:
            return None, False

        pdf_page_index = _resolve_pdf_page_index(tmpdir_path, page)
        if pdf_page_index is None:
            return None, True

        content_stems = {path.stem for path in tmpdir_path.glob("*.content")}
        matching = [path for path in pdf_files if path.stem in content_stems]
        pdf_path = matching[0] if matching else sorted(pdf_files)[0]
        png = render_tablet_pdf_page_to_png(
            pdf_path.read_bytes(),
            page=pdf_page_index + 1,
            target_long_edge=target_long_edge,
        )
        return png, True


def render_merged_page_from_document_zip(
    zip_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    canvas_width: Optional[int] = None,
    canvas_height: Optional[int] = None,
    layers: Optional[str] = None,
    dpi: Optional[float] = None,
) -> tuple[Optional[bytes], Optional[str]]:
    """Render a page with the PDF underlay composited with the annotation layer.

    Extracts the zip, determines which PDF page corresponds to the requested
    reMarkable page, rasterizes the PDF page, renders the annotation layer,
    and alpha-composites them into a single image.

    Credit: Re-implementation inspired by PR #79 from @ColinSha.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color for annotation layer
        canvas_width: Output canvas width (default: derived from PDF page)
        canvas_height: Output canvas height (default: derived from PDF page)
        layers: Layer selection (see ``_v6_paths_from_blocks``)
        dpi: Output resolution when no canvas size is given (default 144)

    Returns:
        Tuple of (png_bytes, note) where note is an informational message
        or None. Returns (None, error_note) on failure.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir_path)
        except Exception:
            return None, "Could not extract document archive."

        return render_merged_page_from_extracted_document(
            tmpdir_path,
            page=page,
            background_color=background_color,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            layers=layers,
            dpi=dpi,
        )


def render_merged_page_from_extracted_document(
    tmpdir_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    canvas_width: Optional[int] = None,
    canvas_height: Optional[int] = None,
    layers: Optional[str] = None,
    dpi: Optional[float] = None,
) -> tuple[Optional[bytes], Optional[str]]:
    """Render a merged page from an already extracted document archive."""
    import io

    import fitz
    from PIL import Image as PILImage

    # Find the PDF file in the extracted directory. If multiple are present
    # (rare), prefer the one whose stem matches the .content document id so
    # selection is deterministic.
    pdf_files = list(tmpdir_path.glob("**/*.pdf"))
    if not pdf_files:
        full_page = render_page_full_page_from_extracted_document(
            tmpdir_path,
            page=page,
            background_color=background_color,
            layers=layers,
            dpi=dpi,
        )
        if full_page is None:
            return None, f"Page {page} has no renderable annotation layer."
        return full_page[0], "No PDF underlay found; returned full-page annotation render."

    content_stems = {p.stem for p in tmpdir_path.glob("*.content")}
    matching = [p for p in pdf_files if p.stem in content_stems]
    pdf_path = matching[0] if matching else sorted(pdf_files)[0]
    pdf_bytes = pdf_path.read_bytes()

    rm_files = _get_ordered_rm_files(tmpdir_path)
    total_pages = len(_get_page_order(tmpdir_path)) or len(rm_files)
    if total_pages == 0:
        try:
            with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf_document:
                total_pages = len(pdf_document)
        except Exception:
            total_pages = 0

    if page < 1 or page > total_pages:
        return None, f"Page {page} out of range (document has {total_pages} pages)."

    # Select the .rm for this page by id (see _select_rm_file_for_page). A
    # page with no strokes yields None and composites to the bare PDF page.
    target_rm_file: Optional[Path] = _select_rm_file_for_page(tmpdir_path, rm_files, page)

    def annotation_only(reason: str) -> Tuple[Optional[bytes], Optional[str]]:
        """Return a complete annotation page when the PDF side cannot be used."""
        full_page = render_page_full_page_from_extracted_document(
            tmpdir_path,
            page=page,
            background_color=background_color,
            layers=layers,
            dpi=dpi,
        )
        if full_page is None:
            return None, f"{reason}; page has no renderable annotation layer."
        return full_page[0], f"{reason}; returned full-page annotation render."

    # Determine which PDF page this reMarkable page maps to. Handles both the
    # formatVersion 2 (cPages.redir) and formatVersion 1 (redirectionPageMap)
    # layouts; the latter is used by many cloud-imported PDFs, which would
    # otherwise be misread as user-added pages and rendered annotation-only.
    pdf_page_index: Optional[int] = _resolve_pdf_page_index(tmpdir_path, page)

    if pdf_page_index is None:
        return annotation_only("Page has no PDF underlay (user-added page)")

    # Get PDF page dimensions to set annotation viewBox correctly
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            if pdf_page_index >= len(doc):
                return annotation_only("PDF page index out of range")

            pdf_page = doc[pdf_page_index]
            pdf_w_pt = pdf_page.rect.width
            pdf_h_pt = pdf_page.rect.height
        finally:
            doc.close()
    except Exception:
        return annotation_only("Could not read PDF dimensions")

    # Determine output canvas size
    pixels_per_point = dpi / 72.0 if dpi else 2.0
    out_w = canvas_width or int(round(pdf_w_pt * pixels_per_point))
    out_h = canvas_height or int(round(pdf_h_pt * pixels_per_point))

    # 1. Rasterize the PDF page
    pdf_png = _render_pdf_page_to_png(pdf_bytes, pdf_page_index, out_w, out_h)
    if pdf_png is None:
        return annotation_only("PDF rasterization failed")

    # 2. Render annotation layer to SVG, then to PNG with transparent background
    ann_svg_path = None
    ann_png_bytes = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            ann_svg_path = Path(tmp_svg.name)

        if target_rm_file is not None and _rm_to_svg(target_rm_file, ann_svg_path, layers):
            # rmscene coordinates use the device paper grid, not PDF points. Set
            # the root viewBox to the calibrated page rectangle before rasterizing.
            svg_content = ann_svg_path.read_text()

            blocks = _v6_blocks(target_rm_file)
            paper_size = _v6_paper_size(blocks) if blocks is not None else None
            points_per_unit = _points_per_unit(paper_size)
            viewbox = _annotation_page_viewbox(
                pdf_w_pt,
                pdf_h_pt,
                points_per_unit,
                centered_x=blocks is not None,
            )
            svg_content = _rewrite_svg_root(
                svg_content,
                viewbox=viewbox,
                width=out_w,
                height=out_h,
            )

            ann_png_bytes = _svg_string_to_png(
                svg_content,
                out_w,
                out_h,
                None,
                backdrop=PILImage.open(io.BytesIO(pdf_png)),
            )
            if ann_png_bytes is None:
                raise RuntimeError("PyMuPDF could not rasterize the annotation SVG")
    except Exception as exc:
        # Annotation rendering failed; return the PDF page but expose the loss.
        logger.debug("Annotation overlay rendering failed: %s", exc)
        ann_render_error = exc
    else:
        ann_render_error = None
    finally:
        if ann_svg_path:
            ann_svg_path.unlink(missing_ok=True)

    # 3. Composite: PDF base + annotation overlay
    try:
        pdf_img = PILImage.open(io.BytesIO(pdf_png)).convert("RGBA")

        if ann_png_bytes:
            # The annotation layer was drawn over the PDF page (its backdrop).
            composite = PILImage.open(io.BytesIO(ann_png_bytes)).convert("RGBA")
            if composite.size != pdf_img.size:
                composite = composite.resize(pdf_img.size, PILImage.LANCZOS)
            merged_note = None
        else:
            composite = pdf_img
            merged_note = (
                "Annotation overlay failed to render; returned PDF page without annotations."
                if ann_render_error is not None
                else None
            )

        composite = composite.convert("RGB")
        buf = io.BytesIO()
        composite.save(buf, format="PNG")
        return buf.getvalue(), merged_note
    except Exception:
        return annotation_only("Compositing failed")


def get_document_page_count(zip_path: Path) -> int:
    """
    Get the number of pages in a reMarkable document zip.

    Uses the .content metadata file for accurate page count (includes
    user-added pages in PDFs). Falls back to counting .rm files.

    Args:
        zip_path: Path to the document zip file

    Returns:
        Number of pages (0 if unable to determine)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        # Try .content metadata first — it's the authoritative page list
        data = _read_content_json(tmpdir_path)
        if has_cpages(data) or isinstance((data or {}).get("pages"), list):
            return len(content_page_ids(data))

        # Fallback to counting .rm files
        return len(list(tmpdir_path.glob("**/*.rm")))


def get_document_file_type(zip_path: Path) -> str:
    """
    Read the ``fileType`` from a document zip's ``.content`` file.

    Returns one of "notebook", "pdf", "epub", or "" when it cannot be
    determined. Reads only the ``.content`` entry from the zip (no full
    extraction) so it is cheap to call alongside get_document_page_count.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".content"):
                    try:
                        data = json.loads(zf.read(name).decode("utf-8"))
                    except Exception:
                        return ""
                    return str(data.get("fileType", "") or "")
    except Exception:
        pass
    return ""


# v6 pen ids (rmscene ``Pen``) for highlighters and erasers.
_HIGHLIGHTER_PENS = {5, 18}
_ERASER_PENS = {6, 8}


def _stroke_bbox(points) -> Optional[List[float]]:
    """``[x0, y0, x1, y1]`` of a stroke in stroke units, including the pen width.

    v6 point widths are four times the width in stroke units.
    """
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    if not xs:
        return None
    half_width = max((getattr(p, "width", 0) or 0) for p in points) / 4.0 / 2.0
    return [
        min(xs) - half_width,
        min(ys) - half_width,
        max(xs) + half_width,
        max(ys) + half_width,
    ]


MM_PER_POINT = 25.4 / 72.0


def _pdf_page_sizes(tmpdir_path: Path) -> List[Tuple[float, float]]:
    """``(width, height)`` in points for each page of a document's PDF, if any."""
    pdf_files = sorted(tmpdir_path.glob("**/*.pdf"))
    if not pdf_files:
        return []
    content_stems = {p.stem for p in tmpdir_path.glob("*.content")}
    matching = [p for p in pdf_files if p.stem in content_stems]
    try:
        import fitz

        with fitz.open(matching[0] if matching else pdf_files[0]) as pdf:
            return [(page.rect.width, page.rect.height) for page in pdf]
    except Exception:
        return []


def _page_frame(
    tmpdir_path: Path,
    page: int,
    rm_file: Optional[Path],
    pdf_page_sizes: List[Tuple[float, float]],
) -> Tuple[float, float, float]:
    """``(width, height, points_per_unit)`` of a page as rendered, in stroke units.

    PDF-backed pages span their PDF page; other pages span the scene's paper
    size, extended downward to hold ink written below it (as rendered).
    Stroke x coordinates are centered on the page, y starts at the top.
    """
    blocks = _v6_blocks(rm_file) if rm_file is not None else None
    paper = _v6_paper_size(blocks) if blocks else (REMARKABLE_WIDTH, REMARKABLE_HEIGHT)
    ppu = _points_per_unit(paper)
    if pdf_page_sizes:
        index = _resolve_pdf_page_index(tmpdir_path, page)
        if index is not None and 0 <= index < len(pdf_page_sizes):
            width_pt, height_pt = pdf_page_sizes[index]
            return width_pt / ppu, height_pt / ppu, ppu
    height = float(paper[1])
    if blocks:
        try:
            _, _, anchor_pos = _v6_text_elements_with_bounds(blocks)
            height = _v6_page_height(blocks, anchor_pos, height)
        except Exception:
            pass
    return float(paper[0]), height, ppu


def _locate_on_page(
    bbox: List[float], frame: Tuple[float, float, float]
) -> Tuple[List[float], List[float]]:
    """A stroke-unit bbox as page millimetres and page fractions.

    Both are ``[left, top, right, bottom]`` from the page's top-left corner.
    The fractions are clamped to the page, matching ``remarkable_image``'s
    ``region`` parameter.
    """
    width, height, ppu = frame
    left, top = bbox[0] + width / 2.0, bbox[1]
    right, bottom = bbox[2] + width / 2.0, bbox[3]
    mm = [round(v * ppu * MM_PER_POINT, 1) for v in (left, top, right, bottom)]

    def fraction(value: float, extent: float) -> float:
        return round(min(max(value / extent, 0.0), 1.0), 3)

    region = [
        fraction(left, width),
        fraction(top, height),
        fraction(right, width),
        fraction(bottom, height),
    ]
    return mm, region


# Gaps (page fractions) within which ink strokes join one region: letters of
# a word and words on a line join; separate lines of writing stay apart.
INK_REGION_GAP_X = 0.03
INK_REGION_GAP_Y = 0.004


def _merge_regions(boxes: List[List[float]]) -> List[List[float]]:
    """Merge boxes that touch or lie within the region gaps of each other."""
    merged = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i], merged[j]
                if (
                    a[0] - INK_REGION_GAP_X <= b[2]
                    and b[0] - INK_REGION_GAP_X <= a[2]
                    and a[1] - INK_REGION_GAP_Y <= b[3]
                    and b[1] - INK_REGION_GAP_Y <= a[3]
                ):
                    merged[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del merged[j]
                    changed = True
                    break
            if changed:
                break
    return sorted(merged, key=lambda r: (r[1], r[0]))


def page_ink_regions(tmpdir_path: Path, page: int) -> List[List[float]]:
    """Boxes around the ink on a page, as page fractions.

    Each box is ``[left, top, right, bottom]`` in the frame of
    ``remarkable_image``'s ``region``, around strokes on visible layers that
    lie close together (roughly a word or a line of writing). Strokes the MCP
    wrote (covers, erasable marks) are left out. Empty when the page has no
    ink.
    """
    from remarkable_mcp.cover_layers import known_author_uuid

    rm_file = _select_rm_file_for_page(tmpdir_path, _get_ordered_rm_files(tmpdir_path), page)
    if rm_file is None:
        return []
    scene = _v6_scene(rm_file)
    if scene is None:
        return []
    blocks, tree = scene
    offsets = _v6_anchor_offsets(rm_file)
    frame = _page_frame(tmpdir_path, page, rm_file, _pdf_page_sizes(tmpdir_path))
    author = known_author_uuid()
    own = next(
        (
            number
            for block in blocks
            for number, value in (getattr(block, "author_uuids", None) or {}).items()
            if author is not None and value == author
        ),
        None,
    )
    boxes = []
    for layer in _scene_layers(tree, blocks):
        if not layer["visible"]:
            continue
        # The MCP's own strokes (covers, erasable marks) are marks, not the
        # user's ink, on whatever layer they are.
        ours = (
            {id(v) for k, v in layer["group"].children.items() if getattr(k, "part1", None) == own}
            if own is not None
            else set()
        )
        for item, node_id in _scene_items(layer["group"]):
            if id(item) in ours:
                continue
            if type(item).__name__ != "Line":
                continue
            tool = getattr(item, "tool", None)
            if getattr(tool, "value", tool) in _ERASER_PENS:
                continue
            bbox = _stroke_bbox(getattr(item, "points", None) or [])
            if bbox is None:
                continue
            dx, dy = offsets.get(node_id, (0.0, 0.0))
            bbox = [bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy]
            boxes.append(_locate_on_page(bbox, frame)[1])
    return _merge_regions(boxes)


def _place_highlight(highlight: Dict[str, Any], frame: Tuple[float, float, float]) -> None:
    """Replace a highlight's stroke-unit ``rects`` with page positions.

    Adds ``rects_mm`` (one box per line or piece), and ``bbox_mm`` and
    ``region`` for the box around all of them.
    """
    rects = highlight.pop("rects", None)
    if not rects:
        return
    highlight["rects_mm"] = [_locate_on_page(r, frame)[0] for r in rects]
    union = [
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    ]
    highlight["bbox_mm"], highlight["region"] = _locate_on_page(union, frame)


def _v6_anchor_offsets(rm_file: Path) -> dict:
    """Group node id -> ``(dx, dy)`` text-anchor translation for a page.

    The same translation the renderer applies (see :func:`_v6_group_offsets`),
    so positions reported for marks match where they are drawn. Ink on
    notebook pages with typed text is stored relative to the text it is
    anchored to.
    """
    blocks = _v6_blocks(rm_file)
    if not blocks:
        return {}
    try:
        _, _, anchor_pos = _v6_text_elements_with_bounds(blocks)
        return _v6_group_offsets(blocks, anchor_pos) if anchor_pos else {}
    except Exception:
        return {}


def _page_annotation_details(rm_file: Path) -> Dict[str, Any]:
    """Extract a page's highlights, highlighter strokes and pen-stroke presence.

    reMarkable text highlights are stored as ``GlyphRange`` scene items inside the
    page's v6 ``.rm`` file (each carries the selected ``text``, its bounding
    ``rectangles`` and its color); freehand ink is stored as ``Line`` items. A
    highlighter stroke that did not snap to text is a ``Line`` drawn with a
    highlighter pen. The legacy ``.highlights`` JSON scanned elsewhere does not
    cover current firmware, so read them here.

    Returns ``{"highlights": [...], "highlighter_strokes": [...], "has_strokes":
    bool, "layers": [...]}``. Each highlight is ``{"text", "color", "color_id",
    "color_hex", "layer"}`` in reading order; each highlighter stroke is
    ``{"color", "color_id", "color_hex", "bbox", "layer"}`` ordered top to
    bottom, with ``bbox`` as ``[x0, y0, x1, y1]`` in stroke units, padded by
    the pen width. ``layer`` is the name of the layer the item is on (None
    outside any layer). ``has_strokes`` covers pen strokes other than
    highlighters and erasers. ``layers`` lists the page's layers in tablet
    order as ``{"name", "visible", "pen_strokes", "pen_colors", "highlights",
    "highlighter_strokes", "images"}`` counts. ``images`` lists pasted images
    (see :mod:`remarkable_mcp.images`) as ``{"file", "bbox", "layer"}``, top to
    bottom, with ``bbox`` in stroke units. Returns empty results if the file
    cannot be parsed.
    """
    details: Dict[str, Any] = {
        "highlights": [],
        "highlighter_strokes": [],
        "has_strokes": False,
        "layers": [],
        "images": [],
    }
    scene = _v6_scene(rm_file)
    if scene is None:
        return details
    blocks, tree = scene

    offsets = _v6_anchor_offsets(rm_file)
    highlights: list = []
    highlighter_strokes: list = []
    try:
        page_layers = _scene_layers(tree, blocks)
        layer_of_node: Dict[Any, Dict[str, Any]] = {}
        layer_stats = []
        for layer in page_layers:
            stats = {
                "name": layer["name"],
                "visible": layer["visible"],
                "pen_strokes": 0,
                "pen_colors": [],
                "highlights": 0,
                "highlighter_strokes": 0,
                "images": 0,
            }
            layer_stats.append(stats)
            layer_of_node[layer["group"].node_id] = stats
            for _, node_id in _scene_items(layer["group"]):
                layer_of_node[node_id] = stats
        details["layers"] = layer_stats

        for item, node_id in _page_scene_items(tree, page_layers):
            kind = type(item).__name__
            stats = layer_of_node.get(node_id)
            layer_name = stats["name"] if stats else None
            if kind == "Line":
                tool = getattr(item, "tool", None)
                tool = getattr(tool, "value", tool)
                if tool in _ERASER_PENS:
                    continue
                color = color_info(getattr(item, "color", None), getattr(item, "color_rgba", None))
                if tool not in _HIGHLIGHTER_PENS:
                    details["has_strokes"] = True
                    if stats:
                        stats["pen_strokes"] += 1
                        if color["color"] not in stats["pen_colors"]:
                            stats["pen_colors"].append(color["color"])
                    continue
                bbox = _stroke_bbox(getattr(item, "points", None) or [])
                if bbox is None:
                    continue
                dx, dy = offsets.get(node_id, (0.0, 0.0))
                bbox = [bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy]
                entry = dict(color, bbox=bbox, layer=layer_name)
                highlighter_strokes.append(entry)
                if stats:
                    stats["highlighter_strokes"] += 1
            elif kind == "GlyphRange":
                text = getattr(item, "text", None)
                if not text:
                    continue
                rects = getattr(item, "rectangles", None) or []
                y = rects[0].y if rects else 0.0
                x = rects[0].x if rects else 0.0
                entry = {"text": text}
                entry.update(
                    color_info(getattr(item, "color", None), getattr(item, "color_rgba", None))
                )
                entry["layer"] = layer_name
                dx, dy = offsets.get(node_id, (0.0, 0.0))
                entry["rects"] = [
                    [
                        r.x + dx,
                        r.y + dy,
                        r.x + dx + getattr(r, "w", 0.0),
                        r.y + dy + getattr(r, "h", 0.0),
                    ]
                    for r in rects
                ]
                highlights.append((y, x, entry))
                if stats:
                    stats["highlights"] += 1
    except Exception:
        pass

    highlights.sort(key=lambda t: (t[0], t[1]))  # top-to-bottom, left-to-right
    details["highlights"] = [t[2] for t in highlights]
    highlighter_strokes.sort(key=lambda h: (h["bbox"][1], h["bbox"][0]))
    details["highlighter_strokes"] = highlighter_strokes

    from remarkable_mcp.images import pasted_images

    images = []
    for image in pasted_images(blocks).values():
        bbox = image.bbox()
        if bbox is None:
            continue
        dx, dy = offsets.get(image.parent_id, (0.0, 0.0))
        bbox = [bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy]
        stats = layer_of_node.get(image.parent_id) if details["layers"] else None
        if stats:
            stats["images"] += 1
        images.append({"file": image.file_name, "bbox": bbox, "layer": stats and stats["name"]})
    images.sort(key=lambda i: (i["bbox"][1], i["bbox"][0]))
    details["images"] = images
    return details


def _extract_page_annotations(rm_file: Path) -> Tuple[List[str], bool]:
    """Return ``(highlighted_texts_in_reading_order, has_pen_strokes)`` for a page."""
    details = _page_annotation_details(rm_file)
    return [h["text"] for h in details["highlights"]], details["has_strokes"]


def extract_text_from_document_zip(
    zip_path: Path,
    include_ocr: bool = False,
    doc_id: Optional[str] = None,
    version: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extract all text content from a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file
        include_ocr: Whether to run OCR on handwritten content
        doc_id: Optional document ID for caching OCR results
        version: The document's current version (e.g. its cloud index hash);
            a result cached for another version is not reused

    Returns:
        {
            "typed_text": [...],      # From rmscene parsing (list of strings)
            "highlights": [...],       # Highlighted text (GlyphRange + legacy JSON)
            "handwritten_text": [...], # From OCR (if enabled): one text per page
                                       # with text, headed "--- Page N ---"
            "handwriting": [           # The same OCR as lines, in reading order
                {"page": int, "lines": [{"text": str, "layers": [str]}]},
            ],                         # "layers" only on pages with several
            "pages": int,
            "page_ids": [...],         # Page UUIDs in order
            "highlight_details": [     # GlyphRange highlights with colors
                {"page": int, "text": str, "color": str,
                 "color_id": int, "color_hex": str | None},
            ],
            "highlighter_strokes": [   # Highlighter strokes not snapped to text
                {"page": int, "color": str, "color_id": int,
                 "color_hex": str | None,
                 "bbox_mm": [left, top, right, bottom],  # from page top-left
                 "region": [left, top, right, bottom]},  # page fractions
            ],
            "annotated_pages": [       # Only pages that carry annotations
                {"page": int,          # 1-based page number
                 "page_id": str,
                 "has_handwriting": bool,   # page has pen strokes
                 "highlights": [str],  # highlighted text on the page
                 "highlight_details": [dict],  # text plus color, as above
                 "highlighter_strokes": [dict],  # as above, without "page"
                 "layers": [dict],     # name, visibility and per-layer counts
                 "images": [dict],     # pasted images, as below without "page"
                 "page_tags": [str]},  # tags on this page
            ],
            "images": [                # Images pasted onto pages (captures)
                {"page": int, "file": str, "layer": str | None,
                 "bbox_mm": [left, top, right, bottom], "region": [...]},
            ],
            "page_tags": [{"page": int, "name": str}],  # .content pageTags
            "ocr_backend": str,        # Which OCR backend was used (if any)
            "page_templates": [        # Non-blank notebook page templates
                {"page": int, "template": str},
            ],
        }
    """
    cache_token = None
    if doc_id:
        with _cache_lock:
            cache_token = (
                _global_cache_generation,
                _cache_generation.get(doc_id, 0),
            )
            cached = _extraction_cache.get(doc_id)
            if (
                cached
                and (cached["include_ocr"] or not include_ocr)
                and _is_cache_valid(cached, version)
            ):
                return cached["result"]

    result: Dict[str, Any] = {
        "typed_text": [],
        "highlights": [],
        "highlight_details": [],
        "highlighter_strokes": [],
        "images": [],
        "handwritten_text": None,
        "pages": 0,
        "page_ids": [],
        "annotated_pages": [],
        "ocr_backend": None,
        "tags": [],
        "page_tags": [],
        "page_templates": [],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        # Get page order from .content file (pages deleted on the tablet skipped)
        content_data = _read_content_json(tmpdir_path)
        page_order = content_page_ids(content_data)
        deleted = deleted_page_ids(content_data)
        templates = content_page_templates(content_data)
        result["page_templates"] = [
            {"page": i + 1, "template": templates[page_id]}
            for i, page_id in enumerate(page_order)
            if templates.get(page_id, BLANK_TEMPLATE) != BLANK_TEMPLATE
        ]

        rm_files = [f for f in tmpdir_path.glob("**/*.rm") if f.stem not in deleted]

        # If we have page order, sort rm_files accordingly
        if page_order:
            # Create mapping of page_id -> rm_file
            rm_by_id = {}
            for rm_file in rm_files:
                page_id = rm_file.stem  # filename without extension
                rm_by_id[page_id] = rm_file

            # Sort rm_files by page order
            ordered_rm_files = []
            for page_id in page_order:
                if page_id in rm_by_id:
                    ordered_rm_files.append(rm_by_id[page_id])
            # Add any remaining files not in page order
            for rm_file in rm_files:
                if rm_file not in ordered_rm_files:
                    ordered_rm_files.append(rm_file)
            rm_files = ordered_rm_files
            result["page_ids"] = page_order
        else:
            result["page_ids"] = [f.stem for f in rm_files]

        result["pages"] = len(page_order) or len(rm_files)

        # Extract typed text from .rm files using rmscene
        for rm_file in rm_files:
            text_lines = extract_text_from_rm_file(rm_file)
            result["typed_text"].extend(text_lines)

        # Extract per-page text highlights (GlyphRange items) and note-presence
        # from the .rm files, indexed by real page number. This powers "show only
        # the annotated pages / the highlighted text" without paging through the
        # whole document. (The legacy .highlights JSON scanned below only covers
        # older firmware exports.)
        page_pos = {pid: i for i, pid in enumerate(page_order)}
        for order_pos, rm_file in enumerate(rm_files):
            page_num = page_pos.get(rm_file.stem, order_pos) + 1
            details = _page_annotation_details(rm_file)
            page_details = details["highlights"]
            page_strokes = details["highlighter_strokes"]
            has_strokes = details["has_strokes"]
            if page_details or page_strokes or has_strokes or details["images"]:
                page_highlights = [h["text"] for h in page_details]
                result["annotated_pages"].append(
                    {
                        "page": page_num,
                        "page_id": rm_file.stem,
                        "has_handwriting": has_strokes,
                        "highlights": page_highlights,
                        "highlight_details": page_details,
                        "highlighter_strokes": page_strokes,
                        "layers": details["layers"],
                        "images": details["images"],
                    }
                )
                result["highlights"].extend(page_highlights)

        # Page tags, attached to their pages in the annotation index
        page_ids = page_order or [f.stem for f in rm_files]
        result["page_tags"] = page_tag_entries(content_data, page_ids)
        by_page = {ap["page"]: ap for ap in result["annotated_pages"]}
        for entry in result["page_tags"]:
            ap = by_page.get(entry["page"])
            if ap is None:
                ap = {
                    "page": entry["page"],
                    "page_id": page_ids[entry["page"] - 1],
                    "has_handwriting": False,
                    "highlights": [],
                    "highlight_details": [],
                    "highlighter_strokes": [],
                    "images": [],
                }
                by_page[entry["page"]] = ap
                result["annotated_pages"].append(ap)
            ap.setdefault("page_tags", []).append(entry["name"])
        result["annotated_pages"].sort(key=lambda a: a["page"])

        # Place highlights, highlighter strokes and pasted images on the page
        # as rendered
        def placed(ap: dict) -> list:
            return ap["highlighter_strokes"] + ap.get("images", [])

        if any(placed(ap) or ap["highlight_details"] for ap in result["annotated_pages"]):
            pdf_sizes = _pdf_page_sizes(tmpdir_path)
            rm_by_stem = {f.stem: f for f in rm_files}
            for ap in result["annotated_pages"]:
                if not (placed(ap) or ap["highlight_details"]):
                    continue
                frame = _page_frame(
                    tmpdir_path, ap["page"], rm_by_stem.get(ap["page_id"]), pdf_sizes
                )
                for entry in placed(ap):
                    entry["bbox_mm"], entry["region"] = _locate_on_page(entry.pop("bbox"), frame)
                for highlight in ap["highlight_details"]:
                    _place_highlight(highlight, frame)
        result["highlight_details"] = [
            {"page": ap["page"], **h}
            for ap in result["annotated_pages"]
            for h in ap["highlight_details"]
        ]
        result["highlighter_strokes"] = [
            {"page": ap["page"], **h}
            for ap in result["annotated_pages"]
            for h in ap["highlighter_strokes"]
        ]
        result["images"] = [
            {"page": ap["page"], **image}
            for ap in result["annotated_pages"]
            for image in ap.get("images", [])
        ]

        # Extract text from .txt and .md files
        for txt_file in tmpdir_path.glob("**/*.txt"):
            try:
                content = txt_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        for md_file in tmpdir_path.glob("**/*.md"):
            try:
                content = md_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        # Extract from .content files (metadata with text and tags)
        for content_file in tmpdir_path.glob("**/*.content"):
            try:
                data = json.loads(content_file.read_text())
                if "text" in data:
                    result["typed_text"].append(data["text"])
                if "tags" in data and data["tags"]:
                    result["tags"] = data["tags"]
            except Exception:
                # Malformed JSON or read error - skip this file
                pass

        # Extract PDF highlights
        for json_file in tmpdir_path.glob("**/*.json"):
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict) and "highlights" in data:
                    for h in data.get("highlights", []):
                        if "text" in h and h["text"]:
                            result["highlights"].append(h["text"])
            except Exception:
                # Malformed JSON - skip this file
                pass

        # OCR for handwritten content (optional)
        if include_ocr and rm_files:
            ocr_pages, ocr_backend = extract_handwriting_ocr(rm_files)
            handwriting = handwriting_by_page(tmpdir_path, rm_files, ocr_pages or [])
            result["handwriting"] = handwriting
            result["handwritten_text"] = [ocr_page_text(e) for e in handwriting] or None
            result["ocr_backend"] = ocr_backend

    if doc_id and cache_token is not None:
        _cache_extraction_result_if_current(doc_id, result, include_ocr, cache_token, version)

    return result


# OCR renders: Google Vision gets the standard screen size, Tesseract a larger
# image for small handwriting.
GOOGLE_OCR_SIZE = (REMARKABLE_WIDTH, REMARKABLE_HEIGHT)
TESSERACT_OCR_SIZE = (2106, 2808)
# A layer is named on an OCR line when it drew at least this share of the
# line's ink, relative to the layer that drew the most there.
LINE_LAYER_SHARE = 0.2

# An OCR line: (text, pixel box), plus Tesseract's confidence (0-100).
OcrLine = tuple
# Tesseract lines below this confidence are marked low_confidence.
LOW_CONFIDENCE = 80


def extract_handwriting_ocr(
    rm_files: List[Path],
) -> Tuple[Optional[List[Optional[List[Dict[str, Any]]]]], Optional[str]]:
    """
    Extract handwritten text using OCR.

    Supports multiple backends (set REMARKABLE_OCR_BACKEND env var):
    - "google": Google Cloud Vision - best for handwriting
    - "tesseract": pytesseract - basic OCR, rasterized locally with PyMuPDF
    - "auto" (default): Google if API key provided, else Tesseract

    Returns ``(pages, backend_used)``. ``pages`` has one entry per file in
    ``rm_files`` (None when that page could not be read), each a list of lines
    ``{"text", "layers"}`` in reading order; ``layers`` (the layers that drew
    the line) is present only on pages with several visible layers. ``pages``
    is None when OCR found nothing or is unavailable.
    """
    backend = get_ocr_backend()

    # Auto-detect best available backend
    if backend == "auto":
        # Check for Google Vision API key first (simplest auth method)
        if os.environ.get("GOOGLE_VISION_API_KEY"):
            backend = "google"
        else:
            backend = "tesseract"

    if backend == "google":
        result = _ocr_google_vision(rm_files)
        if result:
            return (result, "google")
        result = _ocr_tesseract(rm_files)
        return (result, "tesseract")
    result = _ocr_tesseract(rm_files)
    return (result, "tesseract")


def _ocr_image_size(svg: str, size: Tuple[int, int]) -> Tuple[int, int]:
    """The largest size within ``size`` with the SVG's proportions, so writing
    is not stretched for OCR."""
    import re

    match = re.search(r'viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ([\d.]+)"', svg)
    if not match:
        return size
    width, height = float(match.group(1)), float(match.group(2))
    if width <= 0 or height <= 0:
        return size
    scale = min(size[0] / width, size[1] / height)
    return max(1, int(width * scale)), max(1, int(height * scale))


def _ocr_page_images(
    rm_file: Path, size: Tuple[int, int]
) -> Optional[Tuple[bytes, List[Tuple[str, bytes]]]]:
    """``(page_png, [(layer_name, layer_png)])`` for OCR.

    The page shows its visible layers on white, cropped to the writing and
    scaled to fit ``size`` without stretching. When it has several visible
    layers, each is also rendered alone, transparent, in the same frame, so an
    OCR line's box can be matched to the layers that drew it.
    """
    blocks = _v6_blocks(rm_file)
    if blocks is None:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp:
            tmp_svg = Path(tmp.name)
        try:
            if not _rm_to_svg(rm_file, tmp_svg):
                return None
            svg = tmp_svg.read_text()
            png = _svg_string_to_png(svg, *_ocr_image_size(svg, size), "#FFFFFF")
        finally:
            tmp_svg.unlink(missing_ok=True)
        return (png, []) if png is not None else None

    text_elements, text_coords, anchor_pos = _v6_text_elements_with_bounds(blocks)
    paths, coords = _v6_paths_from_blocks(blocks, anchor_pos)
    frame = coords + text_coords
    svg = _svg_from_paths(text_elements + paths, frame)
    if svg is None:
        return None
    size = _ocr_image_size(svg, size)
    png = _svg_string_to_png(svg, size[0], size[1], "#FFFFFF")
    if png is None:
        return None
    layer_pngs: List[Tuple[str, bytes]] = []
    tree = _v6_tree(blocks)
    visible = [la for la in _scene_layers(tree, blocks) if la["visible"]] if tree else []
    if len(visible) > 1:
        for layer in visible:
            layer_paths, layer_coords = _v6_paths_from_blocks(blocks, anchor_pos, layer["name"])
            if not layer_coords:
                continue
            layer_svg = _svg_from_paths(layer_paths, frame)
            layer_png = _svg_string_to_png(layer_svg, size[0], size[1], None) if layer_svg else None
            if layer_png is not None:
                layer_pngs.append((layer["name"], layer_png))
    return png, layer_pngs


def _tag_line_layers(
    lines: List[OcrLine], layer_pngs: List[Tuple[str, bytes]]
) -> List[Dict[str, Any]]:
    """OCR lines ``(text, pixel box)`` as ``{"text", "layers"}``.

    With several layers, a line names each layer whose ink inside the line's
    box is at least ``LINE_LAYER_SHARE`` of the most any layer drew there;
    with fewer, lines carry no layers.
    """

    def entry(line: OcrLine) -> Dict[str, Any]:
        out: Dict[str, Any] = {"text": line[0]}
        if len(line) > 2:
            out["confidence"] = line[2]
            if line[2] < LOW_CONFIDENCE:
                out["low_confidence"] = True
        return out

    if len(layer_pngs) < 2:
        return [entry(line) for line in lines]
    import io

    from PIL import Image

    masks = []
    for name, png in layer_pngs:
        alpha = Image.open(io.BytesIO(png)).convert("RGBA").getchannel("A")
        masks.append((name, alpha.point(lambda v: 255 if v > 32 else 0)))
    out = []
    for line in lines:
        x0, y0, x1, y1 = line[1]
        box = (int(x0), int(y0), max(int(x0) + 1, int(x1)), max(int(y0) + 1, int(y1)))
        counts = [(name, mask.crop(box).histogram()[255]) for name, mask in masks]
        most = max(count for _, count in counts)
        layers = [name for name, count in counts if most and count >= most * LINE_LAYER_SHARE]
        out.append(dict(entry(line), layers=layers))
    return out


def _ocr_pages(
    rm_files: List[Path], size: Tuple[int, int], read_lines: Callable[[bytes], Any]
) -> List[Optional[List[Dict[str, Any]]]]:
    """OCR each file with ``read_lines(png) -> [(text, box)] | None``; a page
    that fails to render or read is None."""
    pages: List[Optional[List[Dict[str, Any]]]] = []
    for rm_file in rm_files:
        try:
            images = _ocr_page_images(rm_file, size)
            lines = read_lines(images[0]) if images else None
            pages.append(None if lines is None else _tag_line_layers(lines, images[1]))
        except _VisionAuthError:
            raise
        except Exception:
            pages.append(None)
    return pages


def _vision_lines(annotation: Dict[str, Any]) -> List[OcrLine]:
    """Lines ``(text, pixel box)`` from a Vision ``fullTextAnnotation``
    (REST camelCase or SDK snake_case keys), in reading order."""

    def get(obj: Any, *keys: str) -> Any:
        for key in keys:
            if isinstance(obj, dict) and key in obj:
                return obj[key]
        return None

    spaces = {"SPACE", "SURE_SPACE", 1, 2}
    hyphens = {"HYPHEN", 4}
    line_ends = {"EOL_SURE_SPACE", "LINE_BREAK", 3, 5} | hyphens
    lines: List[OcrLine] = []

    def finish(text: str, xs: list, ys: list) -> None:
        if text.strip() and xs:
            lines.append((text.strip(), (min(xs), min(ys), max(xs), max(ys))))

    for page in get(annotation, "pages") or []:
        for block in get(page, "blocks") or []:
            for paragraph in get(block, "paragraphs") or []:
                text, xs, ys = "", [], []
                for word in get(paragraph, "words") or []:
                    for vertex in get(get(word, "boundingBox", "bounding_box"), "vertices") or []:
                        xs.append(get(vertex, "x") or 0)
                        ys.append(get(vertex, "y") or 0)
                    for symbol in get(word, "symbols") or []:
                        text += get(symbol, "text") or ""
                        detected = get(get(symbol, "property"), "detectedBreak", "detected_break")
                        kind = get(detected, "type", "type_")
                        if kind in spaces:
                            text += " "
                        elif kind in line_ends:
                            if kind in hyphens:
                                text += "-"
                            finish(text, xs, ys)
                            text, xs, ys = "", [], []
                finish(text, xs, ys)
    return lines


def _ocr_google_vision(rm_files: List[Path]) -> Optional[List[Optional[List[Dict[str, Any]]]]]:
    """
    OCR using Google Cloud Vision API.
    Best quality for handwriting recognition.

    Supports two authentication methods:
    1. GOOGLE_VISION_API_KEY env var (simplest - just an API key)
    2. GOOGLE_APPLICATION_CREDENTIALS or default credentials (service account)
    """
    import os

    api_key = os.environ.get("GOOGLE_VISION_API_KEY")

    if api_key:
        # Use REST API with API key (simpler, no SDK needed)
        return _ocr_google_vision_rest(rm_files, api_key)
    else:
        # Use SDK with service account credentials
        return _ocr_google_vision_sdk(rm_files)


class _VisionAuthError(Exception):
    """The Vision API key is invalid or the API is not enabled."""


def _ocr_google_vision_rest(
    rm_files: List[Path], api_key: str
) -> Optional[List[Optional[List[Dict[str, Any]]]]]:
    """
    OCR using Google Cloud Vision REST API with API key.
    """
    import base64

    import requests

    def read_lines(png: bytes) -> Optional[List[OcrLine]]:
        url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
        payload = {
            "requests": [
                {
                    "image": {"content": base64.b64encode(png).decode("utf-8")},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        }
        response = requests.post(url, json=payload, timeout=60)
        if response.status_code in (401, 403):
            raise _VisionAuthError()
        if response.status_code != 200:
            return None
        responses = response.json().get("responses") or [{}]
        return _vision_lines(responses[0].get("fullTextAnnotation") or {})

    try:
        pages = _ocr_pages(rm_files, GOOGLE_OCR_SIZE, read_lines)
    except _VisionAuthError:
        # API key invalid or API not enabled - fall back to Tesseract
        return _ocr_tesseract(rm_files)
    return pages if any(pages) else None


def _ocr_google_vision_sdk(rm_files: List[Path]) -> Optional[List[Optional[List[Dict[str, Any]]]]]:
    """
    OCR using Google Cloud Vision SDK with service account credentials.
    """
    try:
        from google.cloud import vision

        client = vision.ImageAnnotatorClient()

        def read_lines(png: bytes) -> Optional[List[OcrLine]]:
            # DOCUMENT_TEXT_DETECTION gives the best handwriting results.
            response = client.document_text_detection(image=vision.Image(content=png))
            if response.error.message:
                return None
            annotation = response.full_text_annotation
            return _vision_lines(type(annotation).to_dict(annotation))

        pages = _ocr_pages(rm_files, GOOGLE_OCR_SIZE, read_lines)
        return pages if any(pages) else None

    except ImportError:
        # google-cloud-vision not installed, fall back to tesseract
        return _ocr_tesseract(rm_files)
    except Exception:
        # API error, fall back to tesseract
        return _ocr_tesseract(rm_files)


# Where Homebrew and other installers put Tesseract; apps started outside a
# shell (such as the server under Claude Desktop) often lack these in PATH.
TESSERACT_LOCATIONS = (
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/usr/bin/tesseract",
)


def tesseract_module():
    """``pytesseract``, pointed at the Tesseract program when it is not on
    PATH but installed in a usual place. Raises ImportError when pytesseract
    is missing."""
    import shutil

    import pytesseract

    if shutil.which(pytesseract.pytesseract.tesseract_cmd) is None:
        found = next((path for path in TESSERACT_LOCATIONS if os.path.exists(path)), None)
        if found:
            pytesseract.pytesseract.tesseract_cmd = found
    return pytesseract


def _tesseract_lines(png: bytes) -> List[OcrLine]:
    """Lines ``(text, pixel box)`` found by Tesseract, in reading order."""
    import io

    from PIL import Image, ImageFilter, ImageOps

    pytesseract = tesseract_module()
    # Grayscale, more contrast and slight sharpening help with handwriting.
    img = Image.open(io.BytesIO(png)).convert("L")
    img = ImageOps.autocontrast(img, cutoff=2)
    img = img.filter(ImageFilter.SHARPEN)
    # PSM 11 = sparse text: find as much text as possible.
    data = pytesseract.image_to_data(
        img, config=r"--psm 11 --oem 3", output_type=pytesseract.Output.DICT
    )
    grouped: Dict[tuple, list] = {}
    for i, word in enumerate(data["text"]):
        word = str(word).strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        left, top = data["left"][i], data["top"][i]
        box = (left, top, left + data["width"][i], top + data["height"][i])
        confidence = max(0.0, float(data["conf"][i]))
        grouped.setdefault(key, []).append((word, box, confidence))
    return _rows([word for words in grouped.values() for word in words])


def _rows(words: List[OcrLine]) -> List[OcrLine]:
    """Words joined into lines in reading order: words whose heights overlap
    by half of the shorter share a row, rows run top to bottom and each row
    left to right. Tesseract's sparse-text mode finds words but not their
    order."""
    rows: List[List[OcrLine]] = []
    for word in sorted(words, key=lambda w: (w[1][1] + w[1][3]) / 2):
        _, top, _, bottom = word[1]
        for row in rows:
            row_top = min(w[1][1] for w in row)
            row_bottom = max(w[1][3] for w in row)
            overlap = min(bottom, row_bottom) - max(top, row_top)
            if overlap >= 0.5 * min(bottom - top, row_bottom - row_top):
                row.append(word)
                break
        else:
            rows.append([word])
    lines: List[OcrLine] = []
    for row in rows:
        row.sort(key=lambda w: w[1][0])
        boxes = [w[1] for w in row]
        box = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        line: tuple = (" ".join(w[0] for w in row), box)
        if all(len(w) > 2 for w in row):
            # Confidence weighted by word length.
            chars = sum(len(w[0]) for w in row)
            line += (round(sum(w[2] * len(w[0]) for w in row) / chars),)
        lines.append(line)
    return sorted(lines, key=lambda line: line[1][1])


def _ocr_tesseract(rm_files: List[Path]) -> Optional[List[Optional[List[Dict[str, Any]]]]]:
    """
    OCR using Tesseract.
    Basic quality - designed for printed text, not handwriting.

    Requires: pytesseract and PyMuPDF.
    """
    try:
        tesseract_module()
    except ImportError:
        # OCR dependencies not installed
        return None
    pages = _ocr_pages(rm_files, TESSERACT_OCR_SIZE, _tesseract_lines)
    return pages if any(pages) else None


def ocr_page_text(entry: Dict[str, Any]) -> str:
    """One OCR'd page as text: a page heading, then its lines in reading
    order; on pages with several layers each line ends with its layers."""
    out = [f"--- Page {entry['page']} ---"]
    for line in entry["lines"]:
        text = line["text"]
        if line.get("layers"):
            text += f"  [{', '.join(line['layers'])}]"
        if line.get("low_confidence"):
            text += " (low confidence)"
        out.append(text)
    return "\n".join(out)


def handwriting_by_page(
    tmpdir_path: Path, rm_files: List[Path], pages: List[Optional[List[Dict[str, Any]]]]
) -> List[Dict[str, Any]]:
    """``[{"page", "lines"}]`` for the OCR'd pages that have text, numbered by
    their place in the document."""
    order = _get_page_order(tmpdir_path)
    out = []
    for position, (rm_file, lines) in enumerate(zip(rm_files, pages), start=1):
        if not lines:
            continue
        page = order.index(rm_file.stem) + 1 if rm_file.stem in order else position
        out.append({"page": page, "lines": lines})
    return sorted(out, key=lambda entry: entry["page"])

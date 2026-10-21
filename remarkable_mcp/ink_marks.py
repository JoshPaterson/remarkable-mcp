"""Erasable marks: marks written as tablet ink instead of into the PDF.

A mark with ``"erasable": true`` is rendered as usual, then its drawing is
turned into pen strokes (fineliner for shapes, paths, leaders and text;
highlighter for highlights) written to the page's ink under the MCP's own
author id, as covers and text highlights are (see ``rm_ink``). The user
can erase, select, move and recolor it on the tablet, and its named color
matches on the tablet and in the desktop and browser apps.

Text is drawn with EMS Readability, a single-stroke font (see
``remarkable_mcp/fonts``); bold is a thicker line.
"""

from __future__ import annotations

import base64
import functools
import math
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from remarkable_mcp import rm_ink, svg_path

FONT_DIR = Path(__file__).parent / "fonts"
FONT_FILES = {False: "EMSReadability.svg", True: "EMSReadabilityItalic.svg"}
SVG_NS = "{http://www.w3.org/2000/svg}"
LINE_SPACING = 1.25
UNDERLINE_DROP = 0.12
# Text stroke width as a share of the font size; bold is thicker.
TEXT_WIDTH = 0.07
BOLD_WIDTH = 1.8
MIN_WIDTH = 0.4
BORDER_PAD = 3.0
BORDER_RADIUS = 3.5
CURVE_STEPS = 8
# Highlights taller than this are drawn as several highlighter rows.
MAX_HIGHLIGHT_ROW = 24.0

# Pen color ids for named colors (pens), and highlighter ids with the
# color_rgba the tablet stores.
PEN_COLORS = {
    "black": 0,
    "gray": 1,
    "white": 2,
    "blue": 6,
    "red": 7,
    "green": 10,
    "cyan": 11,
    "magenta": 12,
    "yellow": 13,
}
FINELINER = "fineliner"
HIGHLIGHTER = "highlighter"
ERASABLE_KINDS = ("text", "shape", "path")


class InkError(rm_ink.InkError):
    """An erasable mark that cannot be written as ink."""


# --- Single-stroke text ----------------------------------------------------


@functools.lru_cache(maxsize=2)
def _font(italic: bool) -> Tuple[float, float, Dict[str, Tuple[list, float]]]:
    """``(units_per_em, ascent, {char: (path commands, advance)})``."""
    root = ET.parse(FONT_DIR / FONT_FILES[italic]).getroot()
    font = root.find(f".//{SVG_NS}font")
    face = font.find(f"{SVG_NS}font-face")
    upm = float(face.get("units-per-em", 1000))
    ascent = float(face.get("ascent", upm * 0.8))
    default = float(font.get("horiz-adv-x", upm * 0.4))
    glyphs = {}
    for glyph in font.findall(f"{SVG_NS}glyph"):
        char = glyph.get("unicode")
        if char is not None and len(char) == 1:
            data = glyph.get("d") or ""
            glyphs[char] = (
                svg_path.parse(data) if data else [],
                float(glyph.get("horiz-adv-x", default)),
            )
    return upm, ascent, glyphs


def _advance(text: str, size: float, italic: bool) -> float:
    upm, _, glyphs = _font(italic)
    return sum(glyphs.get(c, glyphs.get("?", ([], upm * 0.5)))[1] for c in text) * size / upm


def wrap(text: str, size: float, italic: bool, width: Optional[float]) -> List[str]:
    """Lines of ``text`` (explicit line breaks kept), wrapped at ``width``
    points when given."""
    lines: List[str] = []
    for paragraph in text.split("\n"):
        if width is None:
            lines.append(paragraph)
            continue
        line = ""
        for word in paragraph.split(" "):
            trial = f"{line} {word}" if line else word
            if line and _advance(trial, size, italic) > width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


def text_path(
    lines: Sequence[str], size: float, italic: bool, underline: bool, border: bool
) -> Tuple[str, float, float]:
    """``(path data, width, height)`` in points for lines of single-stroke
    text, the first baseline at the font's ascent below the top."""
    upm, ascent, glyphs = _font(italic)
    scale = size / upm
    top = ascent * scale
    parts: List[str] = []
    width = 0.0
    for index, line in enumerate(lines):
        base = top + index * LINE_SPACING * size
        x = 0.0
        for char in line:
            commands, advance = glyphs.get(char, glyphs.get("?", ([], upm * 0.5)))
            if commands:
                parts.append(svg_path.transform(commands, scale, -scale, x, base))
            x += advance * scale
        if underline and line.strip():
            y = base + UNDERLINE_DROP * size
            parts.append(f"M 0 {y:.3f} L {x:.3f} {y:.3f}")
        width = max(width, x)
    height = top + (len(lines) - 1) * LINE_SPACING * size + 0.25 * size
    if border:
        p, r = BORDER_PAD, BORDER_RADIUS
        x0, y0, x1, y1 = -p, -p, width + p, height + p
        parts.append(
            f"M {x0 + r:.3f} {y0:.3f} L {x1 - r:.3f} {y0:.3f} "
            f"Q {x1:.3f} {y0:.3f} {x1:.3f} {y0 + r:.3f} "
            f"L {x1:.3f} {y1 - r:.3f} Q {x1:.3f} {y1:.3f} {x1 - r:.3f} {y1:.3f} "
            f"L {x0 + r:.3f} {y1:.3f} Q {x0:.3f} {y1:.3f} {x0:.3f} {y1 - r:.3f} "
            f"L {x0:.3f} {y0 + r:.3f} Q {x0:.3f} {y0:.3f} {x0 + r:.3f} {y0:.3f} Z"
        )
        width, height = width + 2 * p, height + 2 * p
    return " ".join(parts), width, height


def text_as_path(options: Dict[str, Any], page_size: Tuple[float, float]) -> Dict[str, Any]:
    """An erasable text request as the path request that draws it."""
    from remarkable_mcp.pdf_marks import DEFAULT_TEXT_SIZE, MarkError, parse_positive, parse_region

    text = options["text"]
    if not isinstance(text, str) or not text.strip():
        raise MarkError("text must be non-empty.")
    size = parse_positive(options.get("size"), "size", DEFAULT_TEXT_SIZE)
    italic, bold = bool(options.get("italic")), bool(options.get("bold"))
    width = max(MIN_WIDTH, size * TEXT_WIDTH * (BOLD_WIDTH if bold else 1.0))
    request = {k: v for k, v in options.items() if k in ("at", "anchor", "near", "leader")}
    request.update(path="", width=width)
    if "region" in options:
        region = parse_region(options["region"])
        box_w = (region[2] - region[0]) * page_size[0]
        lines = wrap(text, size, italic, box_w - (2 * BORDER_PAD if options.get("border") else 0))
        data, w, h = text_path(
            lines, size, italic, bool(options.get("underline")), bool(options.get("border"))
        )
        if h > (region[3] - region[1]) * page_size[1] + 0.5:
            raise MarkError(
                "The text does not fit its region; make the region larger or size smaller."
            )
        request["region"] = [
            region[0],
            region[1],
            region[0] + w / page_size[0],
            region[1] + h / page_size[1],
        ]
    else:
        lines = wrap(text, size, italic, None)
        data, w, h = text_path(
            lines, size, italic, bool(options.get("underline")), bool(options.get("border"))
        )
        request["size"] = max(w, h)
    request["path"] = data
    return request


# --- Strokes from a rendered mark ------------------------------------------


def _bezier(p0, p1, p2, p3, steps: int = CURVE_STEPS) -> List[Tuple[float, float]]:
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t**3
        out.append(
            (
                a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
            )
        )
    return out


def part_strokes(part: Dict[str, Any], page_size: Tuple[float, float]) -> List[Dict[str, Any]]:
    """Pen strokes (``{"points", "width"}`` in points) drawing a rendered
    mark part (a mark or its leader): its snippet's lines and curves; filled
    shapes are outlined, and small ones (dots, arrow tips) drawn as a dot."""
    import pymupdf

    with pymupdf.open("pdf", base64.b64decode(part["pdf"])) as doc:
        page = doc[0]
        sw, sh = page.rect.width, page.rect.height
        drawings = page.get_drawings()
    x0, y0, x1, y1 = part["box"]
    ox, oy = x0 * page_size[0], y0 * page_size[1]
    sx = (x1 - x0) * page_size[0] / sw if sw else 1.0
    sy = (y1 - y0) * page_size[1] / sh if sh else 1.0

    def at(point) -> Tuple[float, float]:
        return (ox + point.x * sx, oy + point.y * sy)

    strokes: List[Dict[str, Any]] = []
    for drawing in drawings:
        lines: List[List[Tuple[float, float]]] = []
        for item in drawing["items"]:
            kind = item[0]
            if kind == "l":
                points = [at(item[1]), at(item[2])]
            elif kind == "c":
                p = [at(q) for q in item[1:5]]
                points = [p[0]] + _bezier(*p)
            elif kind == "re":
                r = item[1]
                points = [at(r.tl), at(r.tr), at(r.br), at(r.bl), at(r.tl)]
            elif kind == "qu":
                q = item[1]
                points = [at(q.ul), at(q.ur), at(q.lr), at(q.ll), at(q.ul)]
            else:
                continue
            if (
                lines
                and abs(lines[-1][-1][0] - points[0][0]) < 1e-3
                and abs(lines[-1][-1][1] - points[0][1]) < 1e-3
            ):
                lines[-1].extend(points[1:])
            else:
                lines.append(points)
        width = (drawing.get("width") or 0) * sx
        if drawing.get("color") is not None and width:
            strokes += [{"points": line, "width": width} for line in lines]
        elif drawing.get("fill") is not None:
            for line in lines:
                xs, ys = [p[0] for p in line], [p[1] for p in line]
                span = max(max(xs) - min(xs), max(ys) - min(ys))
                if span <= 4.0:
                    centre = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
                    strokes.append(_dot(centre, max(span, MIN_WIDTH)))
                else:
                    strokes.append({"points": line, "width": 1.0})
    return strokes


def _dot(centre: Tuple[float, float], diameter: float) -> Dict[str, Any]:
    """A filled dot: a small closed circle drawn with a line as wide as its
    radius (the tablet does not draw a stroke of zero length)."""
    radius = diameter / 4
    points = [
        (
            centre[0] + radius * math.cos(2 * math.pi * i / 8),
            centre[1] + radius * math.sin(2 * math.pi * i / 8),
        )
        for i in range(9)
    ]
    return {"points": points, "width": diameter / 2}


def highlight_strokes(box: Sequence[float], page_size: Tuple[float, float]) -> List[Dict[str, Any]]:
    """Highlighter strokes filling a region: rows no taller than
    ``MAX_HIGHLIGHT_ROW`` points."""
    x0, y0 = box[0] * page_size[0], box[1] * page_size[1]
    x1, y1 = box[2] * page_size[0], box[3] * page_size[1]
    rows = max(1, int(-(-(y1 - y0) // MAX_HIGHLIGHT_ROW)))
    step = (y1 - y0) / rows
    return [
        {"points": [(x0, y0 + step * (i + 0.5)), (x1, y0 + step * (i + 0.5))], "width": step}
        for i in range(rows)
    ]


# --- Writing ink -----------------------------------------------------------


def add_strokes(
    data: Optional[bytes],
    author: uuid.UUID,
    size: Tuple[float, float],
    strokes: Sequence[Dict[str, Any]],
    tool: str,
    color: str,
    layer: Optional[str] = None,
) -> Tuple[bytes, List[List[int]]]:
    """``data`` (None: the page has no ink) with ``strokes`` (points in page
    points) written with ``tool`` in ``color``; returns the file and ids."""
    page = rm_ink.PageWriter(data, author)
    ids = _write_strokes(page, size, strokes, tool, color, layer)
    return page.data, ids


def _write_strokes(
    page: rm_ink.PageWriter,
    size: Tuple[float, float],
    strokes: Sequence[Dict[str, Any]],
    tool: str,
    color: str,
    layer: Optional[str],
) -> List[List[int]]:
    from remarkable_mcp.text_highlights import COLORS

    if tool == HIGHLIGHTER:
        color_id, rgba = COLORS[color]
    else:
        color_id, rgba = PEN_COLORS[color], None
    lines = [
        {
            "points": [[x / size[0], y / size[1]] for x, y in stroke["points"]],
            "width": stroke["width"],
        }
        for stroke in strokes
    ]
    return page.add_lines(page.ink_layer(layer), lines, size, tool, color_id, rgba)


def ink_status(data: Optional[bytes], item_ids: Iterable[Sequence[int]]) -> str:
    """ "on tablet", "erased on tablet", "partly erased on tablet" (some of
    its strokes or highlight items erased) or "missing"."""
    states = rm_ink.item_states(data, item_ids)
    if not states:
        return "missing"
    live = sum(1 for _, alive in states.values() if alive)
    erased = len(states) - live
    if not live:
        return "erased on tablet"
    return "partly erased on tablet" if erased else "on tablet"


def add_mark_ink(page: rm_ink.PageWriter, mark: Dict[str, Any], size) -> None:
    """Write a pending erasable mark (on a page of ``size`` points), store its
    item ids in the mark and drop its PDF snippets."""
    ink = mark["ink"]
    if ink["tool"] == HIGHLIGHTER:
        strokes = highlight_strokes(mark["box"], size)
    else:
        strokes = part_strokes(mark, size)
        if mark.get("leader"):
            strokes += part_strokes(mark["leader"], size)
    ids = _write_strokes(page, size, strokes, ink["tool"], ink["color"], ink.get("layer"))
    mark["ink"] = {
        "tool": ink["tool"],
        "color": ink["color"],
        "layer": ink.get("layer"),
        "items": ids,
    }
    mark.pop("pdf", None)
    if mark.get("leader"):
        mark["leader"].pop("pdf", None)


def erasable_request(
    options: Dict[str, Any], color: Optional[str], page_size: Tuple[float, float]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(render options, pending ink)`` for a mark with ``"erasable": true``:
    the options without ``erasable`` and ``layer`` (text becomes the path that
    draws it), and how its ink is written."""
    from remarkable_mcp.pdf_marks import MarkError
    from remarkable_mcp.text_highlights import COLORS

    options = dict(options)
    options.pop("erasable", None)
    layer = options.pop("layer", None)
    if layer is not None and (not isinstance(layer, str) or not layer.strip()):
        raise MarkError("layer must be a layer name.")
    kinds = [
        k for k in ("text", "shape", "path", "latex", "graphic", "graphic_file") if k in options
    ]
    if len(kinds) != 1 or kinds[0] not in ERASABLE_KINDS:
        raise MarkError("Only text, shape and path marks can be erasable.")
    kind = kinds[0]
    if kind == "path" and options.get("fill"):
        raise MarkError("An erasable path cannot be filled; it is drawn with a pen.")
    if "opacity" in options:
        raise MarkError("opacity does not apply to erasable marks.")
    highlight = options.get("shape") == "highlight"
    palette = COLORS if highlight else PEN_COLORS
    name = options.get("color")
    if name is None:
        name = color if isinstance(color, str) and color.strip().lower() in palette else None
    name = (name or ("yellow" if highlight else "black")).strip().lower()
    if name not in palette:
        what = "highlighter" if highlight else "pen"
        raise MarkError(f"An erasable mark's color is a {what} color: {', '.join(palette)}.")
    options["color"] = name
    ink = {
        "pending": True,
        "tool": HIGHLIGHTER if highlight else FINELINER,
        "color": name,
        "layer": layer,
    }
    if kind == "text":
        rendered = text_as_path(options, page_size)
        rendered["color"] = name
        return rendered, ink
    return options, ink

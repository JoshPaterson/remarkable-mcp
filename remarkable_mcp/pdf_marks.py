"""Marks (grades, notes, shapes, equations, graphics) drawn into a document's PDF.

The tablet never changes a document's PDF; ink lives in separate ``.rm``
files tied to page ids. Replacing the PDF with a copy of the same page count
and page sizes therefore keeps the user's ink in place, and anything drawn
into that copy appears under the ink.

Each mark is rendered once, when it is added, into a one-page PDF snippet the
size of its box on the page. A marked PDF carries two embedded files: the
original PDF and the list of marks with their snippets. Every change rebuilds
the PDF by placing the remaining snippets on the original, so a removed mark
leaves nothing behind, removing every mark restores the original bytes
exactly, and a mark looks the same however often the PDF is rebuilt.

Mark coordinates are fractions of the page (0-1 from the top-left), the frame
``remarkable_image`` renders and its ``region`` uses. Marks are stored by PDF
page index, which stays stable when pages are added or moved on the tablet.
"""

import base64
import binascii
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pymupdf

from remarkable_mcp import svg_path

BASE_FILE = "remarkable-mcp-original.pdf"
MARKS_FILE = "remarkable-mcp-marks.json"
MARKS_FORMAT = 2

KINDS = ("text", "shape", "path", "latex", "graphic", "graphic_file")
# Built-in shapes drawn as paths. A shape that stretches fills its region on
# both axes; the others keep their aspect. A filled shape has no outline.
SHAPE_PATHS: Dict[str, Dict[str, Any]] = {
    "check": {"path": "M -1 0 L -0.3 0.7 L 1 -0.9", "width": 1.95},
    "cross": {"path": "M -1 -1 L 1 1 M -1 1 L 1 -1", "width": 1.95},
    "circle": {"path": "M 0 1 A 1 1 0 1 0 2 1 A 1 1 0 1 0 0 1", "stretch": True},
    "box": {"path": "M 0 0 H 1 V 1 H 0 Z", "stretch": True},
}
SHAPES = (*SHAPE_PATHS, "highlight", "line", "arrow")
ANCHORS = {
    "top-left": (0.0, 0.0),
    "top": (0.5, 0.0),
    "top-right": (1.0, 0.0),
    "left": (0.0, 0.5),
    "center": (0.5, 0.5),
    "right": (1.0, 0.5),
    "bottom-left": (0.0, 1.0),
    "bottom": (0.5, 1.0),
    "bottom-right": (1.0, 1.0),
}


def _rgb(hex_color: str) -> Tuple[float, float, float]:
    return tuple(int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))


# Pen colors as the Paper Pro shows them. Its screen draws the pure colors
# below without dithering, matching its own pen strokes; the softer values
# its PDF export uses for pens are dithered on the screen and look different.
COLORS = {
    name: _rgb(value)
    for name, value in {
        "black": "#000000",
        "gray": "#909090",
        "white": "#FFFFFF",
        "blue": "#0000FF",
        "red": "#FF0000",
        "green": "#00FF00",
        "yellow": "#FFFF00",
        "cyan": "#00FFFF",
        "magenta": "#FF00FF",
    }.items()
}
# Highlighter colors, drawn with the Darken blend: on white paper they show
# their color, and text under them stays dark. Blue, orange and gray are the
# tablet's PDF export values, which the tablet also shows as its own
# highlighters; the others were matched by eye on a Paper Pro, since the
# export values look different on its screen.
HIGHLIGHT_COLORS = {
    name: _rgb(value)
    for name, value in {
        "yellow": "#FFEA74",
        "blue": "#BEEAFE",
        "pink": "#EA9EFF",
        "orange": "#FFC38C",
        "green": "#ACFF80",
        "gray": "#BFBFBF",
    }.items()
}
DEFAULT_TEXT_SIZE = 11.0
DEFAULT_LATEX_SIZE = 12.0
DEFAULT_SYMBOL_SIZE = 12.0
DEFAULT_LINE_WIDTH = 1.5
MAX_PATH_CHARS = 20_000
MAX_DESCRIPTION_CHARS = 200
_PATH_PATTERN = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE+\-.,\s]+$")
LINE_SPACING = 1.2
MAX_SVG_CHARS = 1_000_000
MAX_GRAPHIC_BYTES = 2 * 1024 * 1024
MAX_GRAPHIC_FILE_BYTES = 10 * 1024 * 1024
MAX_SNIPPET_BYTES = 4 * 1024 * 1024
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_URL_PATTERN = re.compile(r"url\(\s*['\"]?([^'\")\s]*)", re.IGNORECASE)


class MarkError(ValueError):
    """A mark that cannot be drawn (bad coordinates, content or page)."""


# --- Reading and writing the marked state ---------------------------------


def load_state(pdf_bytes: bytes) -> Tuple[bytes, List[Dict[str, Any]]]:
    """``(original_pdf, marks)`` for a document's current PDF.

    A PDF without the embedded files is its own original, with no marks.
    """
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        names = doc.embfile_names()
        if BASE_FILE not in names or MARKS_FILE not in names:
            return pdf_bytes, []
        base = doc.embfile_get(BASE_FILE)
        data = json.loads(doc.embfile_get(MARKS_FILE).decode("utf-8"))
    marks = data.get("marks") if isinstance(data, dict) else None
    if not isinstance(marks, list):
        raise MarkError("The PDF's mark list is unreadable.")
    if data.get("format") != MARKS_FORMAT:
        raise MarkError(
            "The PDF's marks were made by an older version of this tool; "
            "clear them (clear=True) and add them again."
        )
    return base, marks


def original_pdf(pdf_bytes: bytes) -> bytes:
    """The PDF before any marks: ``pdf_bytes`` itself when it has none."""
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            if BASE_FILE in doc.embfile_names():
                return doc.embfile_get(BASE_FILE)
    except Exception:
        pass
    return pdf_bytes


def stored_marks(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """The marks recorded in a PDF; empty when it has none or is unreadable."""
    try:
        return load_state(pdf_bytes)[1]
    except Exception:
        return []


def text_lines(pdf_bytes: bytes, pdf_page: int) -> List[Dict[str, Any]]:
    """The text lines of one PDF page with their boxes as page fractions.

    Each line is ``{"text", "region": [left, top, right, bottom]}``, top to
    bottom; whitespace-only lines are skipped.
    """
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not 0 <= pdf_page < len(doc):
            return []
        page = doc[pdf_page]
        rect = page.rect
        lines = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(span["text"] for span in line["spans"]).strip()
                if not text:
                    continue
                x0, y0, x1, y1 = line["bbox"]
                box = [
                    (x0 - rect.x0) / rect.width,
                    (y0 - rect.y0) / rect.height,
                    (x1 - rect.x0) / rect.width,
                    (y1 - rect.y0) / rect.height,
                ]
                region = [_round(min(max(v, 0.0), 1.0)) for v in box]
                lines.append({"text": text, "region": region})
    return sorted(lines, key=lambda line: (line["region"][1], line["region"][0]))


def page_sizes(pdf_bytes: bytes) -> List[Tuple[float, float]]:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        return [(page.rect.width, page.rect.height) for page in doc]


def build(base: bytes, marks: Sequence[Dict[str, Any]]) -> bytes:
    """The PDF with ``marks`` placed on ``base``; ``base`` itself when none.

    Raises MarkError if placing would change the page count or a page size,
    which would move the tablet's ink relative to the page.
    """
    if not marks:
        return base
    doc = pymupdf.open(stream=base, filetype="pdf")
    try:
        before = [(p.rect.width, p.rect.height) for p in doc]
        for mark in marks:
            index = mark["pdf_page"]
            if not 0 <= index < len(doc):
                raise MarkError(f"Mark '{mark['id']}' is on a PDF page that does not exist.")
            page = doc[index]
            if page.rotation:
                raise MarkError("Rotated PDF pages are not supported.")
            with pymupdf.open("pdf", base64.b64decode(mark["pdf"])) as snippet:
                page.show_pdf_page(_points(page.rect, mark["box"]), snippet, 0)
        doc.embfile_add(BASE_FILE, base, filename=BASE_FILE, desc="PDF before marks")
        payload = json.dumps({"format": MARKS_FORMAT, "marks": list(marks)}, indent=1)
        doc.embfile_add(MARKS_FILE, payload.encode("utf-8"), filename=MARKS_FILE, desc="Marks")
        result = doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()
    if page_sizes(result) != before:
        raise MarkError("Refusing to write: the page count or a page size changed.")
    return result


# --- Validating requests ----------------------------------------------------


def _round(value: float) -> float:
    return round(value, 4)


def _fraction(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarkError(f"{what} must be a number from 0 to 1.")
    if not 0.0 <= float(value) <= 1.0:
        raise MarkError(f"{what} must be from 0 to 1 (a fraction of the page).")
    return float(value)


def _point(value: Any, what: str) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise MarkError(f"{what} must be [x, y] page fractions.")
    return [_fraction(value[0], f"{what} x"), _fraction(value[1], f"{what} y")]


def _region(value: Any) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise MarkError("region must be [left, top, right, bottom] page fractions.")
    x0, y0, x1, y1 = (_fraction(v, "region") for v in value)
    if x0 >= x1 or y0 >= y1:
        raise MarkError("region must have left < right and top < bottom.")
    return [x0, y0, x1, y1]


def _positive(value: Any, what: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise MarkError(f"{what} must be a positive number.")
    return float(value)


def parse_color(value: Any, palette: Dict[str, Tuple[float, ...]] = COLORS) -> List[float]:
    """An RGB triple (0-1) from a name in ``palette`` or ``#RRGGBB``."""
    if isinstance(value, str):
        name = value.strip().lower()
        if name in palette:
            return list(palette[name])
        if re.fullmatch(r"#[0-9a-f]{6}", name):
            return [int(name[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    if palette is HIGHLIGHT_COLORS:
        names = f"highlighter colors are {', '.join(HIGHLIGHT_COLORS)}"
    else:
        names = f"pen colors are {', '.join(COLORS)}"
    raise MarkError(f"Unknown color {value!r}; {names}, or use #RRGGBB.")


def check_color(value: Any) -> str:
    """``value`` if it names a pen or highlighter color, or is ``#RRGGBB``."""
    try:
        parse_color(value)
    except MarkError:
        parse_color(value, HIGHLIGHT_COLORS)
    return value


def color_name(rgb: Sequence[float], palette: Dict[str, Tuple[float, ...]] = COLORS) -> str:
    """The name of an RGB triple in ``palette``, or its ``#RRGGBB`` form."""
    for name, value in palette.items():
        if all(abs(a - b) < 1e-6 for a, b in zip(rgb, value)):
            return name
    return _hex(rgb)


def _hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{round(v * 255):02x}" for v in rgb)


def _palette(shape: Optional[str]) -> Dict[str, Tuple[float, ...]]:
    return HIGHLIGHT_COLORS if shape == "highlight" else COLORS


def default_color(shape: Optional[str] = None) -> List[float]:
    """The color a mark gets when none is given: highlighter yellow for
    highlights, black for everything else."""
    return list(HIGHLIGHT_COLORS["yellow"] if shape == "highlight" else COLORS["black"])


def check_svg(svg: str) -> None:
    """Reject SVG that is malformed or refers to anything outside itself.

    MuPDF resolves ``href`` and ``url()`` references, which could pull local
    files into a PDF that is then uploaded; only fragment (``#id``) and
    ``data:`` references are allowed.
    """
    if len(svg) > MAX_SVG_CHARS:
        raise MarkError(f"SVG is larger than {MAX_SVG_CHARS} characters.")
    lowered = svg.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise MarkError("SVG must not contain a DOCTYPE or entity declarations.")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        raise MarkError(f"SVG is not well-formed XML: {e}") from None
    if not root.tag.endswith("svg"):
        raise MarkError("SVG must have an <svg> root element.")

    def check_ref(ref: str) -> None:
        if ref and not ref.startswith("#") and not ref.lower().startswith("data:"):
            raise MarkError(
                f"SVG reference {ref[:60]!r} is not allowed; embed images as data: URIs."
            )

    for element in root.iter():
        for key, value in element.attrib.items():
            if key.endswith("href"):
                check_ref(value.strip())
            for ref in _URL_PATTERN.findall(value):
                check_ref(ref)
        for ref in _URL_PATTERN.findall(element.text or ""):
            check_ref(ref)
        if "@import" in (element.text or "").lower():
            raise MarkError("SVG stylesheets must not use @import.")


def _description(value: Any) -> Optional[str]:
    """A mark's description as one line of plain text, or None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise MarkError("description must be text.")
    text = " ".join(value.split())
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise MarkError(f"description must be at most {MAX_DESCRIPTION_CHARS} characters.")
    return text or None


def check_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise MarkError("Mark ids must be 1-64 characters: letters, digits, '_', '-', '.', ':'.")
    return value


# --- Rendering marks into snippets ---------------------------------------


def _points(page_rect, box: Sequence[float]):
    """A page-fraction box as a rect in page points."""
    return pymupdf.Rect(
        page_rect.x0 + box[0] * page_rect.width,
        page_rect.y0 + box[1] * page_rect.height,
        page_rect.x0 + box[2] * page_rect.width,
        page_rect.y0 + box[3] * page_rect.height,
    )


def _snippet(width: float, height: float, draw: Callable[[Any], None]) -> bytes:
    """A one-page PDF of ``width`` x ``height`` points drawn by ``draw(page)``."""
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=max(width, 0.1), height=max(height, 0.1))
        draw(page)
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()


class _Page:
    """Converts between page fractions and points on one PDF page."""

    def __init__(self, width: float, height: float):
        self.width, self.height = width, height

    def pt(self, x: float, y: float) -> Tuple[float, float]:
        return x * self.width, y * self.height

    def box(self, x0: float, y0: float, x1: float, y1: float) -> List[float]:
        """A box in points as page fractions; refuses one that leaves the page."""
        box = [x0 / self.width, y0 / self.height, x1 / self.width, y1 / self.height]
        slack = 1e-6
        if box[0] < -slack or box[1] < -slack or box[2] > 1 + slack or box[3] > 1 + slack:
            raise MarkError(
                "The mark runs off the page; move it, or make it smaller "
                "(a smaller size, or a region to wrap text in)."
            )
        return [_round(min(max(v, 0.0), 1.0)) for v in box]


def _opacity(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        raise MarkError("opacity must be a number above 0, up to 1.")
    return float(value)


def _place(request: dict, width: float, height: float, page: _Page, default: str) -> List[float]:
    """The box of a ``width`` x ``height`` mark whose ``anchor`` point is ``at``."""
    anchor = request.get("anchor", default)
    if anchor not in ANCHORS:
        raise MarkError(f"anchor must be one of: {', '.join(ANCHORS)}.")
    ax, ay = page.pt(*_point(request.get("at"), "at"))
    fx, fy = ANCHORS[anchor]
    x0, y0 = ax - fx * width, ay - fy * height
    return page.box(x0, y0, x0 + width, y0 + height)


def _fit(region: Sequence[float], width: float, height: float, page: _Page) -> List[float]:
    """The largest box of the given aspect in ``region``, at its top-left."""
    x0, y0 = page.pt(region[0], region[1])
    x1, y1 = page.pt(region[2], region[3])
    scale = min((x1 - x0) / width, (y1 - y0) / height)
    return page.box(x0, y0, x0 + width * scale, y0 + height * scale)


# Standard PDF Helvetica faces, by (bold, italic).
_FONTS = {
    (False, False): "helv",
    (True, False): "hebo",
    (False, True): "heit",
    (True, True): "hebi",
}


def _underline(snippet, size: float, color: List[float]) -> None:
    """Draw a line under each line of text on ``snippet``."""
    for block in snippet.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [span for span in line["spans"] if span["text"].strip()]
            if not spans:
                continue
            y = spans[0]["origin"][1] + size * 0.12
            x0 = min(span["bbox"][0] for span in spans)
            x1 = max(span["bbox"][2] for span in spans)
            snippet.draw_line((x0, y), (x1, y), color=color, width=max(size * 0.06, 0.5))


def _render_text(request: dict, color: List[float], page: _Page) -> Dict[str, Any]:
    text = request["text"]
    if not isinstance(text, str) or not text.strip():
        raise MarkError("text must be non-empty.")
    size = _positive(request.get("size"), "size", DEFAULT_TEXT_SIZE)
    bold = bool(request.get("bold", False))
    italic = bool(request.get("italic", False))
    underline = bool(request.get("underline", False))
    fontname = _FONTS[(bold, italic)]
    if ("at" in request) == ("region" in request):
        raise MarkError("A text mark needs either at [x, y] or region (to wrap within).")
    out: Dict[str, Any] = {"text": text, "size": size}
    out.update(
        {k: True for k, v in (("bold", bold), ("italic", italic), ("underline", underline)) if v}
    )

    if "region" in request:
        region = _region(request["region"])
        x0, y0 = page.pt(region[0], region[1])
        x1, y1 = page.pt(region[2], region[3])
        fits = []

        def draw(snippet):
            spare = snippet.insert_textbox(
                snippet.rect, text, fontname=fontname, fontsize=size, color=color
            )
            fits.append(spare >= 0)
            if underline:
                _underline(snippet, size, color)

        pdf = _snippet(x1 - x0, y1 - y0, draw)
        if not fits[0]:
            raise MarkError(
                "The text does not fit its region; make the region larger or size smaller."
            )
        return dict(out, box=page.box(x0, y0, x1, y1), pdf=pdf)

    font = pymupdf.Font(fontname)
    lines = text.split("\n")
    ascent, descent = font.ascender * size, -font.descender * size
    width = max(font.text_length(line, fontsize=size) for line in lines)
    height = ascent + descent + (len(lines) - 1) * size * LINE_SPACING

    def draw(snippet):
        for i, line in enumerate(lines):
            baseline = ascent + i * size * LINE_SPACING
            snippet.insert_text((0, baseline), line, fontname=fontname, fontsize=size, color=color)
        if underline:
            _underline(snippet, size, color)

    box = _place(request, width, height, page, "top-left")
    return dict(out, box=box, pdf=_snippet(width, height, draw))


def _draw_path(
    data: str,
    request: dict,
    page: _Page,
    *,
    stroke: Optional[List[float]],
    width: float,
    fill: Optional[List[float]],
    opacity: float,
    stretch: bool,
) -> Tuple[List[float], bytes]:
    """``(box, pdf)`` for path data placed by ``request``'s at or region.

    With ``region`` the path is scaled to fit it, centered, keeping its
    aspect unless ``stretch``; with ``at`` it is scaled so its larger side is
    ``size`` points and placed by ``anchor`` (default center). The path's
    coordinates are scaled, not the drawing, so line widths stay in points.
    """
    try:
        commands = svg_path.parse(data)
    except svg_path.PathError as e:
        raise MarkError(str(e)) from None
    # Measure the path in its own units; MuPDF reports its bounds even
    # where it lies outside the canvas.
    measure = _path_svg(data, (100, 100), 'stroke="black" fill="none"')
    with pymupdf.open("pdf", _svg_to_pdf(measure)) as doc:
        drawings = doc[0].get_drawings()
    if not drawings:
        raise MarkError("The path draws nothing.")
    bounds = pymupdf.Rect(drawings[0]["rect"])
    for drawing in drawings[1:]:
        bounds |= drawing["rect"]
    bw, bh = bounds.width, bounds.height
    if bw <= 0 and bh <= 0:
        raise MarkError("The path draws nothing.")
    pad = width / 2 if stroke else 0.0

    if ("at" in request) == ("region" in request):
        raise MarkError("Place the mark with either at [x, y] or region.")
    if "region" in request:
        if "size" in request:
            raise MarkError("size applies only with at; the region sets the size.")
        region = _region(request["region"])
        x0, y0 = page.pt(region[0], region[1])
        x1, y1 = page.pt(region[2], region[3])
        room_w, room_h = x1 - x0 - 2 * pad, y1 - y0 - 2 * pad
        if room_w <= 0 or room_h <= 0:
            raise MarkError("The region is too small for the mark's line width.")
        if stretch:
            sx = room_w / bw if bw > 0 else 1.0
            sy = room_h / bh if bh > 0 else 1.0
        else:
            sx = sy = min(room_w / bw if bw > 0 else math.inf, room_h / bh if bh > 0 else math.inf)
        out_w = room_w + 2 * pad if stretch else bw * sx + 2 * pad
        out_h = room_h + 2 * pad if stretch else bh * sy + 2 * pad
        left = x0 + (x1 - x0 - out_w) / 2
        top = y0 + (y1 - y0 - out_h) / 2
        box = page.box(left, top, left + out_w, top + out_h)
    else:
        size = _positive(request.get("size"), "size", DEFAULT_SYMBOL_SIZE)
        sx = sy = size / max(bw, bh)
        out_w, out_h = bw * sx + 2 * pad, bh * sy + 2 * pad
        box = _place(request, out_w, out_h, page, "center")

    # Center the path on any axis where it has no extent.
    off_x = pad - bounds.x0 * sx + (out_w - 2 * pad - bw * sx) / 2
    off_y = pad - bounds.y0 * sy + (out_h - 2 * pad - bh * sy) / 2
    placed = svg_path.transform(commands, sx, sy, off_x, off_y)
    style = (
        f'stroke="{_hex(stroke) if stroke else "none"}" stroke-width="{width}" '
        f'fill="{_hex(fill) if fill else "none"}" opacity="{opacity}"'
    )
    return box, _svg_to_pdf(_path_svg(placed, (out_w, out_h), style))


def _darken_rect(width: float, height: float, color: List[float], opacity: float) -> bytes:
    """A PDF of a filled box drawn with the Darken blend, as the tablet draws
    highlighter strokes: the color shows on white, and dark text under it
    stays dark. The box is placed as a form without a transparency group, so
    it blends with the page under it."""
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=width, height=height)
        state = doc.get_new_xref()
        doc.update_object(state, f"<</Type/ExtGState/BM/Darken/ca {opacity}/CA {opacity}>>")
        doc.xref_set_key(page.xref, "Resources", f"<</ExtGState<</Hl {state} 0 R>>>>")
        content = doc.get_new_xref()
        doc.update_object(content, "<<>>")
        r, g, b = color
        ops = f"q /Hl gs {r:.4f} {g:.4f} {b:.4f} rg 0 0 {width:.4f} {height:.4f} re f Q"
        doc.update_stream(content, ops.encode("ascii"))
        doc.xref_set_key(page.xref, "Contents", f"{content} 0 R")
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()


def _render_shape(request: dict, color: List[float], page: _Page) -> Dict[str, Any]:
    shape = request["shape"]
    if shape not in SHAPES:
        raise MarkError(f"shape must be one of: {', '.join(SHAPES)}.")
    out: Dict[str, Any] = {"shape": shape}

    if shape == "highlight":
        if "at" in request:
            raise MarkError("A highlight needs a region.")
        region = _region(request.get("region"))
        opacity = _opacity(request.get("opacity"), 1.0)
        x0, y0 = page.pt(region[0], region[1])
        x1, y1 = page.pt(region[2], region[3])
        pdf = _darken_rect(x1 - x0, y1 - y0, color, opacity)
        return dict(out, box=page.box(x0, y0, x1, y1), pdf=pdf)

    if shape in SHAPE_PATHS:
        preset = SHAPE_PATHS[shape]
        box, pdf = _draw_path(
            preset["path"],
            request,
            page,
            stroke=color,
            width=_positive(request.get("width"), "width", preset.get("width", DEFAULT_LINE_WIDTH)),
            fill=None,
            opacity=_opacity(request.get("opacity"), 1.0),
            stretch=preset.get("stretch", False),
        )
        return dict(out, box=box, pdf=pdf)

    width = _positive(request.get("width"), "width", DEFAULT_LINE_WIDTH)
    start, end = _point(request.get("from"), "from"), _point(request.get("to"), "to")
    ax, ay = page.pt(*start)
    bx, by = page.pt(*end)
    if (ax, ay) == (bx, by):
        raise MarkError("from and to must be different points.")
    head = max(6.0, width * 4) if shape == "arrow" else 0.0
    pad = width + head
    left, top = min(ax, bx) - pad, min(ay, by) - pad
    right, bottom = max(ax, bx) + pad, max(ay, by) + pad

    def draw(snippet):
        a = pymupdf.Point(ax - left, ay - top)
        b = pymupdf.Point(bx - left, by - top)
        snippet.draw_line(a, b, color=color, width=width)
        if shape == "arrow":
            angle = math.atan2(b.y - a.y, b.x - a.x)
            for side in (-1, 1):
                theta = angle + math.pi - side * math.radians(25)
                tip = b + (head * math.cos(theta), head * math.sin(theta))
                snippet.draw_line(b, tip, color=color, width=width)

    # The box is padded for the line width and arrowhead, so for a line along
    # the page edge it reaches past the page; responses report the endpoints.
    box = [_round(v) for v in (left / page.width, top / page.height)]
    box += [_round(v) for v in (right / page.width, bottom / page.height)]
    pdf = _snippet(right - left, bottom - top, draw)
    return dict(out, **{"from": start, "to": end}, box=box, pdf=pdf)


def _render_latex(request: dict, color: List[float], page: _Page) -> Dict[str, Any]:
    import ziamath

    source = request["latex"]
    if not isinstance(source, str) or not source.strip():
        raise MarkError("latex must be non-empty.")
    size = _positive(request.get("size"), "size", DEFAULT_LATEX_SIZE)
    if ("at" in request) == ("region" in request):
        raise MarkError("A latex mark needs either at [x, y] or region (to scale into).")
    # SVG 1.1 output writes glyphs as plain paths; MuPDF misplaces the
    # <symbol>/<use> references of SVG 2 output.
    ziamath.config.svg2 = False
    try:
        svg = ziamath.Latex(source, size=size, color=_hex(color)).svg()
        with pymupdf.open(stream=svg.encode("utf-8"), filetype="svg") as doc:
            pdf = doc.convert_to_pdf()
    except Exception as e:
        raise MarkError(f"The LaTeX could not be rendered: {e}") from None
    with pymupdf.open("pdf", pdf) as doc:
        width, height = doc[0].rect.width, doc[0].rect.height
    out: Dict[str, Any] = {"latex": source, "size": size, "pdf": pdf}
    if "region" in request:
        return dict(out, box=_fit(_region(request["region"]), width, height, page))
    return dict(out, box=_place(request, width, height, page, "top-left"))


def _path_svg(data: str, size: Sequence[float], style: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size[0]}" height="{size[1]}" '
        f'viewBox="0 0 {size[0]} {size[1]}">'
        f'<path d="{data}" {style} stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )


def _svg_to_pdf(svg: str) -> bytes:
    try:
        with pymupdf.open(stream=svg.encode("utf-8"), filetype="svg") as doc:
            return doc.convert_to_pdf()
    except Exception as e:
        raise MarkError(f"The path could not be drawn: {e}") from None


def _render_path(request: dict, color: List[float], page: _Page) -> Dict[str, Any]:
    """A path from SVG path data in any units (see ``_draw_path``)."""
    data = request["path"]
    if not isinstance(data, str) or not data.strip():
        raise MarkError('path must be SVG path data, e.g. "M 0 0 L 10 10".')
    if len(data) > MAX_PATH_CHARS or not _PATH_PATTERN.match(data):
        raise MarkError(
            "path must be SVG path data (commands M L H V C S Q T A Z and numbers), "
            f"up to {MAX_PATH_CHARS} characters."
        )
    box, pdf = _draw_path(
        data,
        request,
        page,
        stroke=color,
        width=_positive(request.get("width"), "width", DEFAULT_LINE_WIDTH),
        fill=parse_color(request["fill"]) if request.get("fill") else None,
        opacity=_opacity(request.get("opacity"), 1.0),
        stretch=bool(request.get("stretch", False)),
    )
    return {"path": data, "box": box, "pdf": pdf}


def _graphic_bytes(request: dict) -> bytes:
    if "graphic_file" in request:
        value = request["graphic_file"]
        if not isinstance(value, str) or not value.strip():
            raise MarkError("graphic_file must be a file path.")
        path = Path(value).expanduser()
        if not path.is_file():
            raise MarkError(f"graphic_file not found: {value}")
        if path.stat().st_size > MAX_GRAPHIC_FILE_BYTES:
            raise MarkError(f"graphic_file is larger than {MAX_GRAPHIC_FILE_BYTES} bytes.")
        return path.read_bytes()
    value = request["graphic"]
    if not isinstance(value, str) or not value.strip():
        raise MarkError("graphic must be SVG markup or base64 PDF, PNG or JPEG data.")
    text = value.strip()
    if text.startswith("<"):
        return text.encode("utf-8")
    if text.startswith("data:"):
        text = text.partition(",")[2]
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        raise MarkError("graphic is neither SVG markup nor valid base64.") from None
    if len(raw) > MAX_GRAPHIC_BYTES:
        raise MarkError(
            f"graphic is larger than {MAX_GRAPHIC_BYTES} bytes; pass a graphic_file instead."
        )
    return raw


def _render_graphic(request: dict, page: _Page) -> Dict[str, Any]:
    """A graphic rendered elsewhere: SVG, a one-page PDF, PNG or JPEG."""
    raw = _graphic_bytes(request)
    try:
        if raw.startswith(b"%PDF"):
            fmt = "pdf"
            with pymupdf.open(stream=raw, filetype="pdf") as src:
                if len(src) != 1:
                    raise MarkError("A graphic PDF must have exactly one page.")
                doc = pymupdf.open()
                doc.insert_pdf(src)
                pdf = doc.tobytes(garbage=3, deflate=True)
                doc.close()
        elif raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff"):
            fmt = "png" if raw.startswith(b"\x89PNG") else "jpeg"
            pixmap = pymupdf.Pixmap(raw)

            def draw(snippet):
                snippet.insert_image(snippet.rect, stream=raw)

            pdf = _snippet(pixmap.width, pixmap.height, draw)
        else:
            try:
                svg = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise MarkError("graphic must be SVG, PDF, PNG or JPEG.") from None
            if not svg.lstrip().startswith("<"):
                raise MarkError("graphic must be SVG, PDF, PNG or JPEG.")
            fmt = "svg"
            check_svg(svg.lstrip())
            with pymupdf.open(stream=svg.lstrip().encode("utf-8"), filetype="svg") as doc:
                pdf = doc.convert_to_pdf()
    except MarkError:
        raise
    except Exception as e:
        raise MarkError(f"The graphic could not be read: {e}") from None
    with pymupdf.open("pdf", pdf) as doc:
        width, height = doc[0].rect.width, doc[0].rect.height
    box = _fit(_region(request.get("region")), width, height, page)
    return {"format": fmt, "box": box, "pdf": pdf}


def render_mark(
    request: Any, page_size: Tuple[float, float], color: Optional[str]
) -> Dict[str, Any]:
    """Render one requested mark (without ``id`` or ``page``) on a page of
    ``page_size`` points.

    Exactly one kind key (see ``KINDS``) gives the content; the rest are its
    options. Without its own color a mark takes ``color`` (a name or
    ``#RRGGBB``), or ``default_color`` when that is None. Highlights take
    highlighter color names, other marks pen color names. Returns the stored form: the kind,
    the options needed to describe it, its ``box`` in page fractions and its
    PDF snippet (base64).
    """
    if not isinstance(request, dict):
        raise MarkError("Each mark must be an object.")
    kinds = [k for k in KINDS if k in request]
    if len(kinds) != 1:
        raise MarkError(
            "Each mark needs exactly one of: text, shape, path, latex, graphic, graphic_file."
        )
    kind = "graphic" if kinds[0] == "graphic_file" else kinds[0]
    page = _Page(*page_size)
    if "anchor" in request and "at" not in request:
        raise MarkError("anchor applies only to marks placed with at.")

    if kind == "graphic":
        if "color" in request:
            raise MarkError("color does not apply to graphic marks.")
        stored = _render_graphic(request, page)
    else:
        palette = _palette(request.get("shape"))
        if "color" in request:
            rgb = parse_color(request["color"], palette)
        elif color:
            rgb = parse_color(color, palette)
        else:
            rgb = default_color(request.get("shape"))
        render = {
            "text": _render_text,
            "shape": _render_shape,
            "path": _render_path,
            "latex": _render_latex,
        }[kind]
        stored = render(request, rgb, page)
        stored["color"] = rgb
    if len(stored["pdf"]) > MAX_SNIPPET_BYTES:
        raise MarkError(f"The rendered mark is larger than {MAX_SNIPPET_BYTES} bytes.")
    stored["pdf"] = base64.b64encode(stored["pdf"]).decode("ascii")
    return {"kind": kind, **stored}


# --- Editing the mark list ----------------------------------------------


def summarize(mark: Dict[str, Any], page: Optional[int]) -> Dict[str, Any]:
    """A short description of a stored mark for tool responses."""
    out: Dict[str, Any] = {"id": mark["id"], "page": page, "kind": mark["kind"]}
    for key in ("shape", "text", "latex", "path", "format"):
        if key in mark:
            out[key] = mark[key]
    if mark.get("color"):
        out["color"] = color_name(mark["color"], _palette(mark.get("shape")))
    if "from" in mark:
        out["from"], out["to"] = mark["from"], mark["to"]
    else:
        out["region"] = mark["box"]
    if mark.get("description"):
        out["description"] = mark["description"]
    return out


def edit_marks(
    marks: List[Dict[str, Any]],
    *,
    add: Sequence[Any],
    remove: Sequence[str],
    clear: bool,
    page_map: Sequence[Optional[int]],
    sizes: Sequence[Tuple[float, float]],
    color: Optional[str],
) -> Dict[str, Any]:
    """Apply removals, then additions, to a copy of ``marks``.

    ``page_map`` maps each tablet page (1-based position) to its PDF page
    index (``None`` for pages added on the tablet); ``sizes`` are the PDF's
    page sizes in points. Each added mark needs a ``page`` and may give an
    ``id``; generated ids are ``mark-N``.

    Returns ``{"marks", "added", "removed", "notes", "changed"}``.
    """
    result = [dict(m) for m in marks]
    notes: List[str] = []
    removed: List[str] = []
    if clear:
        removed = [m["id"] for m in result]
        result = []
    for mark_id in remove:
        match = next((m for m in result if m["id"] == mark_id), None)
        if match is None:
            if not clear:
                notes.append(f"No mark '{mark_id}', so it was not removed.")
            continue
        result.remove(match)
        removed.append(mark_id)

    taken = {m["id"] for m in result}
    added: List[str] = []
    counter = 1
    for request in add:
        if not isinstance(request, dict):
            raise MarkError("Each mark must be an object.")
        page = request.get("page")
        if isinstance(page, bool) or not isinstance(page, int):
            raise MarkError("Each mark needs a page number (1-based).")
        if not 1 <= page <= len(page_map):
            raise MarkError(f"Page {page} does not exist; the document has {len(page_map)}.")
        pdf_page = page_map[page - 1]
        if pdf_page is None:
            raise MarkError(f"Page {page} was added on the tablet and has no PDF page to mark.")
        if "id" in request:
            mark_id = check_id(request["id"])
            if mark_id in taken:
                raise MarkError(
                    f"Mark id '{mark_id}' is already used; remove it first to replace it."
                )
        else:
            while f"mark-{counter}" in taken:
                counter += 1
            mark_id = f"mark-{counter}"
        options = {k: v for k, v in request.items() if k not in ("id", "page", "description")}
        try:
            description = _description(request.get("description"))
            stored = render_mark(options, sizes[pdf_page], color)
        except MarkError as e:
            raise MarkError(f"Mark '{mark_id}': {e}") from None
        mark = {"id": mark_id, "pdf_page": pdf_page, **stored}
        if description:
            mark["description"] = description
        result.append(mark)
        taken.add(mark_id)
        added.append(mark_id)

    return {
        "marks": result,
        "added": added,
        "removed": removed,
        "notes": notes,
        "changed": bool(added or removed),
    }


def tablet_page(pdf_page: int, page_map: Sequence[Optional[int]]) -> Optional[int]:
    """The 1-based tablet page showing ``pdf_page``, if it is still shown."""
    for position, index in enumerate(page_map, start=1):
        if index == pdf_page:
            return position
    return None

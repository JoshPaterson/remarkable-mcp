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

BASE_FILE = "remarkable-mcp-original.pdf"
MARKS_FILE = "remarkable-mcp-marks.json"
MARKS_FORMAT = 2

KINDS = ("text", "shape", "latex", "graphic", "graphic_file")
SHAPES = ("check", "cross", "circle", "box", "line", "arrow")
COLORS = {
    "red": (0.85, 0.0, 0.0),
    "black": (0.0, 0.0, 0.0),
    "blue": (0.0, 0.25, 0.8),
    "green": (0.0, 0.55, 0.0),
    "orange": (0.95, 0.45, 0.0),
    "purple": (0.5, 0.1, 0.6),
    "gray": (0.45, 0.45, 0.45),
}
DEFAULT_TEXT_SIZE = 11.0
DEFAULT_LATEX_SIZE = 12.0
DEFAULT_SYMBOL_SIZE = 12.0
DEFAULT_LINE_WIDTH = 1.5
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

    Each line is ``{"text", "box": [left, top, right, bottom]}``, top to
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
                lines.append({"text": text, "box": [_round(min(max(v, 0.0), 1.0)) for v in box]})
    return sorted(lines, key=lambda line: (line["box"][1], line["box"][0]))


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


def _rect(value: Any, what: str) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise MarkError(f"{what} must be [left, top, right, bottom] page fractions.")
    x0, y0, x1, y1 = (_fraction(v, what) for v in value)
    if x0 >= x1 or y0 >= y1:
        raise MarkError(f"{what} must have left < right and top < bottom.")
    return [x0, y0, x1, y1]


def _positive(value: Any, what: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise MarkError(f"{what} must be a positive number.")
    return float(value)


def parse_color(value: Any) -> List[float]:
    """An RGB triple (0-1) from a color name or ``#RRGGBB``."""
    if isinstance(value, str):
        name = value.strip().lower()
        if name in COLORS:
            return list(COLORS[name])
        if re.fullmatch(r"#[0-9a-f]{6}", name):
            return [int(name[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    raise MarkError(f"Unknown color {value!r}; use {', '.join(COLORS)} or #RRGGBB.")


def color_name(rgb: Sequence[float]) -> str:
    """The name of an RGB triple from ``COLORS``, or its ``#RRGGBB`` form."""
    for name, value in COLORS.items():
        if all(abs(a - b) < 1e-6 for a, b in zip(rgb, value)):
            return name
    return _hex(rgb)


def _hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{round(v * 255):02x}" for v in rgb)


def default_color(shape: Optional[str] = None) -> List[float]:
    """The color a mark gets when none is given: check marks are green,
    everything else red."""
    return list(COLORS["green" if shape == "check" else "red"])


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
                "(a smaller size, or a rect to wrap text in)."
            )
        return [_round(min(max(v, 0.0), 1.0)) for v in box]


def _fit(rect: Sequence[float], width: float, height: float, page: _Page) -> List[float]:
    """The largest box of the given aspect in ``rect``, at its top-left."""
    x0, y0 = page.pt(rect[0], rect[1])
    x1, y1 = page.pt(rect[2], rect[3])
    scale = min((x1 - x0) / width, (y1 - y0) / height)
    return page.box(x0, y0, x0 + width * scale, y0 + height * scale)


def _render_text(request: dict, color: List[float], page: _Page) -> Dict[str, Any]:
    text = request["text"]
    if not isinstance(text, str) or not text.strip():
        raise MarkError("text must be non-empty.")
    size = _positive(request.get("size"), "size", DEFAULT_TEXT_SIZE)
    bold = bool(request.get("bold", False))
    fontname = "hebo" if bold else "helv"
    if ("at" in request) == ("rect" in request):
        raise MarkError("A text mark needs either at [x, y] or rect (to wrap within).")
    out: Dict[str, Any] = {"text": text, "size": size, "bold": bold}

    if "rect" in request:
        rect = _rect(request["rect"], "rect")
        x0, y0 = page.pt(rect[0], rect[1])
        x1, y1 = page.pt(rect[2], rect[3])
        fits = []

        def draw(snippet):
            spare = snippet.insert_textbox(
                snippet.rect, text, fontname=fontname, fontsize=size, color=color
            )
            fits.append(spare >= 0)

        pdf = _snippet(x1 - x0, y1 - y0, draw)
        if not fits[0]:
            raise MarkError("The text does not fit its rect; make the rect larger or size smaller.")
        return dict(out, box=page.box(x0, y0, x1, y1), pdf=pdf)

    at = _point(request["at"], "at")
    font = pymupdf.Font(fontname)
    lines = text.split("\n")
    ascent, descent = font.ascender * size, -font.descender * size
    width = max(font.text_length(line, fontsize=size) for line in lines)
    height = ascent + descent + (len(lines) - 1) * size * LINE_SPACING

    def draw(snippet):
        for i, line in enumerate(lines):
            baseline = ascent + i * size * LINE_SPACING
            snippet.insert_text((0, baseline), line, fontname=fontname, fontsize=size, color=color)

    x0, y0 = page.pt(*at)
    return dict(
        out, box=page.box(x0, y0, x0 + width, y0 + height), pdf=_snippet(width, height, draw)
    )


def _render_shape(request: dict, color: List[float], page: _Page) -> Dict[str, Any]:
    shape = request["shape"]
    if shape not in SHAPES:
        raise MarkError(f"shape must be one of: {', '.join(SHAPES)}.")
    out: Dict[str, Any] = {"shape": shape}

    if shape in ("check", "cross"):
        size = _positive(request.get("size"), "size", DEFAULT_SYMBOL_SIZE)
        width = _positive(request.get("width"), "width", DEFAULT_LINE_WIDTH * 1.3)
        cx, cy = page.pt(*_point(request.get("at"), "at"))
        half = size / 2 + width
        s = size / 2

        def draw(snippet):
            c = pymupdf.Point(half, half)
            if shape == "check":
                pts = [c + (-s, 0), c + (-s * 0.3, s * 0.7), c + (s, -s * 0.9)]
                snippet.draw_polyline(pts, color=color, width=width)
            else:
                snippet.draw_line(c + (-s, -s), c + (s, s), color=color, width=width)
                snippet.draw_line(c + (-s, s), c + (s, -s), color=color, width=width)

        box = page.box(cx - half, cy - half, cx + half, cy + half)
        return dict(out, size=size, box=box, pdf=_snippet(2 * half, 2 * half, draw))

    width = _positive(request.get("width"), "width", DEFAULT_LINE_WIDTH)
    if shape in ("circle", "box"):
        rect = _rect(request.get("rect"), "rect")
        x0, y0 = page.pt(rect[0], rect[1])
        x1, y1 = page.pt(rect[2], rect[3])
        w, h = x1 - x0 + width, y1 - y0 + width

        def draw(snippet):
            inner = pymupdf.Rect(width / 2, width / 2, w - width / 2, h - width / 2)
            if shape == "circle":
                snippet.draw_oval(inner, color=color, width=width)
            else:
                snippet.draw_rect(inner, color=color, width=width)

        box = page.box(x0 - width / 2, y0 - width / 2, x1 + width / 2, y1 + width / 2)
        return dict(out, box=box, pdf=_snippet(w, h, draw))

    ax, ay = page.pt(*_point(request.get("from"), "from"))
    bx, by = page.pt(*_point(request.get("to"), "to"))
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

    box = _clamped_box(page, left, top, right, bottom)
    return dict(out, box=box, pdf=_snippet(right - left, bottom - top, draw))


def _clamped_box(page: _Page, x0: float, y0: float, x1: float, y1: float) -> List[float]:
    """A box that may reach past the page edge only by its padding.

    Lines and arrows are padded for their width and head; a line drawn along
    the edge of the page keeps its full box so nothing is distorted.
    """
    box = [x0 / page.width, y0 / page.height, x1 / page.width, y1 / page.height]
    return [_round(v) for v in box]


def _render_latex(request: dict, color: List[float], page: _Page) -> Dict[str, Any]:
    import ziamath

    source = request["latex"]
    if not isinstance(source, str) or not source.strip():
        raise MarkError("latex must be non-empty.")
    size = _positive(request.get("size"), "size", DEFAULT_LATEX_SIZE)
    if ("at" in request) == ("rect" in request):
        raise MarkError("A latex mark needs either at [x, y] or rect (to scale into).")
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
    if "rect" in request:
        return dict(out, box=_fit(_rect(request["rect"], "rect"), width, height, page))
    x0, y0 = page.pt(*_point(request["at"], "at"))
    return dict(out, box=page.box(x0, y0, x0 + width, y0 + height))


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
    box = _fit(_rect(request.get("rect"), "rect"), width, height, page)
    return {"format": fmt, "box": box, "pdf": pdf}


def render_mark(
    request: Any, page_size: Tuple[float, float], color: Optional[List[float]]
) -> Dict[str, Any]:
    """Render one requested mark (without ``id`` or ``page``) on a page of
    ``page_size`` points.

    Exactly one kind key (see ``KINDS``) gives the content; the rest are its
    options. Without its own color a mark takes ``color``, or
    ``default_color`` when that is None. Returns the stored form: the kind,
    the options needed to describe it, its ``box`` in page fractions and its
    PDF snippet (base64).
    """
    if not isinstance(request, dict):
        raise MarkError("Each mark must be an object.")
    kinds = [k for k in KINDS if k in request]
    if len(kinds) != 1:
        raise MarkError(
            "Each mark needs exactly one of: text, shape, latex, graphic, graphic_file."
        )
    kind = "graphic" if kinds[0] == "graphic_file" else kinds[0]
    page = _Page(*page_size)

    if kind == "graphic":
        if "color" in request:
            raise MarkError("color does not apply to graphic marks.")
        stored = _render_graphic(request, page)
    else:
        if "color" in request:
            rgb = parse_color(request["color"])
        else:
            rgb = color or default_color(request.get("shape"))
        render = {"text": _render_text, "shape": _render_shape, "latex": _render_latex}[kind]
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
    for key in ("shape", "text", "latex", "format"):
        if key in mark:
            out[key] = mark[key]
    if mark.get("color"):
        out["color"] = color_name(mark["color"])
    out["box"] = mark["box"]
    return out


def edit_marks(
    marks: List[Dict[str, Any]],
    *,
    add: Sequence[Any],
    remove: Sequence[str],
    clear: bool,
    page_map: Sequence[Optional[int]],
    sizes: Sequence[Tuple[float, float]],
    color: Optional[List[float]],
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
        options = {k: v for k, v in request.items() if k not in ("id", "page")}
        try:
            stored = render_mark(options, sizes[pdf_page], color)
        except MarkError as e:
            raise MarkError(f"Mark '{mark_id}': {e}") from None
        result.append({"id": mark_id, "pdf_page": pdf_page, **stored})
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

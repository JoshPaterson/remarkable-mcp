"""Marks (grades, notes, shapes, SVG, images) drawn into a document's PDF.

The tablet never changes a document's PDF; ink lives in separate ``.rm``
files tied to page ids. Replacing the PDF with a copy of the same page count
and page sizes therefore keeps the user's ink in place, and anything drawn
into that copy appears under the ink.

A marked PDF carries two embedded files: the original PDF and the list of
marks. Every change rebuilds the PDF from the original plus the current
marks, so a removed mark leaves nothing behind, and removing every mark
restores the original bytes exactly.

Mark coordinates are fractions of the page (0-1 from the top-left), the frame
``remarkable_image`` renders and its ``region`` uses. Marks are stored by PDF
page index, which stays stable when pages are added or moved on the tablet.
"""

import base64
import binascii
import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymupdf

BASE_FILE = "remarkable-mcp-original.pdf"
MARKS_FILE = "remarkable-mcp-marks.json"
MARKS_FORMAT = 1

KINDS = ("text", "check", "cross", "circle", "line", "underline", "svg", "image")
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
DEFAULT_SYMBOL_SIZE = 12.0
DEFAULT_LINE_WIDTH = 1.5
MAX_SVG_CHARS = 1_000_000
MAX_IMAGE_BYTES = 2 * 1024 * 1024
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
    return base, marks


def original_pdf(pdf_bytes: bytes) -> bytes:
    """The PDF before any marks: ``pdf_bytes`` itself when it has none."""
    try:
        return load_state(pdf_bytes)[0]
    except Exception:
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
                    round(min(max((x0 - rect.x0) / rect.width, 0.0), 1.0), 3),
                    round(min(max((y0 - rect.y0) / rect.height, 0.0), 1.0), 3),
                    round(min(max((x1 - rect.x0) / rect.width, 0.0), 1.0), 3),
                    round(min(max((y1 - rect.y0) / rect.height, 0.0), 1.0), 3),
                ]
                lines.append({"text": text, "box": box})
    return sorted(lines, key=lambda line: (line["box"][1], line["box"][0]))


def page_sizes(pdf_bytes: bytes) -> List[Tuple[float, float]]:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        return [(page.rect.width, page.rect.height) for page in doc]


def build(base: bytes, marks: Sequence[Dict[str, Any]]) -> bytes:
    """The PDF with ``marks`` drawn on ``base``; ``base`` itself when none.

    Raises MarkError if drawing would change the page count or a page size,
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
            _draw(doc[index], mark)
        doc.embfile_add(BASE_FILE, base, filename=BASE_FILE, desc="PDF before marks")
        payload = json.dumps({"format": MARKS_FORMAT, "marks": list(marks)}, indent=1)
        doc.embfile_add(MARKS_FILE, payload.encode("utf-8"), filename=MARKS_FILE, desc="Marks")
        result = doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()
    if page_sizes(result) != before:
        raise MarkError("Refusing to write: the page count or a page size changed.")
    return result


# --- Validating marks -------------------------------------------------------


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
    return "#" + "".join(f"{round(v * 255):02x}" for v in rgb)


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


def decode_image(data: Any, max_bytes: int) -> bytes:
    """PNG or JPEG bytes from base64 text (a ``data:`` prefix is accepted)."""
    if not isinstance(data, str) or not data.strip():
        raise MarkError("image must be base64-encoded PNG or JPEG data.")
    text = data.strip()
    if text.startswith("data:"):
        text = text.partition(",")[2]
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        raise MarkError("image is not valid base64.") from None
    if len(raw) > max_bytes:
        raise MarkError(f"image is larger than {max_bytes} bytes.")
    if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
        raise MarkError("image must be PNG or JPEG.")
    try:
        pymupdf.Pixmap(raw)
    except Exception:
        raise MarkError("image could not be decoded.") from None
    return raw


def kind_color(kind: str) -> List[float]:
    """The color a mark of ``kind`` gets when none is given: check marks are
    green, everything else red."""
    return list(COLORS["green" if kind == "check" else "red"])


def normalize_mark(
    mark: Any, *, default_color: Optional[List[float]], max_image_bytes: int
) -> Dict[str, Any]:
    """A validated copy of one requested mark, without ``id`` or page.

    Exactly one kind key (see ``KINDS``) gives the content; the rest are
    options for that kind. Without its own color a mark takes
    ``default_color``, or ``kind_color`` when that is None.
    """
    if not isinstance(mark, dict):
        raise MarkError("Each mark must be an object.")
    kinds = [k for k in KINDS if k in mark]
    if len(kinds) != 1:
        raise MarkError(f"Each mark needs exactly one of: {', '.join(KINDS)}.")
    kind = kinds[0]
    out: Dict[str, Any] = {"kind": kind}
    if "color" in mark:
        out["color"] = parse_color(mark["color"])
    else:
        out["color"] = list(default_color) if default_color else kind_color(kind)
    value = mark[kind]

    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            raise MarkError("text must be non-empty.")
        out["text"] = value
        out["size"] = _positive(mark.get("size"), "size", DEFAULT_TEXT_SIZE)
        out["bold"] = bool(mark.get("bold", False))
        if ("at" in mark) == ("rect" in mark):
            raise MarkError("A text mark needs either at [x, y] or rect (to wrap).")
        if "at" in mark:
            out["at"] = _point(mark["at"], "at")
        else:
            out["rect"] = _rect(mark["rect"], "rect")
    elif kind in ("check", "cross"):
        out["at"] = _point(value, kind)
        out["size"] = _positive(mark.get("size"), "size", DEFAULT_SYMBOL_SIZE)
        out["width"] = _positive(mark.get("width"), "width", DEFAULT_LINE_WIDTH * 1.3)
    elif kind in ("circle", "line"):
        if kind == "circle":
            out["rect"] = _rect(value, kind)
        else:
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                raise MarkError("line must be [x0, y0, x1, y1] page fractions.")
            out["points"] = [_fraction(v, "line") for v in value]
        out["width"] = _positive(mark.get("width"), "width", DEFAULT_LINE_WIDTH)
    elif kind == "underline":
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise MarkError("underline must be [x0, y, x1] page fractions.")
        x0, y, x1 = (_fraction(v, "underline") for v in value)
        if x0 >= x1:
            raise MarkError("underline must have x0 < x1.")
        out["points"] = [x0, y, x1, y]
        out["width"] = _positive(mark.get("width"), "width", DEFAULT_LINE_WIDTH)
    elif kind == "svg":
        if not isinstance(value, str):
            raise MarkError("svg must be SVG markup.")
        check_svg(value)
        out["svg"] = value
        out["rect"] = _rect(mark.get("rect"), "rect")
        del out["color"]
    else:
        raw = decode_image(value, max_image_bytes)
        out["image"] = base64.b64encode(raw).decode("ascii")
        out["rect"] = _rect(mark.get("rect"), "rect")
        del out["color"]
    if kind in ("svg", "image") and "color" in mark:
        raise MarkError(f"color does not apply to {kind} marks.")
    return out


def check_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise MarkError("Mark ids must be 1-64 characters: letters, digits, '_', '-', '.', ':'.")
    return value


# --- Drawing ------------------------------------------------------------


def _page_point(page, x: float, y: float):
    rect = page.rect
    return pymupdf.Point(rect.x0 + x * rect.width, rect.y0 + y * rect.height)


def _page_rect(page, box: Sequence[float]):
    return pymupdf.Rect(_page_point(page, box[0], box[1]), _page_point(page, box[2], box[3]))


def _draw(page, mark: Dict[str, Any]) -> None:
    if page.rotation:
        raise MarkError("Rotated PDF pages are not supported.")
    kind = mark["kind"]
    color = tuple(mark.get("color") or (0, 0, 0))
    if kind == "text":
        font = "hebo" if mark.get("bold") else "helv"
        size = mark["size"]
        if "at" in mark:
            top_left = _page_point(page, *mark["at"])
            page.insert_text(
                top_left + (0, size * 0.8),
                mark["text"],
                fontname=font,
                fontsize=size,
                color=color,
            )
        else:
            spare = page.insert_textbox(
                _page_rect(page, mark["rect"]),
                mark["text"],
                fontname=font,
                fontsize=size,
                color=color,
            )
            if spare < 0:
                raise MarkError(
                    f"Text of mark '{mark['id']}' does not fit its rect; "
                    "make the rect larger or the size smaller."
                )
    elif kind in ("check", "cross"):
        c = _page_point(page, *mark["at"])
        s = mark["size"] / 2
        if kind == "check":
            pts = [c + (-s, 0), c + (-s * 0.3, s * 0.7), c + (s, -s * 0.9)]
            page.draw_polyline(pts, color=color, width=mark["width"])
        else:
            page.draw_line(c + (-s, -s), c + (s, s), color=color, width=mark["width"])
            page.draw_line(c + (-s, s), c + (s, -s), color=color, width=mark["width"])
    elif kind == "circle":
        page.draw_oval(_page_rect(page, mark["rect"]), color=color, width=mark["width"])
    elif kind in ("line", "underline"):
        x0, y0, x1, y1 = mark["points"]
        page.draw_line(
            _page_point(page, x0, y0),
            _page_point(page, x1, y1),
            color=color,
            width=mark["width"],
        )
    elif kind == "svg":
        try:
            with pymupdf.open(stream=mark["svg"].encode("utf-8"), filetype="svg") as svg:
                pdf = pymupdf.open("pdf", svg.convert_to_pdf())
        except Exception as e:
            raise MarkError(f"SVG of mark '{mark['id']}' could not be drawn: {e}") from None
        with pdf:
            page.show_pdf_page(_page_rect(page, mark["rect"]), pdf, 0)
    elif kind == "image":
        page.insert_image(_page_rect(page, mark["rect"]), stream=base64.b64decode(mark["image"]))


# --- Editing the mark list ----------------------------------------------


def summarize(mark: Dict[str, Any], page: Optional[int]) -> Dict[str, Any]:
    """A short description of a stored mark for tool responses."""
    out: Dict[str, Any] = {"id": mark["id"], "page": page, "kind": mark["kind"]}
    if mark.get("color"):
        out["color"] = color_name(mark["color"])
    if mark["kind"] == "text":
        out["text"] = mark["text"]
    for key in ("at", "rect", "points"):
        if key in mark:
            out[key] = mark[key]
    return out


def edit_marks(
    marks: List[Dict[str, Any]],
    *,
    add: Sequence[Any],
    remove: Sequence[str],
    clear: bool,
    page_map: Sequence[Optional[int]],
    default_color: Optional[List[float]],
    max_image_bytes: int,
) -> Dict[str, Any]:
    """Apply removals, then additions, to a copy of ``marks``.

    ``page_map`` maps each tablet page (1-based position) to its PDF page
    index (``None`` for pages added on the tablet). Each added mark needs a
    ``page`` and may give an ``id``; generated ids are ``mark-N``.

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
        stored = normalize_mark(
            options, default_color=default_color, max_image_bytes=max_image_bytes
        )
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

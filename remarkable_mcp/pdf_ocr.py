"""OCR written into a PDF: an invisible text layer over scanned pages.

A scanned page is an image with no text, so it cannot be read, searched or
outlined. OCR finds its words and their positions, and each word is written
over the image as invisible text (PDF render mode 3) in the same place, as
OCR tools such as OCRmyPDF do. The page looks the same; its text can now be
extracted, searched on the tablet and selected for highlighting.

Only pages without text are OCR'd, so a page's existing text (and any tablet
highlights on it) is never changed. A whole PDF is OCR'd before it is
written; work in progress is kept in a file so a long book can be finished
over several calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pymupdf

# A page needs OCR when it has no text but images cover at least this share
# of it (a scan), rather than being blank.
SCAN_COVERAGE = 0.5
# Rendering resolution for OCR, and the largest image side sent to OCR.
OCR_DPI = 300
MAX_OCR_SIDE = 4200
# Seconds of OCR per call before returning progress, so calls stay short.
TIME_BUDGET = 40.0
WORK_DIR_NAME = "ocr-work"
FONT_NAME = "ocrtext"

# A word: (text, x0, y0, x1, y1) in image pixels, then the id of its line;
# words of one line are written together, in order.
Word = tuple


class OcrError(Exception):
    """OCR cannot be run (no backend) or failed."""


def pages_needing_ocr(pdf_bytes: bytes) -> Dict[str, List[int]]:
    """``{"scanned": [...], "rotated": [...]}``: 0-based pages with no text
    that are mostly image, and those of them that are rotated (not
    supported)."""
    scanned, rotated = [], []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for index, page in enumerate(doc):
            if page.get_text("text").strip():
                continue
            area = abs(page.rect)
            covered = 0.0
            for info in page.get_image_info():
                box = pymupdf.Rect(info["bbox"]) & page.rect
                covered += abs(box)
            if area and covered / area >= SCAN_COVERAGE:
                (rotated if page.rotation else scanned).append(index)
    return {"scanned": scanned, "rotated": rotated}


def _render(page: "pymupdf.Page") -> Tuple[bytes, float]:
    """``(png, pixels_per_point)`` for OCR."""
    zoom = OCR_DPI / 72
    longest = max(page.rect.width, page.rect.height) * zoom
    if longest > MAX_OCR_SIDE:
        zoom *= MAX_OCR_SIDE / longest
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY)
    return pix.tobytes("png"), zoom


def tesseract_words(png: bytes) -> List[Word]:
    """Words found by Tesseract (page layout analysis, for printed pages)."""
    import io

    from PIL import Image

    pytesseract = _tesseract()
    data = pytesseract.image_to_data(
        Image.open(io.BytesIO(png)), config="--psm 3 --oem 3", output_type=pytesseract.Output.DICT
    )
    words = []
    for i, text in enumerate(data["text"]):
        text = str(text).strip()
        if not text or float(data["conf"][i]) < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        line = f"{data['block_num'][i]}.{data['par_num'][i]}.{data['line_num'][i]}"
        words.append((text, x, y, x + w, y + h, line))
    return words


def google_words(png: bytes, api_key: str) -> List[Word]:
    """Words found by Google Vision's document text detection."""
    import base64

    import requests

    response = requests.post(
        f"https://vision.googleapis.com/v1/images:annotate?key={api_key}",
        json={
            "requests": [
                {
                    "image": {"content": base64.b64encode(png).decode("ascii")},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        },
        timeout=120,
    )
    if response.status_code != 200:
        raise OcrError(f"Google Vision returned {response.status_code}.")
    annotation = (response.json().get("responses") or [{}])[0].get("fullTextAnnotation") or {}
    words, line = [], 0
    for page in annotation.get("pages") or []:
        for block in page.get("blocks") or []:
            for paragraph in block.get("paragraphs") or []:
                line += 1
                for word in paragraph.get("words") or []:
                    symbols = word.get("symbols") or []
                    text = "".join(s.get("text", "") for s in symbols)
                    vertices = (word.get("boundingBox") or {}).get("vertices") or []
                    if text.strip() and vertices:
                        xs = [v.get("x", 0) for v in vertices]
                        ys = [v.get("y", 0) for v in vertices]
                        words.append((text, min(xs), min(ys), max(xs), max(ys), line))
                    end = (
                        ((symbols[-1].get("property") or {}).get("detectedBreak") or {}).get("type")
                        if symbols
                        else None
                    )
                    if end in ("EOL_SURE_SPACE", "LINE_BREAK", "HYPHEN"):
                        line += 1
    return words


def _tesseract():
    """``pytesseract``, finding Homebrew's Tesseract when it is not on PATH."""
    import shutil

    try:
        import pytesseract
    except ImportError as e:
        raise OcrError("OCR needs pytesseract and Tesseract installed.") from e
    if shutil.which(pytesseract.pytesseract.tesseract_cmd) is None:
        for path in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
            if os.path.exists(path):
                pytesseract.pytesseract.tesseract_cmd = path
                break
        else:
            raise OcrError("Tesseract is not installed (brew install tesseract).")
    return pytesseract


def ocr_backend() -> Tuple[str, Callable[[bytes], List[Word]]]:
    """``(name, words_fn)``: Google Vision when an API key is set, else Tesseract."""
    from remarkable_mcp.extract import get_ocr_backend

    api_key = os.environ.get("GOOGLE_VISION_API_KEY")
    choice = get_ocr_backend()
    if api_key and choice in ("auto", "google"):
        return "google", lambda png: google_words(png, api_key)
    _tesseract()
    return "tesseract", tesseract_words


def _lines(words: List[Word]) -> List[Word]:
    """Consecutive words with the same line id joined into one entry with
    spaces between them; words without a line id stay on their own."""
    lines: List[list] = []
    for word in words:
        line_id = word[5] if len(word) > 5 else None
        if lines and line_id is not None and lines[-1][5] == line_id:
            last = lines[-1]
            last[0] += " " + word[0]
            last[1], last[2] = min(last[1], word[1]), min(last[2], word[2])
            last[3], last[4] = max(last[3], word[3]), max(last[4], word[4])
        else:
            lines.append([word[0], word[1], word[2], word[3], word[4], line_id])
    return [tuple(line) for line in lines]


def add_text_layer(doc: "pymupdf.Document", index: int, words: List[Word], zoom: float) -> int:
    """Write ``words`` (pixel boxes at ``zoom`` pixels per point) as invisible
    text on page ``index``, a line at a time, each stretched to its box.
    Returns the number of lines written."""
    page = doc[index]
    font = pymupdf.Font("notos")
    page.insert_font(fontname=FONT_NAME, fontbuffer=font.buffer)
    count = 0
    for text, x0, y0, x1, y1, _ in _lines(words):
        box = pymupdf.Rect(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom) + (
            page.rect.x0,
            page.rect.y0,
            page.rect.x0,
            page.rect.y0,
        )
        if box.is_empty:
            continue
        size = max(1.0, box.height * 0.9)
        length = font.text_length(text, fontsize=size)
        if length <= 0:
            continue
        origin = pymupdf.Point(box.x0, box.y1 - box.height * 0.15)
        page.insert_text(
            origin,
            text,
            fontname=FONT_NAME,
            fontsize=size,
            render_mode=3,
            morph=(origin, pymupdf.Matrix(box.width / length, 1)),
        )
        count += 1
    return count


def with_text_layers(base: bytes, pages: Dict[int, dict]) -> bytes:
    """``base`` with each page's OCR words (``{"zoom", "words"}``) written in."""
    doc = pymupdf.open(stream=base, filetype="pdf")
    try:
        for index, result in sorted(pages.items()):
            add_text_layer(doc, index, [tuple(w) for w in result["words"]], result["zoom"])
        doc.subset_fonts()
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()


# --- Work in progress ------------------------------------------------------


def _work_path(doc_id: str, base: bytes, directory: Optional[Path] = None) -> Path:
    from remarkable_mcp.api import REMARKABLE_CONFIG_DIR

    digest = hashlib.sha256(base).hexdigest()[:16]
    return (directory or REMARKABLE_CONFIG_DIR / WORK_DIR_NAME) / f"{doc_id}-{digest}.json"


def load_work(doc_id: str, base: bytes, directory: Optional[Path] = None) -> Dict[int, dict]:
    """OCR results kept from earlier calls for this exact PDF."""
    try:
        data = json.loads(_work_path(doc_id, base, directory).read_text())
        return {int(k): v for k, v in data.get("pages", {}).items()}
    except (OSError, ValueError):
        return {}


def save_work(
    doc_id: str, base: bytes, pages: Dict[int, dict], directory: Optional[Path] = None
) -> None:
    path = _work_path(doc_id, base, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps({"pages": {str(k): v for k, v in pages.items()}}))
    os.replace(tmp, path)


def clear_work(doc_id: str, directory: Optional[Path] = None) -> None:
    """Remove this document's work files (the MCP's own temporary files)."""
    from remarkable_mcp.api import REMARKABLE_CONFIG_DIR

    folder = directory or REMARKABLE_CONFIG_DIR / WORK_DIR_NAME
    for path in folder.glob(f"{doc_id}-*.json"):
        path.unlink(missing_ok=True)


def run_ocr(
    doc_id: str,
    base: bytes,
    pages: List[int],
    words_fn: Callable[[bytes], List[Word]],
    *,
    budget: float = TIME_BUDGET,
    clock: Callable[[], float] = time.monotonic,
    directory: Optional[Path] = None,
) -> Dict[int, dict]:
    """OCR ``pages`` of ``base``, continuing earlier work, until done or
    ``budget`` seconds have passed. Returns every finished page's
    ``{"zoom", "words"}``; progress is saved after each page."""
    done = load_work(doc_id, base, directory)
    start = clock()
    with pymupdf.open(stream=base, filetype="pdf") as doc:
        for index in pages:
            if index in done:
                continue
            if clock() - start >= budget:
                break
            png, zoom = _render(doc[index])
            done[index] = {"zoom": zoom, "words": [list(w) for w in words_fn(png)]}
            save_work(doc_id, base, done, directory)
    return done


def summary(pages: Dict[int, Any]) -> int:
    return sum(len(result["words"]) for result in pages.values())

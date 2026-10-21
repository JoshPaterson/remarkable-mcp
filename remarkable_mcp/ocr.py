"""OCR backends: backend selection, the Google Vision client and Tesseract.

Callers get text in three shapes:

- ``image_text``: plain text for a page image (``remarkable_image``);
- ``google_line_reader`` / ``tesseract_lines``: handwriting lines with pixel
  boxes, and Tesseract's confidence, in reading order;
- ``google_words`` / ``tesseract_words``: words with pixel boxes and line ids,
  for invisible PDF text layers.

``REMARKABLE_OCR_BACKEND`` picks ``auto`` (Google Vision when
``GOOGLE_VISION_API_KEY`` is set, else Tesseract), ``google`` or
``tesseract``. The API key is sent in the ``x-goog-api-key`` header, never in
the URL.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

OCR_BACKENDS = frozenset({"auto", "google", "tesseract"})

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"

# Where Homebrew and other installers put Tesseract; apps started outside a
# shell (such as the server under Claude Desktop) often lack these in PATH.
TESSERACT_LOCATIONS = (
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/usr/bin/tesseract",
)

# Tesseract settings for handwriting: PSM 11 = sparse text, find as much text
# as possible.
HANDWRITING_CONFIG = r"--psm 11 --oem 3"
# Tesseract settings for printed pages: full page layout analysis.
PRINTED_CONFIG = "--psm 3 --oem 3"

# An OCR line: (text, pixel box), plus Tesseract's confidence (0-100).
OcrLine = tuple
# A word: (text, x0, y0, x1, y1) in image pixels, then the id of its line.
Word = tuple


class OcrError(ValueError):
    """OCR cannot be run (no backend) or failed."""


class VisionError(OcrError):
    """Google Vision answered with an error status."""

    def __init__(self, status: int):
        super().__init__(f"Google Vision returned {status}.")
        self.status = status


class VisionAuthError(VisionError):
    """The Vision API key is invalid or the API is not enabled."""


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


def google_api_key() -> Optional[str]:
    """The Google Vision API key, or None when it is not set."""
    return os.environ.get("GOOGLE_VISION_API_KEY") or None


def preferred_backend() -> str:
    """``"google"`` when Google Vision is tried first, else ``"tesseract"``.

    Google is tried first when chosen explicitly, or under ``auto`` when an
    API key is set. Callers fall back to Tesseract when Google fails or finds
    nothing.
    """
    choice = get_ocr_backend()
    if choice == "google" or (choice == "auto" and google_api_key()):
        return "google"
    return "tesseract"


# ---------------------------------------------------------------------------
# Google Vision
# ---------------------------------------------------------------------------


def vision_annotate(png: bytes, api_key: str, timeout: float = 60) -> Dict[str, Any]:
    """The ``fullTextAnnotation`` of Vision's document text detection for
    ``png`` (empty when nothing was found). Raises VisionAuthError on 401 or
    403 and VisionError on any other non-200 status."""
    import requests

    response = requests.post(
        VISION_URL,
        headers={"x-goog-api-key": api_key},
        json={
            "requests": [
                {
                    "image": {"content": base64.b64encode(png).decode("ascii")},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        },
        timeout=timeout,
    )
    if response.status_code in (401, 403):
        raise VisionAuthError(response.status_code)
    if response.status_code != 200:
        raise VisionError(response.status_code)
    responses = response.json().get("responses") or [{}]
    return responses[0].get("fullTextAnnotation") or {}


def google_text(png: bytes) -> Optional[str]:
    """Plain text Google Vision finds in ``png``; None when no key is set,
    the request fails or nothing was found."""
    api_key = google_api_key()
    if not api_key:
        return None
    try:
        text = (vision_annotate(png, api_key).get("text") or "").strip()
    except Exception:
        # OCR is best-effort; the caller falls back to Tesseract.
        return None
    return text or None


def vision_lines(annotation: Dict[str, Any]) -> List[OcrLine]:
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


def google_line_reader() -> Callable[[bytes], Optional[List[OcrLine]]]:
    """A function ``png -> [(text, box)]`` reading handwriting with Google
    Vision: the REST API with ``GOOGLE_VISION_API_KEY`` when set, else the
    google-cloud-vision SDK with service account credentials.

    The REST reader raises VisionAuthError when the key is rejected and
    VisionError on other error statuses. Raises ImportError when the SDK is
    needed but not installed.
    """
    api_key = google_api_key()
    if api_key:
        return lambda png: vision_lines(vision_annotate(png, api_key))

    from google.cloud import vision

    client = vision.ImageAnnotatorClient()

    def read_lines(png: bytes) -> Optional[List[OcrLine]]:
        # DOCUMENT_TEXT_DETECTION gives the best handwriting results.
        response = client.document_text_detection(image=vision.Image(content=png))
        if response.error.message:
            return None
        annotation = response.full_text_annotation
        return vision_lines(type(annotation).to_dict(annotation))

    return read_lines


def vision_words(annotation: Dict[str, Any]) -> List[Word]:
    """Words ``(text, x0, y0, x1, y1, line)`` from a Vision
    ``fullTextAnnotation``; a new line id starts at each paragraph and after
    each line break."""
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


def google_words(png: bytes, api_key: str) -> List[Word]:
    """Words found by Google Vision's document text detection. Raises
    VisionError when Vision answers with an error status."""
    return vision_words(vision_annotate(png, api_key, timeout=120))


# ---------------------------------------------------------------------------
# Tesseract
# ---------------------------------------------------------------------------


def tesseract_module():
    """``pytesseract``, pointed at the Tesseract program when it is not on
    PATH but installed in a usual place. Raises ImportError when pytesseract
    is missing."""
    import pytesseract

    if shutil.which(pytesseract.pytesseract.tesseract_cmd) is None:
        found = next((path for path in TESSERACT_LOCATIONS if os.path.exists(path)), None)
        if found:
            pytesseract.pytesseract.tesseract_cmd = found
    return pytesseract


def require_tesseract():
    """``pytesseract`` with the Tesseract program found; raises OcrError when
    either is missing."""
    try:
        pytesseract = tesseract_module()
    except ImportError as e:
        raise OcrError("OCR needs pytesseract and Tesseract installed.") from e
    if shutil.which(pytesseract.pytesseract.tesseract_cmd) is None:
        raise OcrError("Tesseract is not installed (brew install tesseract).")
    return pytesseract


# Seconds one Tesseract run may take.
TESSERACT_TIMEOUT = 120
TSV_NUMBERS = ("block_num", "par_num", "line_num", "left", "top", "width", "height")


def _run_tesseract(image, config: str, output: str = "") -> str:
    """Tesseract's stdout for the PIL ``image``, sent as a PNG on stdin so no
    temporary file is needed (a sandboxed server may not share its temporary
    folder with the program). ``output`` names a config such as ``tsv``.
    Raises OcrError when Tesseract fails."""
    pytesseract = tesseract_module()
    command = shutil.which(pytesseract.pytesseract.tesseract_cmd)
    if command is None:
        raise OcrError("Tesseract is not installed (brew install tesseract).")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    args = [command, "stdin", "stdout", *config.split()] + ([output] if output else [])
    try:
        result = subprocess.run(
            args, input=buffer.getvalue(), capture_output=True, timeout=TESSERACT_TIMEOUT
        )
    except subprocess.TimeoutExpired as e:
        raise OcrError("Tesseract took too long.") from e
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OcrError(f"Tesseract failed: {error[-1] if error else result.returncode}")
    return result.stdout.decode("utf-8", "replace")


def _tesseract_data(image, config: str) -> Dict[str, list]:
    """Tesseract's TSV output as columns (``text``, ``conf``, boxes and
    block/paragraph/line numbers)."""
    rows = [line.split("\t") for line in _run_tesseract(image, config, "tsv").splitlines()]
    if not rows:
        return {"text": []}
    header, body = rows[0], [row for row in rows[1:] if len(row) == len(rows[0])]
    data = {name: [row[i] for row in body] for i, name in enumerate(header)}
    for name in TSV_NUMBERS:
        data[name] = [int(v) for v in data.get(name, [])]
    data["conf"] = [float(v) for v in data.get("conf", [])]
    return data


def _handwriting_image(png: bytes):
    """``png`` prepared for handwriting OCR: grayscale, more contrast and
    slight sharpening."""
    from PIL import Image, ImageFilter, ImageOps

    img = Image.open(io.BytesIO(png)).convert("L")
    img = ImageOps.autocontrast(img, cutoff=2)
    return img.filter(ImageFilter.SHARPEN)


def tesseract_text(png: bytes) -> Optional[str]:
    """Plain text Tesseract finds in ``png``; None when Tesseract is
    unavailable, fails or finds nothing."""
    try:
        text = _run_tesseract(_handwriting_image(png), HANDWRITING_CONFIG)
    except Exception:
        return None
    return text.strip() or None


def tesseract_lines(png: bytes) -> List[OcrLine]:
    """Lines ``(text, pixel box, confidence)`` found by Tesseract, in reading
    order."""
    data = _tesseract_data(_handwriting_image(png), HANDWRITING_CONFIG)
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


def tesseract_words(png: bytes) -> List[Word]:
    """Words found by Tesseract (page layout analysis, for printed pages).
    Raises OcrError when Tesseract is unavailable."""
    from PIL import Image

    require_tesseract()
    data = _tesseract_data(Image.open(io.BytesIO(png)), PRINTED_CONFIG)
    words = []
    for i, text in enumerate(data["text"]):
        text = str(text).strip()
        if not text or float(data["conf"][i]) < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        line = f"{data['block_num'][i]}.{data['par_num'][i]}.{data['line_num'][i]}"
        words.append((text, x, y, x + w, y + h, line))
    return words


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def image_text(png: bytes) -> Tuple[Optional[str], Optional[str]]:
    """``(text, backend)`` for a page image: Google Vision first when it is
    preferred, then Tesseract. ``backend`` names the backend that produced the
    text; both are None when nothing was found."""
    if preferred_backend() == "google":
        text = google_text(png)
        if text:
            return text, "google"
    text = tesseract_text(png)
    return (text, "tesseract") if text else (None, None)


def pdf_words_backend() -> Tuple[str, Callable[[bytes], List[Word]]]:
    """``(name, words_fn)`` for PDF text layers: Google Vision when it is
    preferred and an API key is set, else Tesseract (raises OcrError when
    Tesseract is unavailable)."""
    api_key = google_api_key()
    if api_key and preferred_backend() == "google":
        return "google", lambda png: google_words(png, api_key)
    require_tesseract()
    return "tesseract", tesseract_words

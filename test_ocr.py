"""OCR backends: the Vision API key travels in a header, and the backend
reported is the one that produced the text."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from remarkable_mcp import extract, ocr

KEY = "secret-vision-key"

ANNOTATION = {
    "text": "hello world",
    "pages": [
        {
            "blocks": [
                {
                    "paragraphs": [
                        {
                            "words": [
                                {
                                    "boundingBox": {
                                        "vertices": [{"x": 1, "y": 2}, {"x": 30, "y": 12}]
                                    },
                                    "symbols": [
                                        {"text": "h"},
                                        {"text": "i"},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    ],
}


def _response(status=200, annotation=None):
    response = Mock(status_code=status)
    response.json.return_value = {"responses": [{"fullTextAnnotation": annotation or {}}]}
    return response


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setenv("GOOGLE_VISION_API_KEY", KEY)
    monkeypatch.setenv("REMARKABLE_OCR_BACKEND", "google")


def _assert_key_in_header_only(post):
    for call in post.call_args_list:
        url = call.args[0] if call.args else call.kwargs["url"]
        assert KEY not in url
        assert "key=" not in url
        assert call.kwargs["headers"] == {"x-goog-api-key": KEY}


@pytest.mark.parametrize(
    "read",
    [
        lambda: ocr.google_text(b"png"),
        lambda: ocr.google_words(b"png", KEY),
        lambda: ocr.google_line_reader()(b"png"),
    ],
    ids=["text", "words", "lines"],
)
def test_vision_key_is_sent_in_header_not_url(google, read):
    with patch("requests.post", return_value=_response(annotation=ANNOTATION)) as post:
        assert read()
    post.assert_called_once()
    _assert_key_in_header_only(post)


def test_vision_errors_do_not_mention_the_key(google):
    with patch("requests.post", return_value=_response(status=500)):
        with pytest.raises(ocr.OcrError) as raised:
            ocr.google_words(b"png", KEY)
    assert KEY not in str(raised.value)
    assert str(raised.value) == "Google Vision returned 500."


def test_handwriting_reports_tesseract_after_vision_rejects_key(google, monkeypatch):
    monkeypatch.setattr(extract, "_ocr_page_images", lambda rm_file, size: (b"png", []))
    monkeypatch.setattr(ocr, "tesseract_lines", lambda png: [("x = 4", (0, 0, 10, 10), 95)])
    with patch("requests.post", return_value=_response(status=403)) as post:
        pages, backend = extract.extract_handwriting_ocr([Path("page.rm")])

    assert backend == "tesseract"
    assert pages == [[{"text": "x = 4", "confidence": 95}]]
    _assert_key_in_header_only(post)


def test_handwriting_reports_tesseract_when_vision_sdk_fails(monkeypatch):
    monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
    monkeypatch.setenv("REMARKABLE_OCR_BACKEND", "google")

    def no_sdk():
        raise ImportError("google-cloud-vision")

    monkeypatch.setattr(ocr, "google_line_reader", no_sdk)
    monkeypatch.setattr(extract, "_ocr_page_images", lambda rm_file, size: (b"png", []))
    monkeypatch.setattr(ocr, "tesseract_lines", lambda png: [("note", (0, 0, 10, 10), 90)])

    pages, backend = extract.extract_handwriting_ocr([Path("page.rm")])

    assert backend == "tesseract"
    assert pages == [[{"text": "note", "confidence": 90}]]


def test_handwriting_reports_google_when_vision_reads(google, monkeypatch):
    monkeypatch.setattr(extract, "_ocr_page_images", lambda rm_file, size: (b"png", []))
    with patch("requests.post", return_value=_response(annotation=ANNOTATION)):
        pages, backend = extract.extract_handwriting_ocr([Path("page.rm")])
    assert backend == "google"
    assert pages == [[{"text": "hi"}]]


def test_image_text_reports_the_backend_that_read_it(google, monkeypatch):
    monkeypatch.setattr(ocr, "tesseract_text", lambda png: "local")
    with patch("requests.post", return_value=_response(status=403)):
        assert ocr.image_text(b"png") == ("local", "tesseract")
    with patch("requests.post", return_value=_response(annotation=ANNOTATION)):
        assert ocr.image_text(b"png") == ("hello world", "google")
    monkeypatch.setattr(ocr, "tesseract_text", lambda png: None)
    with patch("requests.post", return_value=_response()):
        assert ocr.image_text(b"png") == (None, None)


@pytest.mark.parametrize(
    ("backend", "key", "expected"),
    [
        ("auto", KEY, "google"),
        ("auto", None, "tesseract"),
        ("google", None, "google"),
        ("tesseract", KEY, "tesseract"),
    ],
)
def test_preferred_backend(monkeypatch, backend, key, expected):
    monkeypatch.setenv("REMARKABLE_OCR_BACKEND", backend)
    if key:
        monkeypatch.setenv("GOOGLE_VISION_API_KEY", key)
    else:
        monkeypatch.delenv("GOOGLE_VISION_API_KEY", raising=False)
    assert ocr.preferred_backend() == expected


def test_pdf_words_use_google_only_with_a_key(monkeypatch):
    monkeypatch.setenv("REMARKABLE_OCR_BACKEND", "google")
    monkeypatch.setenv("GOOGLE_VISION_API_KEY", KEY)
    assert ocr.pdf_words_backend()[0] == "google"
    monkeypatch.delenv("GOOGLE_VISION_API_KEY")
    monkeypatch.setattr(ocr, "require_tesseract", lambda: None)
    assert ocr.pdf_words_backend() == ("tesseract", ocr.tesseract_words)

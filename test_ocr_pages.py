"""OCR'd handwriting by page, in reading order, with the layers that drew it."""

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw

from remarkable_mcp import extract
from remarkable_mcp.exporters import ExportMetadata, write_markdown_export


def _layer_png(size, boxes):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for box in boxes:
        draw.rectangle(box, fill=(0, 0, 0, 255))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_vision_lines_follow_detected_breaks():
    def word(text, x0, y0, x1, y1, end=None):
        symbols = [{"text": c} for c in text]
        if end:
            symbols[-1]["property"] = {"detectedBreak": {"type": end}}
        box = {"vertices": [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}]}
        return {"boundingBox": box, "symbols": symbols}

    annotation = {
        "pages": [
            {
                "blocks": [
                    {
                        "paragraphs": [
                            {
                                "words": [
                                    word("x", 10, 10, 20, 30, "SPACE"),
                                    word("=", 25, 10, 35, 30, "SPACE"),
                                    word("3", 40, 10, 50, 30, "LINE_BREAK"),
                                    word("check", 10, 50, 60, 70),
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }
    assert extract._vision_lines(annotation) == [
        ("x = 3", (10, 10, 50, 30)),
        ("check", (10, 50, 60, 70)),
    ]


def test_lines_name_the_layers_that_drew_them():
    size = (200, 100)
    layers = [
        ("Layer 1", _layer_png(size, [(10, 10, 90, 30), (10, 60, 50, 80)])),
        ("Layer 2", _layer_png(size, [(110, 10, 190, 30), (60, 60, 100, 80)])),
    ]
    lines = [
        ("answer", (10, 10, 90, 30)),
        ("correction", (110, 10, 190, 30)),
        ("mixed", (10, 60, 100, 80)),
    ]
    tagged = extract._tag_line_layers(lines, layers)

    assert [line["layers"] for line in tagged] == [
        ["Layer 1"],
        ["Layer 2"],
        ["Layer 1", "Layer 2"],
    ]
    # One layer (or none): no layer names.
    assert extract._tag_line_layers(lines[:1], layers[:1]) == [{"text": "answer"}]


def test_pages_keep_their_numbers(tmp_path):
    (tmp_path / "doc.content").write_text(json.dumps({"pages": ["a", "b", "c"]}))
    files = [Path("a.rm"), Path("c.rm")]
    pages = [[{"text": "first"}], [{"text": "third", "layers": ["Layer 2"]}]]

    by_page = extract.handwriting_by_page(tmp_path, files, pages)

    assert [entry["page"] for entry in by_page] == [1, 3]
    assert extract.ocr_page_text(by_page[1]) == "--- Page 3 ---\nthird  [Layer 2]"


def test_unreadable_pages_are_left_out(tmp_path):
    (tmp_path / "doc.content").write_text(json.dumps({"pages": ["a", "b"]}))
    by_page = extract.handwriting_by_page(
        tmp_path, [Path("a.rm"), Path("b.rm")], [None, [{"text": "x"}]]
    )
    assert by_page == [{"page": 2, "lines": [{"text": "x"}]}]


def test_markdown_export_puts_ocr_under_its_page(tmp_path):
    content = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": ["--- Page 2 ---\nx = 4  [Layer 2]"],
        "handwriting": [{"page": 2, "lines": [{"text": "x = 4", "layers": ["Layer 2"]}]}],
        "ocr_backend": "tesseract",
    }
    metadata = ExportMetadata(
        document_id="d",
        title="Sheet",
        path="/Sheet",
        source_type="notebook",
        page_count=2,
        modified=None,
        tags=(),
    )
    destination = tmp_path / "out.md"
    write_markdown_export(
        destination, metadata, source_text=None, extraction=content, include_ocr=True
    )
    markdown = destination.read_text()

    assert "### Page 2" in markdown and "x = 4  [Layer 2]" in markdown


def test_tesseract_found_outside_path(monkeypatch, tmp_path):
    import pytesseract

    program = tmp_path / "tesseract"
    program.write_text("")
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(extract, "TESSERACT_LOCATIONS", ("/missing", str(program)))

    assert extract.tesseract_module().pytesseract.tesseract_cmd == str(program)

"""Annotation extraction against files written by current tablet firmware.

The fixtures under fixtures/tablet/ are the .content and .rm files of two test
documents (the source PDFs are omitted):

- highlight_colors: page 1 is typeset PDF text, page 2 a scanned page with an
  OCR text layer; each is highlighted once per highlighter color.
- layers_and_tags: document tag "test-doc"; page 2 has freehand highlighter
  strokes in every color; page 3 has page tag "test-page" and three layers,
  with layer 3 hidden.
"""

import zipfile
from pathlib import Path

import pytest

from remarkable_mcp.extract import (
    _render_rm_v6_to_svg,
    extract_text_from_document_zip,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tablet"

PALETTE = ["yellow", "blue", "pink", "orange", "green", "gray"]


def _zip(name: str, tmp_path: Path) -> Path:
    root = FIXTURES / name
    zip_path = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root))
    return zip_path


@pytest.fixture
def highlight_colors(tmp_path):
    return extract_text_from_document_zip(_zip("highlight_colors", tmp_path))


@pytest.fixture
def layers_and_tags(tmp_path):
    return extract_text_from_document_zip(_zip("layers_and_tags", tmp_path))


@pytest.mark.parametrize("page", [1, 2])
def test_each_highlighter_color_reported_in_page_order(highlight_colors, page):
    details = [h for h in highlight_colors["highlight_details"] if h["page"] == page]

    # A highlight that wraps a line is stored as two ranges of the same color.
    colors = [h["color"] for h in details]
    assert list(dict.fromkeys(colors)) == PALETTE
    assert {h["color_hex"] for h in details if h["color"] == "blue"} == {"#beeafe"}
    assert {h["color_hex"] for h in details if h["color"] == "orange"} == {"#ffc38c"}
    assert {h["color_id"] for h in details if h["color"] in ("blue", "orange")} == {9}


def test_typeset_highlight_text(highlight_colors):
    first = highlight_colors["highlight_details"][0]
    assert first["page"] == 1
    assert first["text"].startswith("The Moon completes one sidereal revolution")


def test_freehand_highlighter_strokes(layers_and_tags):
    strokes = [h for h in layers_and_tags["highlighter_strokes"] if h["page"] == 2]

    assert [h["color"] for h in strokes] == [
        "pink",
        "yellow",
        "yellow",
        "blue",
        "pink",
        "orange",
        "green",
        "gray",
    ]
    # Swatches are drawn left of center, one under another, on a 157 mm page.
    for h in strokes[2:]:
        left, top, right, bottom = h["bbox_mm"]
        assert 0 < left < right < 80
        assert bottom - top > 2  # pen width included
        assert h["region"][0] < h["region"][2] < 0.5
    tops = [h["region"][1] for h in strokes]
    assert tops == sorted(tops)


def test_document_and_page_tags(layers_and_tags):
    assert [t["name"] for t in layers_and_tags["tags"]] == ["test-doc"]
    assert layers_and_tags["page_tags"] == [{"page": 3, "name": "test-page"}]
    page3 = next(ap for ap in layers_and_tags["annotated_pages"] if ap["page"] == 3)
    assert page3["page_tags"] == ["test-page"]


def test_layers_split_strokes_by_layer(layers_and_tags):
    page3 = next(ap for ap in layers_and_tags["annotated_pages"] if ap["page"] == 3)

    assert [(la["name"], la["visible"], la["pen_colors"]) for la in page3["layers"]] == [
        ("Layer 1", True, ["black", "blue"]),
        ("Layer 2", True, ["red"]),
        ("Layer 3", False, ["green"]),
    ]


def test_render_omits_hidden_layer():
    rm_file = next((FIXTURES / "layers_and_tags").rglob("36b60bbc-*.rm"))

    visible = _render_rm_v6_to_svg(rm_file)
    everything = _render_rm_v6_to_svg(rm_file, layers="all")

    assert 'stroke="#C23132"' in visible  # red, layer 2
    assert 'stroke="#91DA71"' not in visible  # green, hidden layer 3
    assert 'stroke="#91DA71"' in everything


def test_moved_and_deleted_layers(layers_and_tags):
    """Page 1 had layers added, moved and deleted; page 2 had extra layers deleted."""
    pages = {ap["page"]: ap for ap in layers_and_tags["annotated_pages"]}

    assert [la["name"] for la in pages[1]["layers"]] == ["Layer 1", "Layer 2", "Layer 3"]
    assert [h["layer"] for h in pages[1]["highlight_details"]] == ["Layer 2"]
    assert [la["name"] for la in pages[2]["layers"]] == ["Layer 1"]

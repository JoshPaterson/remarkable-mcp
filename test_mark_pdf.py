"""remarkable_mark_pdf: marks drawn into a document's PDF, kept removable."""

import base64
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest
from PIL import Image

from remarkable_mcp import pdf_marks, tablet_json
from remarkable_mcp.pages import pdf_page_map
from remarkable_mcp.pdf_marks import MarkError
from remarkable_mcp.server import mcp
from remarkable_mcp.sync import RemarkableClient, _hash_entries, _serialize_doc_index

DOC_ID = "4e93be85-f688-43ca-8ca2-f0f738301f13"
A4 = (595.0, 842.0)
DEFAULT_WIDTH = pdf_marks.DEFAULT_LINE_WIDTH


def _pdf(pages=2, text="Answer: ____") -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=A4[0], height=A4[1])
        page.insert_text((100, 200), text, fontsize=12)
    return doc.tobytes()


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (80, 40), (0, 90, 200)).save(out, "PNG")
    return out.getvalue()


def _one_page_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page(width=200, height=100).draw_rect((10, 10, 190, 90), color=(0, 0, 1))
    return doc.tobytes()


SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50">'
    '<path d="M5 25 L95 25" stroke="red" stroke-width="3"/></svg>'
)


def _edit(marks=(), add=(), remove=(), clear=False, page_map=(0, 1), color=None):
    return pdf_marks.edit_marks(
        list(marks),
        add=list(add),
        remove=list(remove),
        clear=clear,
        page_map=list(page_map),
        sizes=[A4, A4],
        color=color,
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- pdf_marks -------------------------------------------------------------


def test_marks_round_trip_and_clear_restores_original(tmp_path):
    original = _pdf()
    png_file = tmp_path / "plot.png"
    png_file.write_bytes(_png())
    result = _edit(
        add=[
            {"page": 1, "id": "score", "text": "3/4", "at": [0.8, 0.05], "size": 20},
            {"page": 1, "id": "q1", "shape": "check", "at": [0.4, 0.24]},
            {"page": 2, "id": "note", "text": "See me", "region": [0.1, 0.5, 0.6, 0.6]},
            {"page": 2, "shape": "circle", "region": [0.1, 0.1, 0.3, 0.2]},
            {"page": 2, "shape": "arrow", "from": [0.5, 0.3], "to": [0.3, 0.3]},
            {"page": 2, "latex": r"\frac{a}{b}", "at": [0.6, 0.1]},
            {"page": 2, "graphic": SVG, "region": [0.5, 0.7, 0.7, 0.8]},
            {"page": 2, "graphic": _b64(_png()), "region": [0.1, 0.8, 0.2, 0.85]},
            {"page": 2, "graphic": _b64(_one_page_pdf()), "region": [0.3, 0.8, 0.5, 0.9]},
            {"page": 2, "graphic_file": str(png_file), "region": [0.6, 0.9, 0.7, 0.95]},
        ]
    )
    assert result["added"][:3] == ["score", "q1", "note"]
    assert len(result["added"]) == 10
    marked = pdf_marks.build(original, result["marks"])

    base, marks = pdf_marks.load_state(marked)
    assert base == original
    assert [m["id"] for m in marks] == result["added"]
    assert [m["kind"] for m in marks][-4:] == ["graphic"] * 4
    assert [m["format"] for m in marks[-4:]] == ["svg", "png", "pdf", "png"]
    assert pdf_marks.page_sizes(marked) == pdf_marks.page_sizes(original)
    with pymupdf.open(stream=marked, filetype="pdf") as doc:
        assert "3/4" in doc[0].get_text()
        assert "See me" in doc[1].get_text()
    assert pdf_marks.original_pdf(marked) == original

    cleared = _edit(marks, clear=True)
    assert cleared["removed"] == result["added"]
    assert pdf_marks.build(base, cleared["marks"]) == original


def test_rebuild_reuses_stored_snippets():
    original = _pdf()
    first = _edit(add=[{"page": 1, "id": "a", "text": "keep", "at": [0.1, 0.1]}])
    base, marks = pdf_marks.load_state(pdf_marks.build(original, first["marks"]))
    second = _edit(
        marks, add=[{"page": 1, "id": "b", "shape": "box", "region": [0.2, 0.2, 0.3, 0.3]}]
    )
    assert second["marks"][0]["pdf"] == first["marks"][0]["pdf"]


def test_mark_lands_in_its_box():
    original = _pdf()
    result = _edit(add=[{"page": 1, "text": "HERE", "at": [0.5, 0.25], "size": 20}])
    box = result["marks"][0]["box"]
    assert box[0] == pytest.approx(0.5) and box[1] == pytest.approx(0.25)
    with pymupdf.open(stream=pdf_marks.build(original, result["marks"]), filetype="pdf") as doc:
        words = [w for w in doc[0].get_text("words") if w[4] == "HERE"]
    x0, y0, x1, y1 = words[0][:4]
    assert x0 == pytest.approx(0.5 * A4[0], abs=1.5)
    assert y0 == pytest.approx(0.25 * A4[1], abs=3)
    assert box[2] * A4[0] == pytest.approx(x1, abs=1.5)


def test_removed_mark_leaves_nothing_behind():
    original = _pdf()
    first = _edit(
        add=[
            {"page": 1, "id": "q3", "shape": "cross", "at": [0.4, 0.3]},
            {"page": 1, "id": "q3-note", "text": "Orange is secondary", "at": [0.45, 0.3]},
        ]
    )
    base, marks = pdf_marks.load_state(pdf_marks.build(original, first["marks"]))
    second = _edit(
        marks,
        remove=["q3", "q3-note"],
        add=[{"page": 1, "id": "q3", "shape": "check", "at": [0.4, 0.3]}],
    )
    assert second["removed"] == ["q3", "q3-note"] and second["added"] == ["q3"]
    rebuilt = pdf_marks.build(base, second["marks"])
    with pymupdf.open(stream=rebuilt, filetype="pdf") as doc:
        assert "Orange" not in doc[0].get_text()
    assert [m["shape"] for m in pdf_marks.load_state(rebuilt)[1]] == ["check"]


def test_default_colors():
    result = _edit(
        add=[
            {"page": 1, "shape": "check", "at": [0.1, 0.1]},
            {"page": 1, "shape": "cross", "at": [0.2, 0.1]},
            {"page": 1, "text": "x", "at": [0.3, 0.1]},
            {"page": 1, "latex": "x^2", "at": [0.5, 0.1]},
            {"page": 1, "path": "M 0 0 L 5 5", "region": [0.6, 0.1, 0.65, 0.15]},
            {"page": 1, "shape": "highlight", "region": [0.1, 0.2, 0.3, 0.22]},
            {"page": 1, "text": "4/4", "at": [0.4, 0.1], "color": "green"},
        ]
    )
    colors = [pdf_marks.summarize(m, 1)["color"] for m in result["marks"]]
    assert colors == ["black", "black", "black", "black", "black", "yellow", "green"]
    assert result["marks"][5]["color"] == pytest.approx(pdf_marks.HIGHLIGHT_COLORS["yellow"])
    red = _edit(add=[{"page": 1, "shape": "check", "at": [0.1, 0.1]}], color="red")
    assert pdf_marks.summarize(red["marks"][0], 1)["color"] == "red"


def test_named_colors_are_the_tablets():
    assert pdf_marks._hex(pdf_marks.COLORS["red"]) == "#ff0000"
    assert pdf_marks._hex(pdf_marks.HIGHLIGHT_COLORS["pink"]) == "#ff55cf"
    pink = _edit(
        add=[{"page": 1, "shape": "highlight", "region": [0.1, 0.1, 0.2, 0.2], "color": "pink"}]
    )
    assert pdf_marks.summarize(pink["marks"][0], 1)["color"] == "pink"
    # Pink is a highlighter color only; pens have no pink.
    with pytest.raises(MarkError, match="pen colors are"):
        _edit(add=[{"page": 1, "shape": "check", "at": [0.1, 0.1], "color": "pink"}])
    with pytest.raises(MarkError, match="highlighter colors are"):
        _edit(add=[{"page": 1, "shape": "highlight", "region": [0, 0, 1, 1], "color": "magenta"}])
    # A call-wide color names the matching shade for each mark.
    both = _edit(
        add=[
            {"page": 1, "shape": "check", "at": [0.1, 0.1]},
            {"page": 1, "shape": "highlight", "region": [0.1, 0.2, 0.3, 0.25]},
        ],
        color="green",
    )
    assert both["marks"][0]["color"] == pytest.approx(pdf_marks.COLORS["green"])
    assert both["marks"][1]["color"] == pytest.approx(pdf_marks.HIGHLIGHT_COLORS["green"])


@pytest.mark.parametrize("anchor", ["top-left", "center", "left", "bottom-right"])
def test_anchor_places_that_point_of_the_box_at(anchor):
    mark = _edit(add=[{"page": 1, "text": "Note", "at": [0.5, 0.5], "anchor": anchor}])
    left, top, right, bottom = mark["marks"][0]["box"]
    fx, fy = pdf_marks.ANCHORS[anchor]
    assert left + fx * (right - left) == pytest.approx(0.5, abs=1e-3)
    assert top + fy * (bottom - top) == pytest.approx(0.5, abs=1e-3)


def test_check_and_cross_are_centered_by_default():
    left, top, right, bottom = _edit(add=[{"page": 1, "shape": "check", "at": [0.5, 0.5]}])[
        "marks"
    ][0]["box"]
    assert (left + right) / 2 == pytest.approx(0.5, abs=1e-3)
    assert (top + bottom) / 2 == pytest.approx(0.5, abs=1e-3)


def test_highlight_darkens_and_keeps_text_dark():
    original = _pdf()
    region = [0.15, 0.2, 0.4, 0.25]
    result = _edit(add=[{"page": 1, "shape": "highlight", "region": region}])
    assert result["marks"][0]["box"] == region
    marked = pdf_marks.build(original, result["marks"])
    with pymupdf.open(stream=marked, filetype="pdf") as doc:
        pix = doc[0].get_pixmap(dpi=72)
    # Paper inside the highlight takes its color; text under it stays black.
    assert pix.pixel(int(0.38 * A4[0]), int(0.21 * A4[1])) == (254, 253, 96)
    text_pixels = [pix.pixel(x, 197) for x in range(100, 180)]
    assert min(sum(p) for p in text_pixels) < 60


def test_path_is_scaled_into_its_region_keeping_line_width():
    region = [0.2, 0.2, 0.6, 0.3]
    mark = _edit(add=[{"page": 1, "path": "M 0 0 L 2 0 L 2 1", "region": region, "width": 2}])[
        "marks"
    ][0]
    left, top, right, bottom = mark["box"]
    assert (top, bottom) == (0.2, 0.3)
    assert (left + right) / 2 == pytest.approx(0.4, abs=1e-3)
    # The path is twice as wide as tall; the region's height limits it.
    height_pt = (bottom - top) * A4[1]
    width_pt = (right - left) * A4[0]
    assert height_pt == pytest.approx(0.1 * A4[1], abs=0.1)
    assert width_pt - 2 == pytest.approx(2 * (height_pt - 2), abs=0.5)
    with pymupdf.open("pdf", base64.b64decode(mark["pdf"])) as snippet:
        widths = {round(d["width"], 2) for d in snippet[0].get_drawings()}
    assert widths == {2.0}


def test_path_units_do_not_matter():
    small = _edit(add=[{"page": 1, "path": "M 0 0 L 1 1", "region": [0.1, 0.1, 0.2, 0.2]}])
    large = _edit(add=[{"page": 1, "path": "M 0 0 L 500 500", "region": [0.1, 0.1, 0.2, 0.2]}])
    assert small["marks"][0]["box"] == large["marks"][0]["box"]


def test_listing_uses_regions_and_line_endpoints():
    result = _edit(
        add=[
            {"page": 1, "id": "hl", "shape": "highlight", "region": [0.1, 0.2, 0.3, 0.25]},
            {"page": 1, "id": "ln", "shape": "line", "from": [0.0, 0.5], "to": [1.0, 0.5]},
            {"page": 1, "id": "ck", "shape": "check", "region": [0.4, 0.4, 0.45, 0.43]},
        ]
    )
    hl, ln, ck = (pdf_marks.summarize(m, 1) for m in result["marks"])
    assert hl["region"] == [0.1, 0.2, 0.3, 0.25]
    assert ln["from"] == [0.0, 0.5] and ln["to"] == [1.0, 0.5] and "region" not in ln
    left, top, right, bottom = ck["region"]
    assert (left + right) / 2 == pytest.approx(0.425, abs=1e-3)
    assert (top + bottom) / 2 == pytest.approx(0.415, abs=1e-3)


def test_path_with_fill():
    mark = _edit(
        add=[
            {
                "page": 1,
                "path": "M 0 0 L 10 0 L 5 8 Z",
                "region": [0.5, 0.5, 0.6, 0.6],
                "fill": "yellow",
            }
        ]
    )["marks"][0]
    with pymupdf.open("pdf", base64.b64decode(mark["pdf"])) as snippet:
        fills = [d["fill"] for d in snippet[0].get_drawings() if d.get("fill")]
    assert fills and fills[0] == pytest.approx(pdf_marks.COLORS["yellow"], abs=0.01)


def test_latex_renders_math_as_vectors():
    result = _edit(
        add=[{"page": 1, "latex": r"x = \frac{-b \pm \sqrt{b^2-4ac}}{2a}", "at": [0.1, 0.1]}]
    )
    mark = result["marks"][0]
    with pymupdf.open("pdf", base64.b64decode(mark["pdf"])) as snippet:
        assert snippet[0].get_drawings()
        assert not snippet[0].get_images()
    left, top, right, bottom = mark["box"]
    assert right - left > 0.15 and bottom - top > 0.02


def test_graphic_fits_rect_keeping_aspect():
    result = _edit(add=[{"page": 1, "graphic": SVG, "region": [0.1, 0.1, 0.9, 0.9]}])
    left, top, right, bottom = result["marks"][0]["box"]
    assert (left, top, right) == (0.1, 0.1, 0.9)
    assert (right - left) * A4[0] / ((bottom - top) * A4[1]) == pytest.approx(2.0, rel=0.01)


def test_marks_follow_pdf_pages_not_tablet_positions():
    # Tablet page 2 was added on the tablet; tablet page 3 shows PDF page 1.
    result = _edit(
        add=[{"page": 3, "id": "a", "shape": "check", "at": [0.5, 0.5]}], page_map=[0, None, 1]
    )
    assert result["marks"][0]["pdf_page"] == 1
    assert pdf_marks.tablet_page(1, [1, 0]) == 1
    with pytest.raises(MarkError, match="added on the tablet"):
        _edit(add=[{"page": 2, "shape": "check", "at": [0.5, 0.5]}], page_map=[0, None, 1])


@pytest.mark.parametrize(
    "mark, message",
    [
        ({"page": 1, "shape": "check", "at": [1.2, 0.5]}, "from 0 to 1"),
        ({"page": 1, "shape": "star", "at": [0.5, 0.5]}, "shape must be one of"),
        ({"page": 1, "shape": "line", "from": [0.1, 0.1], "to": [0.1, 0.1]}, "different"),
        ({"page": 1, "shape": "circle", "region": [0.5, 0.5, 0.4, 0.6]}, "left < right"),
        ({"page": 1, "text": "x"}, "either"),
        ({"page": 1, "text": "x", "at": [0.1, 0.1], "region": [0, 0, 1, 1]}, "either"),
        ({"page": 1, "text": "far too long for the edge", "at": [0.95, 0.1]}, "runs off"),
        ({"page": 1, "text": "long " * 200, "region": [0.1, 0.1, 0.2, 0.12]}, "does not fit"),
        ({"page": 1, "text": "x", "latex": "y", "at": [0.1, 0.1]}, "exactly one"),
        ({"page": 1, "text": "x", "at": [0.1, 0.1], "color": "chartreuse"}, "Unknown color"),
        ({"page": 1, "shape": "highlight", "at": [0.1, 0.1]}, "needs a region"),
        ({"page": 1, "graphic": SVG, "region": [0, 0, 1, 1], "color": "red"}, "does not apply"),
        ({"page": 1, "graphic": "not base64!", "region": [0, 0, 1, 1]}, "base64"),
        ({"page": 1, "graphic": _b64(b"GIF89a...."), "region": [0, 0, 1, 1]}, "SVG, PDF, PNG"),
        ({"page": 1, "graphic": _b64(_pdf(2)), "region": [0, 0, 1, 1]}, "exactly one page"),
        ({"page": 1, "graphic": SVG}, "region"),
        ({"page": 1, "graphic_file": "/nonexistent/plot.svg", "region": [0, 0, 1, 1]}, "not found"),
        ({"page": 3, "shape": "check", "at": [0.1, 0.1]}, "does not exist"),
        ({"page": 1, "text": "x", "region": [0, 0, 1, 1], "anchor": "center"}, "only to marks"),
        ({"page": 1, "text": "x", "at": [0.5, 0.5], "anchor": "middle"}, "anchor must be"),
        ({"page": 1, "shape": "highlight", "region": [0, 0, 1, 1], "opacity": 2}, "opacity"),
        ({"page": 1, "path": '"/><image href="x', "region": [0, 0, 1, 1]}, "SVG path data"),
        ({"page": 1, "path": "M 0 0", "region": [0, 0, 1, 1]}, "draws nothing"),
        (
            {"page": 1, "path": "M 0 0 L 9 0", "region": [0.5, 0.5, 0.501, 0.501], "width": 2},
            "too small",
        ),
        ({"page": 1, "path": "M 0 0 L 5 5"}, "region"),
        (
            {"page": 1, "shape": "check", "region": [0.1, 0.1, 0.2, 0.2], "size": 5},
            "size applies only",
        ),
        ({"shape": "check", "at": [0.1, 0.1]}, "page number"),
        ({"page": 1, "id": "bad id!", "shape": "check", "at": [0.1, 0.1]}, "Mark ids"),
    ],
)
def test_invalid_marks(mark, message):
    with pytest.raises(MarkError, match=message):
        _edit(add=[mark])


def test_graphic_file_must_be_a_graphic(tmp_path):
    secret = tmp_path / "notes.txt"
    secret.write_text("private")
    with pytest.raises(MarkError, match="SVG, PDF, PNG or JPEG"):
        _edit(add=[{"page": 1, "graphic_file": str(secret), "region": [0, 0, 1, 1]}])


def test_duplicate_id_needs_removal_first():
    first = _edit(add=[{"page": 1, "id": "q1", "shape": "check", "at": [0.1, 0.1]}])
    with pytest.raises(MarkError, match="already used"):
        _edit(first["marks"], add=[{"page": 1, "id": "q1", "shape": "cross", "at": [0.1, 0.1]}])


@pytest.mark.parametrize(
    "svg",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink">'
        '<image x:href="secret.png"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(/etc/x)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "x.css";</style></svg>',
        '<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>',
        "<svg",
        "<html/>",
    ],
)
def test_svg_with_outside_references_is_refused(svg):
    with pytest.raises(MarkError):
        pdf_marks.check_svg(svg)


def test_svg_internal_and_data_references_are_allowed():
    pdf_marks.check_svg(
        '<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs>'
        '<rect style="fill:url(#g)"/><image href="data:image/png;base64,AAAA"/></svg>'
    )


def test_unmarked_pdf_has_no_marks():
    original = _pdf()
    assert pdf_marks.load_state(original) == (original, [])
    assert pdf_marks.stored_marks(b"not a pdf") == []
    assert pdf_marks.original_pdf(b"not a pdf") == b"not a pdf"


def test_older_mark_format_must_be_cleared():
    original = _pdf()
    doc = pymupdf.open(stream=original, filetype="pdf")
    doc.embfile_add(pdf_marks.BASE_FILE, original)
    doc.embfile_add(pdf_marks.MARKS_FILE, json.dumps({"format": 1, "marks": []}).encode())
    old = doc.tobytes()
    with pytest.raises(MarkError, match="older version"):
        pdf_marks.load_state(old)
    assert pdf_marks.original_pdf(old) == original


def test_text_lines_are_page_fractions():
    lines = pdf_marks.text_lines(_pdf(), 0)
    assert lines[0]["text"] == "Answer: ____"
    left, top, right, bottom = lines[0]["region"]
    assert left == pytest.approx(100 / 595, abs=0.005)
    assert top < 200 / 842 < bottom


def test_pdf_page_map_layouts():
    assert pdf_page_map({"pages": ["a", "b"], "redirectionPageMap": [0, -1]}) == [0, None]
    assert pdf_page_map({"pages": ["a", "b"]}) == [0, 1]
    cpages = {
        "cPages": {
            "pages": [
                {"id": "a", "redir": {"value": 1}},
                {"id": "b"},
                {"id": "c", "redir": {"value": 0}, "deleted": {"value": 1}},
                {"id": "d", "redir": {"value": 0}},
            ]
        }
    }
    assert pdf_page_map(cpages) == [1, None, 0]


# --- cloud tool ------------------------------------------------------------


CONTENT = {"fileType": "pdf", "pages": ["page-a", "page-b"], "redirectionPageMap": [0, 1]}
METADATA = tablet_json.dumps({"lastModified": "1", "version": 2, "visibleName": "Worksheet"})


class FakeCloud(RemarkableClient):
    """Real sync client logic over an in-memory blob store and root."""

    def __init__(self, pdf: bytes, content=CONTENT):
        super().__init__()
        self.blobs = {}
        self.commits = 0
        files = [
            self._blob(tablet_json.dumps(content).encode(), f"{DOC_ID}.content"),
            self._blob(METADATA.encode(), f"{DOC_ID}.metadata"),
            self._blob(pdf, f"{DOC_ID}.pdf"),
        ]
        doc_hash = _hash_entries(files)
        self.blobs[doc_hash] = _serialize_doc_index(files)
        self.entries = [{"hash": doc_hash, "type": "0", "id": DOC_ID, "subfiles": 3, "size": 1}]
        self.generation = 1

    def _blob(self, data: bytes, name: str) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        self.blobs[digest] = data
        return {"hash": digest, "type": "0", "id": name, "subfiles": 0, "size": len(data)}

    def get_root(self):
        from remarkable_mcp.sync import _serialize_root_index

        body = _serialize_root_index(self.entries)
        digest = hashlib.sha256(body).hexdigest()
        self.blobs[digest] = body
        return digest, self.generation

    def _get_file(self, file_hash, file_name):
        return self.blobs[file_hash]

    def _put_blob(self, content, blob_hash, filename, content_type="application/octet-stream"):
        self.blobs[blob_hash] = content

    def _commit_root(self, root_hash, generation, broadcast=True):
        self.entries = self._parse_index(self.blobs[root_hash])
        self.generation += 1
        self.commits += 1
        return self.generation

    def get_meta_items(self, *args, **kwargs):
        doc = SimpleNamespace(ID=DOC_ID, VissibleName="Worksheet", Parent="", is_folder=False)
        folder = SimpleNamespace(ID="folder", VissibleName="Course", Parent="", is_folder=True)
        return [doc, folder]

    def stored(self, suffix: str) -> bytes:
        index = self._parse_index(self.blobs[self.entries[0]["hash"]])
        entry = next(f for f in index if f["id"].endswith(suffix))
        return self.blobs[entry["hash"]]


@pytest.fixture
def original():
    return _pdf()


@pytest.fixture
def cloud(monkeypatch, original):
    from remarkable_mcp.write_tools import register_write_tools

    for var in (
        "REMARKABLE_USE_SSH",
        "REMARKABLE_USE_USB_WEB",
        "REMARKABLE_READ_ONLY",
        "REMARKABLE_USE_LOCAL_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    tools = mcp._tool_manager._tools
    saved = dict(tools)
    register_write_tools()
    fake = FakeCloud(original)
    try:
        with patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake):
            yield fake
    finally:
        tools.clear()
        tools.update(saved)


async def _mark_tool(**arguments):
    result = await mcp.call_tool("remarkable_mark_pdf", arguments)
    return json.loads(result.content[0].text), result.content[1:]


async def test_cloud_add_remove_and_clear(cloud, original):
    content_before = cloud.stored(".content")
    data, _ = await _mark_tool(
        document="Worksheet",
        add=[
            {"page": 2, "id": "q1", "shape": "cross", "at": [0.4, 0.3]},
            {"page": 2, "id": "q1-note", "text": "Check the units", "at": [0.45, 0.3]},
        ],
    )
    assert data["added"] == ["q1", "q1-note"] and data["changed"]
    assert [m["page"] for m in data["marks"]] == [2, 2]
    assert data["marks"][0]["color"] == "black"
    assert cloud.commits == 1
    assert cloud.stored(".content") == content_before
    meta = json.loads(cloud.stored(".metadata"))
    assert meta["lastModified"] != "1" and meta["version"] == 2
    assert pdf_marks.original_pdf(cloud.stored(".pdf")) == original

    data, _ = await _mark_tool(document="Worksheet", remove="q1-note")
    assert data["removed"] == ["q1-note"]
    assert [m["id"] for m in data["marks"]] == ["q1"]

    data, _ = await _mark_tool(document="Worksheet", clear=True)
    assert data["removed"] == ["q1"]
    assert cloud.stored(".pdf") == original
    assert cloud.commits == 3


async def test_cloud_dry_run_writes_nothing_and_returns_preview(cloud, original):
    with patch(
        "remarkable_mcp.write_tools._mark_preview_images", return_value=["image"]
    ) as preview:
        data, images = await _mark_tool(
            document="Worksheet",
            add=[{"page": 1, "shape": "check", "at": [0.5, 0.5]}],
            dry_run=True,
        )
    assert data["dry_run"] and data["added"] == ["mark-1"]
    assert data["preview_pages"] == [1]
    assert preview.call_args.args[3] == [1]
    assert cloud.commits == 0
    assert cloud.stored(".pdf") == original


async def test_cloud_listing_and_layout(cloud):
    await _mark_tool(
        document="Worksheet", add=[{"page": 1, "id": "s", "text": "1/2", "at": [0.8, 0.05]}]
    )
    with patch("remarkable_mcp.extract.page_ink_regions", return_value=[[0.1, 0.2, 0.3, 0.25]]):
        data, _ = await _mark_tool(document="Worksheet", layout=1)
    assert [m["id"] for m in data["marks"]] == ["s"] and not data["changed"]
    assert data["layout"]["ink"] == [[0.1, 0.2, 0.3, 0.25]]
    # Text lines come from the PDF before marks.
    assert [line["text"] for line in data["layout"]["text_lines"]] == ["Answer: ____"]
    assert cloud.commits == 1


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"add": [{"page": 1, "shape": "check", "at": [2, 0]}]}, "from 0 to 1"),
        ({"add": [{"page": 1, "shape": "check", "at": [0.1, 0.1]}], "layout": 1}, "layout cannot"),
        ({"layout": 5}, "does not exist"),
    ],
)
async def test_cloud_rejects_bad_requests(cloud, arguments, message):
    data, _ = await _mark_tool(document="Worksheet", **arguments)
    assert data["_error"]["type"] == "invalid_parameter"
    assert message in data["_error"]["message"]
    assert cloud.commits == 0


async def test_cloud_refuses_non_pdf_documents(monkeypatch, cloud):
    notebook = FakeCloud(_pdf(), content={"fileType": "notebook", "pages": ["a"]})
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=notebook):
        data, _ = await _mark_tool(
            document="Worksheet", add=[{"page": 1, "shape": "check", "at": [0.1, 0.1]}]
        )
    assert data["_error"]["type"] == "mark_failed"
    assert "not a PDF" in data["_error"]["message"]
    assert notebook.commits == 0


def test_archive_without_marks_restores_original_pdf(original):
    from remarkable_mcp.tools import _archive_without_marks

    marks = _edit(add=[{"page": 1, "shape": "check", "at": [0.5, 0.5]}])["marks"]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{DOC_ID}.pdf", pdf_marks.build(original, marks))
        zf.writestr(f"{DOC_ID}.content", json.dumps(CONTENT))
    with zipfile.ZipFile(io.BytesIO(_archive_without_marks(archive.getvalue()))) as zf:
        assert zf.read(f"{DOC_ID}.pdf") == original
        assert json.loads(zf.read(f"{DOC_ID}.content")) == CONTENT


def test_text_styles():
    mark = _edit(
        add=[
            {
                "page": 1,
                "text": "Styled note",
                "at": [0.1, 0.1],
                "bold": True,
                "italic": True,
                "underline": True,
            }
        ]
    )["marks"][0]
    assert (mark["bold"], mark["italic"], mark["underline"]) == (True, True, True)
    with pymupdf.open("pdf", base64.b64decode(mark["pdf"])) as snippet:
        fonts = {
            span["font"]
            for b in snippet[0].get_text("dict")["blocks"]
            for line in b["lines"]
            for span in line["spans"]
        }
        assert any("BoldOblique" in f for f in fonts)
        assert len(snippet[0].get_drawings()) == 1
    plain = _edit(add=[{"page": 1, "text": "Plain", "at": [0.1, 0.1]}])["marks"][0]
    assert "bold" not in plain and "underline" not in plain


def test_svg_path_parse_and_transform():
    from remarkable_mcp import svg_path

    commands = svg_path.parse("M0,0 10 0 l5 5 H 20 v-5 a5 5 0 1010 10 Z")
    assert [c for c, _ in commands] == ["M", "L", "l", "H", "v", "a", "Z"]
    assert commands[5][1] == [5, 5, 0, 1, 0, 10, 10]
    moved = svg_path.parse(svg_path.transform(commands, 2, 3, 100, 200))
    assert moved[0][1] == [100, 200]
    assert moved[1][1] == [120, 200]
    assert moved[2][1] == [10, 15]
    assert moved[3][1] == [140]
    assert moved[5][1] == [10, 15, 0, 1, 0, 20, 30]
    with pytest.raises(svg_path.PathError):
        svg_path.parse("10 10 L 5")


def test_stretched_shape_fills_region_with_exact_line_width():
    region = [0.1, 0.1, 0.5, 0.15]
    mark = _edit(add=[{"page": 1, "shape": "circle", "region": region, "width": 2}])["marks"][0]
    assert mark["box"] == region
    with pymupdf.open("pdf", base64.b64decode(mark["pdf"])) as snippet:
        drawings = snippet[0].get_drawings()
        assert {round(d["width"], 2) for d in drawings} == {2.0}
        bounds = drawings[0]["rect"]
        for d in drawings[1:]:
            bounds |= d["rect"]
        assert bounds.width == pytest.approx(0.4 * A4[0] - 2, abs=0.5)
        assert bounds.height == pytest.approx(0.05 * A4[1] - 2, abs=0.5)


def test_path_stretch_and_at_placement():
    stretched = _edit(
        add=[{"page": 1, "path": "M 0 0 L 1 1", "region": [0.1, 0.1, 0.5, 0.2], "stretch": True}]
    )["marks"][0]
    assert stretched["box"] == [0.1, 0.1, 0.5, 0.2]
    placed = _edit(add=[{"page": 1, "path": "M 0 0 L 2 1", "at": [0.5, 0.5], "size": 20}])["marks"][
        0
    ]
    left, top, right, bottom = placed["box"]
    assert (right - left) * A4[0] == pytest.approx(20 + DEFAULT_WIDTH, abs=0.1)
    assert (left + right) / 2 == pytest.approx(0.5, abs=1e-3)


def test_description_is_stored_and_listed():
    from remarkable_mcp.tools import _mark_line

    result = _edit(
        add=[
            {
                "page": 1,
                "id": "q5",
                "path": "M 0 0 L 10 0 L 5 8 Z",
                "at": [0.5, 0.5],
                "description": "gold star:\n  neat, complete working",
            }
        ]
    )
    summary = pdf_marks.summarize(result["marks"][0], 1)
    assert summary["description"] == "gold star: neat, complete working"
    assert _mark_line(summary).endswith("[description: gold star: neat, complete working]")
    with pytest.raises(MarkError, match="at most 200"):
        _edit(add=[{"page": 1, "text": "x", "at": [0.1, 0.1], "description": "d" * 201}])


def test_rendered_highlighter_darkens_paper_and_keeps_ink_dark():
    from PIL import Image

    from remarkable_mcp.extract import HIGHLIGHT_BLEND, _svg_string_to_png

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="40" viewBox="0 0 100 40">'
        '<rect x="10" y="10" width="20" height="20" fill="#000000"/>'
        f'<rect x="0" y="15" width="100" height="10" fill="#FEFD60" {HIGHLIGHT_BLEND}/>'
        "</svg>"
    )
    png = _svg_string_to_png(svg, 100, 40, "#FFFFFF")
    image = Image.open(io.BytesIO(png)).convert("RGB")
    assert image.getpixel((60, 20)) == (254, 253, 96)  # highlighter on paper
    assert image.getpixel((20, 20)) == (0, 0, 0)  # ink under the highlighter
    assert image.getpixel((60, 5)) == (255, 255, 255)  # paper

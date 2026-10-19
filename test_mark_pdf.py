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
RED = pdf_marks.parse_color("red")


def _pdf(pages=2, text="Answer: ____") -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((100, 200), text, fontsize=12)
    return doc.tobytes()


def _png() -> str:
    out = io.BytesIO()
    Image.new("RGB", (8, 4), (0, 90, 200)).save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    '<path d="M1 5 L9 5" stroke="red"/></svg>'
)


def _edit(marks=(), add=(), remove=(), clear=False, page_map=(0, 1), color=None):
    return pdf_marks.edit_marks(
        list(marks),
        add=list(add),
        remove=list(remove),
        clear=clear,
        page_map=list(page_map),
        default_color=color,
        max_image_bytes=pdf_marks.MAX_IMAGE_BYTES,
    )


# --- pdf_marks -------------------------------------------------------------


def test_marks_round_trip_and_clear_restores_original():
    original = _pdf()
    result = _edit(
        add=[
            {"page": 1, "id": "score", "text": "3/4", "at": [0.8, 0.05], "size": 20},
            {"page": 1, "id": "q1", "check": [0.4, 0.24]},
            {"page": 2, "id": "note", "text": "See me", "rect": [0.1, 0.5, 0.6, 0.6]},
            {"page": 2, "circle": [0.1, 0.1, 0.3, 0.2]},
            {"page": 2, "svg": SVG, "rect": [0.5, 0.7, 0.7, 0.8]},
            {"page": 2, "image": _png(), "rect": [0.1, 0.8, 0.2, 0.85]},
        ]
    )
    assert result["added"] == ["score", "q1", "note", "mark-1", "mark-2", "mark-3"]
    marked = pdf_marks.build(original, result["marks"])

    base, marks = pdf_marks.load_state(marked)
    assert base == original
    assert [m["id"] for m in marks] == result["added"]
    assert pdf_marks.page_sizes(marked) == pdf_marks.page_sizes(original)
    with pymupdf.open(stream=marked, filetype="pdf") as doc:
        assert "3/4" in doc[0].get_text()
        assert "See me" in doc[1].get_text()
    assert pdf_marks.original_pdf(marked) == original

    cleared = _edit(marks, clear=True)
    assert cleared["removed"] == result["added"]
    assert pdf_marks.build(base, cleared["marks"]) == original


def test_removed_mark_leaves_nothing_behind():
    original = _pdf()
    first = _edit(
        add=[
            {"page": 1, "id": "q3", "cross": [0.4, 0.3]},
            {"page": 1, "id": "q3-note", "text": "Orange is secondary", "at": [0.45, 0.3]},
        ]
    )
    marked = pdf_marks.build(original, first["marks"])
    base, marks = pdf_marks.load_state(marked)
    second = _edit(
        marks,
        remove=["q3", "q3-note"],
        add=[{"page": 1, "id": "q3", "check": [0.4, 0.3]}],
    )
    assert second["removed"] == ["q3", "q3-note"] and second["added"] == ["q3"]
    rebuilt = pdf_marks.build(base, second["marks"])
    with pymupdf.open(stream=rebuilt, filetype="pdf") as doc:
        assert "Orange" not in doc[0].get_text()
    assert [m["kind"] for m in pdf_marks.load_state(rebuilt)[1]] == ["check"]


def test_default_colors_by_kind():
    result = _edit(
        add=[
            {"page": 1, "check": [0.1, 0.1]},
            {"page": 1, "cross": [0.2, 0.1]},
            {"page": 1, "text": "x", "at": [0.3, 0.1]},
            {"page": 1, "text": "4/4", "at": [0.4, 0.1], "color": "green"},
        ]
    )
    colors = [pdf_marks.color_name(m["color"]) for m in result["marks"]]
    assert colors == ["green", "red", "red", "green"]
    blue = _edit(add=[{"page": 1, "check": [0.1, 0.1]}], color=pdf_marks.parse_color("blue"))
    assert pdf_marks.color_name(blue["marks"][0]["color"]) == "blue"


def test_marks_follow_pdf_pages_not_tablet_positions():
    # Tablet page 2 was added on the tablet; tablet page 3 shows PDF page 1.
    result = _edit(add=[{"page": 3, "id": "a", "check": [0.5, 0.5]}], page_map=[0, None, 1])
    mark = result["marks"][0]
    assert mark["pdf_page"] == 1
    assert pdf_marks.tablet_page(1, [1, 0]) == 1
    with pytest.raises(MarkError, match="added on the tablet"):
        _edit(add=[{"page": 2, "check": [0.5, 0.5]}], page_map=[0, None, 1])


@pytest.mark.parametrize(
    "mark, message",
    [
        ({"page": 1, "check": [1.2, 0.5]}, "from 0 to 1"),
        ({"page": 1, "text": "x"}, "at"),
        ({"page": 1, "text": "x", "at": [0.1, 0.1], "rect": [0, 0, 1, 1]}, "either"),
        ({"page": 1, "circle": [0.5, 0.5, 0.4, 0.6]}, "left < right"),
        ({"page": 1, "check": [0.1, 0.1], "cross": [0.1, 0.1]}, "exactly one"),
        ({"page": 1, "check": [0.1, 0.1], "color": "chartreuse"}, "Unknown color"),
        ({"page": 1, "svg": SVG, "rect": [0, 0, 1, 1], "color": "red"}, "does not apply"),
        ({"page": 1, "image": "not base64!", "rect": [0, 0, 1, 1]}, "base64"),
        ({"page": 1, "image": base64.b64encode(b"GIF89a").decode(), "rect": [0, 0, 1, 1]}, "PNG"),
        ({"page": 3, "check": [0.1, 0.1]}, "does not exist"),
        ({"check": [0.1, 0.1]}, "page number"),
        ({"page": 1, "id": "bad id!", "check": [0.1, 0.1]}, "Mark ids"),
    ],
)
def test_invalid_marks(mark, message):
    with pytest.raises(MarkError, match=message):
        _edit(add=[mark])


def test_duplicate_id_needs_removal_first():
    first = _edit(add=[{"page": 1, "id": "q1", "check": [0.1, 0.1]}])
    with pytest.raises(MarkError, match="already used"):
        _edit(first["marks"], add=[{"page": 1, "id": "q1", "cross": [0.1, 0.1]}])


def test_text_that_does_not_fit_is_refused():
    result = _edit(add=[{"page": 1, "text": "long " * 200, "rect": [0.1, 0.1, 0.2, 0.12]}])
    with pytest.raises(MarkError, match="does not fit"):
        pdf_marks.build(_pdf(), result["marks"])


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


def test_text_lines_are_page_fractions():
    lines = pdf_marks.text_lines(_pdf(), 0)
    assert lines[0]["text"] == "Answer: ____"
    left, top, right, bottom = lines[0]["box"]
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
            {"page": 2, "id": "q1", "cross": [0.4, 0.3]},
            {"page": 2, "id": "q1-note", "text": "Check the units", "at": [0.45, 0.3]},
        ],
    )
    assert data["added"] == ["q1", "q1-note"] and data["changed"]
    assert [m["page"] for m in data["marks"]] == [2, 2]
    assert data["marks"][0]["color"] == "red"
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
            document="Worksheet", add=[{"page": 1, "check": [0.5, 0.5]}], dry_run=True
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
        ({"add": [{"page": 1, "check": [2, 0]}]}, "from 0 to 1"),
        ({"add": [{"page": 1, "check": [0.1, 0.1]}], "layout": 1}, "layout cannot"),
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
        data, _ = await _mark_tool(document="Worksheet", add=[{"page": 1, "check": [0.1, 0.1]}])
    assert data["_error"]["type"] == "mark_failed"
    assert "not a PDF" in data["_error"]["message"]
    assert notebook.commits == 0


def test_archive_without_marks_restores_original_pdf(original):
    from remarkable_mcp.tools import _archive_without_marks

    marks = _edit(add=[{"page": 1, "check": [0.5, 0.5]}])["marks"]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{DOC_ID}.pdf", pdf_marks.build(original, marks))
        zf.writestr(f"{DOC_ID}.content", json.dumps(CONTENT))
    with zipfile.ZipFile(io.BytesIO(_archive_without_marks(archive.getvalue()))) as zf:
        assert zf.read(f"{DOC_ID}.pdf") == original
        assert json.loads(zf.read(f"{DOC_ID}.content")) == CONTENT

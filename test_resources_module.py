"""Document resources: registration, root filtering, and reading each kind."""

import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pymupdf
import pytest
from mcp.types import ResourceTemplateReference

from remarkable_mcp import notebooks, resources
from remarkable_mcp.server import mcp


def _doc(doc_id, name, parent="", folder=False):
    return SimpleNamespace(
        ID=doc_id, VissibleName=name, Parent=parent, is_folder=folder, ModifiedClient=None
    )


def _notebook(text="Typed resource text", pages=2) -> bytes:
    out = io.BytesIO()
    page_ids = [f"page-{i}" for i in range(pages)]
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            "nb.content",
            json.dumps(
                {"fileType": "notebook", "cPages": {"pages": [{"id": p} for p in page_ids]}}
            ),
        )
        archive.writestr(f"{page_ids[0]}.rm", notebooks.page_rm_bytes(text))
    return out.getvalue()


def _pdf(text="Raw PDF words") -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


@pytest.fixture
def registry(monkeypatch):
    """A clean resource registry, restored afterwards."""
    manager = mcp._resource_manager
    saved = (dict(manager._resources), dict(manager._templates))
    state = {
        name: getattr(resources, name)
        for name in (
            "_registered_docs",
            "_registered_raw",
            "_registered_img",
            "_registered_uris",
            "_img_uri_to_doc",
        )
    }
    for name, value in state.items():
        monkeypatch.setattr(resources, name, type(value)())
    monkeypatch.delenv("REMARKABLE_ROOT_PATH", raising=False)
    try:
        yield manager
    finally:
        manager._resources.clear()
        manager._resources.update(saved[0])
        manager._templates.clear()
        manager._templates.update(saved[1])


@pytest.fixture
def client():
    fake = Mock()
    fake.download.side_effect = lambda doc: _notebook() if doc.ID.startswith("nb") else b""
    return fake


async def _read(uri: str):
    (content,) = await mcp.read_resource(uri)
    return content.content


async def test_notebook_gets_text_png_and_svg_resources(registry, client):
    doc = _doc("nb-1", "Journal")
    assert resources._register_document(client, doc, file_types={"nb-1": "notebook"})

    assert "Typed resource text" in await _read("remarkable:///Journal.txt")
    png = await _read("remarkableimg:///Journal.page-1.png")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    svg = await _read("remarkablesvg:///Journal.page-1.svg")
    assert "<svg" in svg
    with pytest.raises(Exception, match="page-0"):
        await _read("remarkableimg:///Journal.page-0.png")


async def test_pdf_gets_a_raw_text_resource(registry, client):
    doc = _doc("pdf-1", "Paper")
    raw = Mock(side_effect=[_pdf(), b""])
    with patch("remarkable_mcp.api.download_raw_file", raw):
        assert resources._register_document(client, doc, file_types={"pdf-1": "pdf"})
    assert "Raw PDF words" in await _read("remarkableraw:///Paper.pdf.txt")
    assert "not found" in await _read("remarkableraw:///Paper.pdf.txt")
    assert not any(uri.startswith("remarkableimg") for uri in resources._registered_uris)


async def test_same_names_get_numbered_uris(registry, client):
    resources._register_document(client, _doc("nb-1", "Notes"), file_types={"nb-1": "notebook"})
    resources._register_document(client, _doc("nb-2", "Notes"), file_types={"nb-2": "notebook"})
    assert {"remarkable:///Notes.txt", "remarkable:///Notes_1.txt"} <= resources._registered_uris
    assert not resources._register_document(client, _doc("nb-1", "Notes"))  # once per id


async def test_root_path_and_trash_filter_registration(registry, client, monkeypatch):
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")
    work = _doc("f-work", "Work", folder=True)
    inside = _doc("nb-in", "Plan", parent="f-work")
    outside = _doc("nb-out", "Diary")
    trashed = _doc("nb-trash", "Old", parent="trash")
    items = {d.ID: d for d in (work, inside, outside, trashed)}
    root = resources._get_root_path()
    types = {"nb-in": "notebook", "nb-out": "notebook", "nb-trash": "notebook"}

    assert resources._register_document(client, inside, items, types, root=root)
    assert not resources._register_document(client, outside, items, types, root=root)
    assert not resources._register_document(client, trashed, items, types, root=root)
    assert "remarkable:///Plan.txt" in resources._registered_uris  # /Work is the root


async def test_text_resource_falls_back_to_ocr_and_reports_errors(registry):
    client = Mock()
    client.download.return_value = _notebook(text="")
    resources._register_document(
        client, _doc("nb-ocr", "Sketch"), file_types={"nb-ocr": "notebook"}
    )
    with patch(
        "remarkable_mcp.extract.extract_handwriting_ocr", return_value=(["handwritten words"], "x")
    ):
        assert "handwritten words" in await _read("remarkable:///Sketch.txt")
    client.download.side_effect = RuntimeError("offline")
    assert (await _read("remarkable:///Sketch.txt")).startswith("Error: offline")


async def test_page_completion_lists_the_notebooks_pages(registry, client):
    client.download.side_effect = lambda doc: _notebook(pages=12)
    resources._register_document(client, _doc("nb-1", "Long"), file_types={"nb-1": "notebook"})
    ref = ResourceTemplateReference(
        type="ref/resource", uri="remarkableimg:///Long.page-{page}.png"
    )

    result = await resources.handle_completion(ref, SimpleNamespace(name="page", value=""), None)
    assert result.values == [str(i) for i in range(1, 11)] and result.has_more
    result = await resources.handle_completion(ref, SimpleNamespace(name="page", value="1"), None)
    assert result.values == ["1", "10", "11", "12"]


def test_load_all_documents_registers_each_once(registry, monkeypatch):
    client = Mock()
    docs = [_doc("nb-1", "A"), _doc("pdf-1", "B"), _doc("f-1", "Folder", folder=True)]
    client.get_meta_items.return_value = docs
    client.get_all_file_types.return_value = {"nb-1": "notebook", "pdf-1": "pdf"}
    monkeypatch.setattr("remarkable_mcp.api.get_rmapi", lambda: client)

    assert resources.load_all_documents_sync() == 2
    assert resources._registered_raw == {"pdf-1"} and resources._registered_img == {"nb-1"}

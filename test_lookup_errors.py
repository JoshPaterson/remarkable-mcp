"""One document lookup and one set of lookup and parameter errors for every tool."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mcp import Client

from remarkable_mcp.api import get_items_by_id
from remarkable_mcp.lookup import lookup, lookup_folder
from remarkable_mcp.server import mcp

READ_TOOLS = {
    "remarkable_read",
    "remarkable_browse",
    "remarkable_search",
    "remarkable_recent",
    "remarkable_status",
    "remarkable_image",
    "remarkable_export",
}


def _item(item_id, name, parent="", is_folder=False):
    return SimpleNamespace(
        ID=item_id,
        VissibleName=name,
        Parent=parent,
        is_folder=is_folder,
        ModifiedClient=None,
        tags=[],
    )


def _library():
    return [
        _item("course", "Course", is_folder=True),
        _item("sheet", "Worksheet", parent="course"),
        _item("notes", "Notebook"),
        _item("book", "Book"),
    ]


def _client():
    client = Mock()
    client.get_meta_items.return_value = _library()
    types = {"sheet": "pdf", "notes": "notebook", "book": "epub"}
    client.get_file_type.side_effect = lambda doc: types.get(doc.ID)
    return client


def _error(reply):
    return json.loads(reply)["_error"]


@pytest.fixture(autouse=True)
def no_root(monkeypatch):
    monkeypatch.delenv("REMARKABLE_ROOT_PATH", raising=False)


# --- lookup ------------------------------------------------------------------


def test_lookup_finds_documents_and_folders():
    client = _client()
    assert lookup(client, "Worksheet", kind="pdf")[0].ID == "sheet"
    assert lookup(client, "/Course")[0].ID == "course"
    assert lookup(client, "Notebook", kind="document")[0].ID == "notes"


def test_missing_document_suggests_names():
    target, error = lookup(_client(), "Worksheat", note="Nothing was changed.")
    assert target is None
    error = _error(error)
    assert error["type"] == "document_not_found"
    assert error["did_you_mean"][0] == "Worksheet"
    assert error["suggestion"].endswith("Nothing was changed.")


def test_folder_where_a_document_is_expected():
    error = _error(lookup(_client(), "Course", kind="pdf")[1])
    assert error["type"] == "invalid_parameter"
    assert error["message"] == "document: 'Course' is a folder, not a PDF document."


@pytest.mark.parametrize("name, what", [("Notebook", "a notebook"), ("Book", "an EPUB")])
def test_non_pdf_where_a_pdf_is_expected(name, what):
    error = _error(lookup(_client(), name, kind="pdf")[1])
    assert error["type"] == "invalid_parameter"
    assert error["message"] == f"document: '{name}' is {what}, not a PDF."


def test_missing_folder_names_the_parameter():
    collection = _library()
    folder_id, error = lookup_folder("/Cours", collection, get_items_by_id(collection), param="x")
    assert folder_id is None
    error = _error(error)
    assert error["type"] == "folder_not_found"
    assert error["message"].startswith("x: ")
    assert error["did_you_mean"] == ["/Course"]


# --- tools -------------------------------------------------------------------


@pytest.fixture
def cloud(monkeypatch):
    from remarkable_mcp.write_tools import register_write_tools

    for var in (
        "REMARKABLE_USE_SSH",
        "REMARKABLE_USE_USB_WEB",
        "REMARKABLE_READ_ONLY",
        "REMARKABLE_USE_LOCAL_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    tools = mcp._tool_manager._tools
    for name in [n for n in tools if n not in READ_TOOLS]:
        tools.pop(name)
    register_write_tools()
    client = _client()
    with (
        patch("remarkable_mcp.write_tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
    ):
        yield client


PDF_TOOLS = [
    ("remarkable_mark_pdf", {"add": [{"page": 1, "shape": "check", "at": [0.5, 0.5]}]}),
    ("remarkable_pdf_outline", {}),
    ("remarkable_pdf_pages", {"insert": [{"after": 1}]}),
    ("remarkable_pdf_ocr", {}),
]


@pytest.mark.parametrize("tool, arguments", PDF_TOOLS)
async def test_pdf_tools_share_lookup_errors(cloud, tool, arguments):
    result = await mcp.call_tool(tool, {"document": "Course", **arguments})
    assert result.is_error
    assert _error(result.content[0].text)["message"].endswith("is a folder, not a PDF document.")

    result = await mcp.call_tool(tool, {"document": "Worksheat", **arguments})
    error = _error(result.content[0].text)
    assert error["type"] == "document_not_found"
    assert error["did_you_mean"][0] == "Worksheet"
    cloud.read_document_pdf.assert_not_called()


async def test_read_tool_reports_folder_clearly(cloud):
    result = await mcp.call_tool("remarkable_read", {"document": "Course"})
    error = _error(result.content[0].text)
    assert error["type"] == "invalid_parameter"
    assert "is a folder" in error["message"]


async def test_errors_set_is_error_over_the_protocol(cloud):
    with (
        patch("remarkable_mcp.resources.start_background_loader", return_value=None),
        patch("remarkable_mcp.resources.stop_background_loader", new_callable=AsyncMock),
    ):
        async with Client(mcp) as client:
            failed = await client.call_tool("remarkable_read", {"document": "Missing"})
            listed = await client.call_tool("remarkable_browse", {"path": "/"})
    assert failed.is_error
    assert json.loads(failed.content[0].text)["_error"]["type"] == "document_not_found"
    assert not listed.is_error

"""REMARKABLE_ROOT_PATH applies the same way to read and write tools."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from remarkable_mcp.api import get_items_by_id
from remarkable_mcp.lookup import display_path, find_folder_id, find_item, scoped_items
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
        _item("work", "Work", is_folder=True),
        _item("notes-in", "Notes", parent="work"),
        _item("archive-in", "Archive", parent="work", is_folder=True),
        _item("notes-out", "Notes"),
        _item("outside", "Outside"),
        _item("archive-out", "Archive", is_folder=True),
        _item("trashed", "Old", parent="trash"),
    ]


@pytest.fixture
def root(monkeypatch):
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Work")


@pytest.fixture
def cloud(monkeypatch, root):
    from remarkable_mcp.write_tools import register_write_tools

    for var in (
        "REMARKABLE_USE_SSH",
        "REMARKABLE_USE_USB_WEB",
        "REMARKABLE_READ_ONLY",
        "REMARKABLE_USE_LOCAL_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REMARKABLE_SKIP_CONFIRM", "1")
    tools = mcp._tool_manager._tools
    for name in [n for n in tools if n not in READ_TOOLS]:
        tools.pop(name)
    register_write_tools()
    client = Mock()
    client.get_meta_items.return_value = _library()
    with (
        patch("remarkable_mcp.write_tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
    ):
        yield client


async def _call(name, arguments):
    result = await mcp.call_tool(name, arguments)
    return json.loads(result.content[0].text)


# --- lookup ------------------------------------------------------------------


def test_lookup_applies_the_root(root):
    collection = _library()
    by_id = get_items_by_id(collection)
    assert find_item("/Notes", collection, by_id).ID == "notes-in"
    assert find_item("Notes", collection, by_id).ID == "notes-in"
    assert find_item("Outside", collection, by_id) is None
    assert find_item("/Work/Notes", collection, by_id) is None
    assert find_item("Work", collection, by_id) is None
    assert find_item("/Archive", collection, by_id, kind="folder").ID == "archive-in"
    assert find_item("/trash/Old", collection, by_id, include_trash=True) is None
    assert display_path(by_id["notes-in"], by_id) == "/Notes"


def test_lookup_folders_resolve_inside_the_root(root):
    collection = _library()
    by_id = get_items_by_id(collection)
    assert find_folder_id("/", collection, by_id) == "work"
    assert find_folder_id("", collection, by_id) == "work"
    assert find_folder_id("/Archive", collection, by_id) == "archive-in"
    assert find_folder_id("/Missing", collection, by_id) is None
    assert {i.ID for i in scoped_items(collection, by_id)} == {"notes-in", "archive-in"}


def test_lookup_without_a_root_reaches_trash_by_explicit_path(monkeypatch):
    monkeypatch.delenv("REMARKABLE_ROOT_PATH", raising=False)
    collection = _library()
    by_id = get_items_by_id(collection)
    assert find_item("/trash/Old", collection, by_id, include_trash=True).ID == "trashed"
    assert find_item("Old", collection, by_id, include_trash=True) is None
    assert find_folder_id("/", collection, by_id) == ""


def test_missing_root_folder_has_no_folder_id(monkeypatch):
    monkeypatch.setenv("REMARKABLE_ROOT_PATH", "/Missing")
    collection = _library()
    assert find_folder_id("/", collection, get_items_by_id(collection)) is None


# --- read tools --------------------------------------------------------------


def test_read_lookup_uses_the_root(root):
    from remarkable_mcp.tools import _find_target_document

    collection = _library()
    by_id = get_items_by_id(collection)
    assert _find_target_document(collection, by_id, "/Notes").ID == "notes-in"
    assert _find_target_document(collection, by_id, "Outside") is None


async def test_browse_shows_root_relative_paths(cloud):
    data = await _call("remarkable_browse", {"path": "/"})
    assert [d["name"] for d in data["documents"]] == ["Notes"]
    assert [f["name"] for f in data["folders"]] == ["Archive"]
    data = await _call("remarkable_browse", {"query": "Notes"})
    assert [d["path"] for d in data["results"]] == ["/Notes"]


# --- write tools -------------------------------------------------------------


async def test_move_resolves_document_and_destination_inside_the_root(cloud):
    data = await _call("remarkable_move", {"document": "/Notes", "dest_folder": "/Archive"})
    cloud.move.assert_called_once_with("notes-in", "archive-in")
    assert data["from"] == "/Notes"


async def test_move_to_root_targets_the_root_folder(cloud):
    cloud.get_meta_items.return_value = [
        *_library(),
        _item("deep", "Deep", parent="archive-in"),
    ]
    await _call("remarkable_move", {"document": "/Archive/Deep", "dest_folder": "/"})
    cloud.move.assert_called_once_with("deep", "work")


async def test_move_outside_document_is_not_found(cloud):
    data = await _call("remarkable_move", {"document": "Outside", "dest_folder": "/"})
    assert data["_error"]["type"] == "document_not_found"
    assert "Outside" not in (data["_error"].get("did_you_mean") or [])
    cloud.move.assert_not_called()


async def test_rename_uses_the_root(cloud):
    await _call("remarkable_rename", {"document": "Notes", "new_name": "Renamed"})
    cloud.rename.assert_called_once_with("notes-in", "Renamed")


async def test_delete_uses_the_root(cloud):
    data = await _call("remarkable_delete", {"document": "/Notes"})
    cloud.delete.assert_called_once_with("notes-in")
    assert data["path"] == "/Notes"


async def test_delete_outside_document_is_not_found(cloud):
    data = await _call("remarkable_delete", {"document": "/Outside"})
    assert data["_error"]["type"] == "document_not_found"
    cloud.delete.assert_not_called()


async def test_mkdir_at_root_creates_inside_the_root_folder(cloud):
    cloud.create_folder.return_value = SimpleNamespace(id="new")
    await _call("remarkable_mkdir", {"folder_name": "New"})
    cloud.create_folder.assert_called_once_with("New", "work")


async def test_tag_outside_document_is_not_found(cloud):
    data = await _call("remarkable_tag", {"document": "Outside", "add": "x"})
    assert data["_error"]["type"] == "document_not_found"
    cloud.update_documents_content.assert_not_called()


async def test_tag_uses_the_root(cloud):
    cloud.update_documents_content.return_value = {
        "notes-in": {"changed": True, "tags": ["x"], "added": ["x"]}
    }
    await _call("remarkable_tag", {"document": "/Notes", "add": "x"})
    assert cloud.update_documents_content.call_args.args[0] == ["notes-in"]


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("remarkable_mark_pdf", {"add": [{"page": 1, "shape": "check", "at": [0.5, 0.5]}]}),
        ("remarkable_pdf_outline", {}),
        ("remarkable_pdf_pages", {"insert": [{"after": 1}]}),
        ("remarkable_pdf_ocr", {}),
    ],
)
async def test_pdf_tools_do_not_reach_outside_the_root(cloud, tool, arguments):
    data = await _call(tool, {"document": "Outside", **arguments})
    assert data["_error"]["type"] == "document_not_found"
    cloud.read_document_pdf.assert_not_called()

"""Folder parameters are named folder; old names still work; SSH-only options
appear only in SSH mode."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from remarkable_mcp.server import mcp

READ_TOOLS = {
    "remarkable_read",
    "remarkable_browse",
    "remarkable_search",
    "remarkable_recent",
    "remarkable_status",
    "remarkable_image",
    "remarkable_page_layout",
    "remarkable_export",
    "remarkable_canvas",
}
MODE_VARIABLES = (
    "REMARKABLE_USE_SSH",
    "REMARKABLE_USE_USB_WEB",
    "REMARKABLE_READ_ONLY",
    "REMARKABLE_USE_LOCAL_DIR",
    "REMARKABLE_LOCAL_DIR",
    "REMARKABLE_ROOT_PATH",
)


def _register(monkeypatch, **env):
    from remarkable_mcp.write_tools import register_write_tools

    for var in MODE_VARIABLES:
        monkeypatch.delenv(var, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    tools = mcp._tool_manager._tools
    for name in [n for n in tools if n not in READ_TOOLS]:
        tools.pop(name)
    register_write_tools()
    return {tool.name: tool for tool in mcp._tool_manager.list_tools()}


def _properties(tool) -> dict:
    return tool.parameters["properties"]


def test_cloud_schemas_use_folder_and_hide_old_names(monkeypatch):
    tools = _register(monkeypatch)
    for name in ("remarkable_mkdir", "remarkable_move", "remarkable_upload"):
        assert "folder" in _properties(tools[name]), name
    assert "folder" in _properties(tools["remarkable_markdown_to_pdf"])
    hidden = {"parent", "dest_folder", "parent_folder", "check", "layout"}
    for tool in tools.values():
        assert not hidden & set(_properties(tool)), tool.name
    # remarkable_read's text part was called page; page is a tablet page elsewhere.
    assert "page" not in _properties(tools["remarkable_read"])
    assert "part" in _properties(tools["remarkable_read"])
    assert "dry_run" in _properties(tools["remarkable_pdf_ocr"])


def test_cloud_schemas_leave_out_ssh_only_options(monkeypatch):
    tools = _register(monkeypatch)
    for tool in tools.values():
        assert "defer_restart" not in _properties(tool), tool.name
        assert "defer_restart" not in tool.description, tool.name
        assert "remarkable_refresh" not in tool.description, tool.name
    assert "remarkable_refresh" not in tools


def test_ssh_schemas_keep_defer_restart(monkeypatch):
    tools = _register(monkeypatch, REMARKABLE_USE_SSH="1")
    for name in ("remarkable_move", "remarkable_upload", "remarkable_delete"):
        assert "defer_restart" in _properties(tools[name]), name
        assert "remarkable_refresh" in tools[name].description, name
    assert "remarkable_refresh" in tools


@pytest.fixture
def cloud(monkeypatch):
    _register(monkeypatch, REMARKABLE_SKIP_CONFIRM="1")
    library = [
        SimpleNamespace(ID="doc", VissibleName="Notes", Parent="", is_folder=False),
        SimpleNamespace(ID="arch", VissibleName="Archive", Parent="", is_folder=True),
    ]
    client = Mock()
    client.get_meta_items.return_value = library
    client.create_folder.return_value = SimpleNamespace(id="new")
    client.upload_document.return_value = SimpleNamespace(id="up")
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        yield client


async def _call(name, arguments):
    result = await mcp.call_tool(name, arguments)
    return json.loads(result.content[0].text)


def _applied_parent(client):
    (applies,) = client.update_documents_metadata.call_args.args
    metadata: dict = {}
    applies["doc"](metadata)
    return metadata["parent"]


async def test_move_takes_folder_or_dest_folder(cloud):
    await _call("remarkable_move", {"document": "Notes", "folder": "/Archive"})
    assert _applied_parent(cloud) == "arch"
    data = await _call("remarkable_move", {"document": "Notes", "dest_folder": "/Archive"})
    assert data["moved"] is True and _applied_parent(cloud) == "arch"
    both = await _call(
        "remarkable_move", {"document": "Notes", "folder": "/", "dest_folder": "/Archive"}
    )
    assert both["_error"]["type"] == "invalid_parameter"
    missing = await _call("remarkable_move", {"document": "Notes"})
    assert missing["_error"]["message"].startswith("folder:")


async def test_mkdir_takes_folder_or_parent(cloud):
    await _call("remarkable_mkdir", {"folder_name": "New", "folder": "/Archive"})
    await _call("remarkable_mkdir", {"folder_name": "New", "parent": "/Archive"})
    assert [c.args for c in cloud.create_folder.call_args_list] == [("New", "arch")] * 2


async def test_markdown_to_pdf_takes_folder_or_parent_folder(cloud):
    for key in ("folder", "parent_folder"):
        data = await _call(
            "remarkable_markdown_to_pdf",
            {"markdown": "# Hi", "document_name": "Hi", key: "/Archive"},
        )
        assert data["uploaded"] is True
        assert cloud.upload_document.call_args.args[3] == "arch"


async def test_defer_restart_is_still_accepted_in_cloud_mode(cloud):
    data = await _call("remarkable_mkdir", {"folder_name": "New", "defer_restart": True})
    assert data["created"] is True


async def test_pdf_ocr_takes_dry_run_or_check(cloud):
    with patch("remarkable_mcp.write_tools._ocr_check", return_value='{"checked": 0}') as check:
        await _call("remarkable_pdf_ocr", {"folder": "/Archive", "dry_run": True})
        await _call("remarkable_pdf_ocr", {"folder": "/Archive", "check": True})
    assert check.call_count == 2
    refused = await _call("remarkable_pdf_ocr", {"folder": "/Archive"})
    assert refused["_error"]["type"] == "invalid_parameter"

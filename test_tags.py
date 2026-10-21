"""Tests for document tag loading and tag-filtered browse/search."""

import json
from unittest.mock import Mock, patch

import pytest

from remarkable_mcp.server import mcp
from remarkable_mcp.sync import RemarkableClient
from remarkable_mcp.tags import document_tag_names, matches_tags, tag_names


async def _call_tool(name: str, arguments: dict):
    return await mcp.call_tool(name, arguments)


def _result_json(result) -> dict:
    return json.loads(result.content[0].text)


def test_tag_names_accepts_content_objects_and_strings():
    tags = [{"name": "test-doc", "timestamp": 1}, "work", {"name": "test-doc"}, {}]
    assert tag_names(tags) == ["test-doc", "work"]
    assert tag_names(None) == []


def test_document_tag_names_prefers_content_file():
    metadata = {"tags": ["old"]}
    content = {"tags": [{"name": "test-doc", "timestamp": 1}]}
    assert document_tag_names(metadata, content) == ["test-doc"]
    assert document_tag_names(metadata, {}) == ["old"]
    assert document_tag_names({}, None) == []


def test_matches_tags_is_case_insensitive():
    assert matches_tags([{"name": "Test-Doc"}], ["test-doc"])
    assert not matches_tags([{"name": "other"}], ["test-doc"])


def test_cloud_document_tags_are_read_from_content_blob():
    """Cloud listing reads tags from <id>.content; .metadata has none."""
    doc_id = "doc-1"
    blobs = {
        "index-hash": (
            "3\n" f"meta-hash:0:{doc_id}.metadata:0:10\n" f"content-hash:0:{doc_id}.content:0:10\n"
        ).encode(),
        "meta-hash": json.dumps(
            {"visibleName": "S0.2-06 Layers and tags test", "type": "DocumentType"}
        ).encode(),
        "content-hash": json.dumps(
            {
                "fileType": "pdf",
                "tags": [{"name": "test-doc", "timestamp": 1}],
                "pageTags": [{"name": "test-page", "pageId": "p3", "timestamp": 2}],
            }
        ).encode(),
    }
    client = RemarkableClient.__new__(RemarkableClient)
    client._get_file = lambda file_hash, file_name: blobs[file_hash]

    doc = client._load_document({"id": doc_id, "hash": "index-hash", "size": 0})

    assert doc is not None
    assert doc.tags == ["test-doc"]
    assert doc.page_tags == ["test-page"]


def test_ssh_document_tags_are_read_from_content_file():
    """SSH listing reads .metadata and .content in one batched command."""
    from remarkable_mcp.ssh import SSHClient

    content = json.dumps(
        {
            "fileType": "pdf",
            "tags": [{"name": "test-doc", "timestamp": 1}],
            "pageTags": [{"name": "test-page", "pageId": "p3", "timestamp": 2}],
        },
        indent=4,
    )
    output = (
        "\n===FILE===doc-1\n"
        '{"visibleName": "Tagged", "type": "DocumentType", "parent": ""}\n'
        "===CONTENT===\n"
        f"{content}\n"
        "\n===FILE===folder-1\n"
        '{"visibleName": "Folder", "type": "CollectionType", "parent": ""}\n'
        "===CONTENT===\n"
        "\n===FILE===doc-2\n"
        '{"visibleName": "Plain", "type": "DocumentType", "parent": "",'
        ' "tags": ["meta-tag"]}\n'
        "===CONTENT===\n"
        '{"fileType": "notebook"}\n'
    )
    commands = []

    def fake_command(command, timeout=30):
        commands.append(command)
        return output

    client = SSHClient()
    client._ssh_command = fake_command
    try:
        docs = {doc.ID: doc for doc in client.get_meta_items()}

        assert docs["doc-1"].tags == ["test-doc"]
        assert docs["doc-1"].page_tags == ["test-page"]
        assert docs["folder-1"].tags == []
        assert docs["doc-2"].tags == ["meta-tag"]
        assert docs["doc-2"].page_tags == []
        # The same listing fills the file-type cache without another command.
        assert client.get_all_file_types() == {"doc-1": "pdf", "doc-2": "notebook"}
        assert len(commands) == 1
        assert ".content" in commands[0]
    finally:
        client.close()


def test_ssh_limited_listing_does_not_fill_file_type_cache():
    from remarkable_mcp.ssh import SSHClient

    client = SSHClient()
    client._ssh_command = lambda command, timeout=30: (
        "===FILE===doc-1\n"
        '{"visibleName": "One", "type": "DocumentType"}\n'
        "===CONTENT===\n"
        '{"fileType": "pdf"}\n'
    )
    try:
        assert [doc.ID for doc in client.get_meta_items(limit=1)] == ["doc-1"]
        assert client._file_type_cache is None
    finally:
        client.close()


def _item(name, item_id, parent, *, folder=False, tags=None, page_tags=None):
    item = Mock()
    item.VissibleName = name
    item.ID = item_id
    item.Parent = parent
    item.is_folder = folder
    item.ModifiedClient = None
    item.tags = tags or []
    item.page_tags = page_tags or []
    return item


def _library():
    return [
        _item("Physics", "f-lunar", "", folder=True),
        _item("Worksheets", "f-ws", "f-lunar", folder=True),
        _item(
            "S0.2-06 Layers and tags test",
            "d-tagged",
            "f-ws",
            tags=[{"name": "test-doc", "timestamp": 1}],
            page_tags=["test-page"],
        ),
        _item("S0.2-07 Highlight colors test", "d-plain", "f-ws"),
        _item("Elsewhere", "d-root", "", tags=[{"name": "test-doc", "timestamp": 1}]),
    ]


async def _call_with_library(name: str, arguments: dict):
    client = Mock()
    client.get_meta_items.return_value = _library()
    with patch("remarkable_mcp.tools.get_rmapi", return_value=client):
        with patch("remarkable_mcp.tools._is_cloud_archived", return_value=False):
            return _result_json(await _call_tool(name, arguments))


@pytest.mark.asyncio
async def test_browse_tag_filter_lists_tagged_document_in_folder():
    data = await _call_with_library(
        "remarkable_browse", {"path": "/Physics/Worksheets", "tags": ["test-doc"]}
    )

    assert [d["name"] for d in data["documents"]] == ["S0.2-06 Layers and tags test"]
    assert data["documents"][0]["tags"] == ["test-doc"]
    assert data["documents"][0]["path"] == "/Physics/Worksheets/S0.2-06 Layers and tags test"


@pytest.mark.asyncio
async def test_browse_tag_filter_searches_subfolders():
    data = await _call_with_library("remarkable_browse", {"path": "/Physics", "tags": ["test-doc"]})

    assert data["recursive"] is True
    assert data["folders"] == []
    assert [d["id"] for d in data["documents"]] == ["d-tagged"]


@pytest.mark.asyncio
async def test_browse_without_tags_lists_tag_names():
    data = await _call_with_library("remarkable_browse", {"path": "/Physics/Worksheets"})

    by_name = {d["name"]: d for d in data["documents"]}
    assert by_name["S0.2-06 Layers and tags test"]["tags"] == ["test-doc"]
    assert "tags" not in by_name["S0.2-07 Highlight colors test"]


@pytest.mark.asyncio
async def test_search_mode_tag_filter_matches_content_tags():
    data = await _call_with_library(
        "remarkable_browse", {"query": "Layers and tags", "tags": ["test-doc"]}
    )

    assert [r["name"] for r in data["results"]] == ["S0.2-06 Layers and tags test"]
    assert data["results"][0]["tags"] == ["test-doc"]


@pytest.mark.asyncio
async def test_browse_tag_filter_matches_page_tags():
    data = await _call_with_library("remarkable_browse", {"path": "/", "tags": ["TEST-PAGE"]})

    assert [d["id"] for d in data["documents"]] == ["d-tagged"]
    assert data["documents"][0]["page_tags"] == ["test-page"]


@pytest.mark.asyncio
async def test_search_results_include_page_tags():
    client = Mock()
    client.get_meta_items.return_value = _library()
    read = ({"content": "text", "total_pages": 3}, "hint")
    with (
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools._is_cloud_archived", return_value=False),
        patch("remarkable_mcp.tools._read_document", return_value=read),
    ):
        data = _result_json(await _call_tool("remarkable_search", {"query": "Layers and tags"}))

    assert data["documents"][0]["tags"] == ["test-doc"]
    assert data["documents"][0]["page_tags"] == ["test-page"]

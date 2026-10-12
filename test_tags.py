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
            {"fileType": "pdf", "tags": [{"name": "test-doc", "timestamp": 1}]}
        ).encode(),
    }
    client = RemarkableClient.__new__(RemarkableClient)
    client._get_file = lambda file_hash, file_name: blobs[file_hash]

    doc = client._load_document({"id": doc_id, "hash": "index-hash", "size": 0})

    assert doc is not None
    assert doc.tags == ["test-doc"]


def _item(name, item_id, parent, *, folder=False, tags=None):
    item = Mock()
    item.VissibleName = name
    item.ID = item_id
    item.Parent = parent
    item.is_folder = folder
    item.ModifiedClient = None
    item.tags = tags or []
    return item


def _library():
    return [
        _item("Lunar Theory", "f-lunar", "", folder=True),
        _item("Worksheets", "f-ws", "f-lunar", folder=True),
        _item(
            "S0.2-06 Layers and tags test",
            "d-tagged",
            "f-ws",
            tags=[{"name": "test-doc", "timestamp": 1}],
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
        "remarkable_browse", {"path": "/Lunar Theory/Worksheets", "tags": ["test-doc"]}
    )

    assert [d["name"] for d in data["documents"]] == ["S0.2-06 Layers and tags test"]
    assert data["documents"][0]["tags"] == ["test-doc"]
    assert data["documents"][0]["path"] == "/Lunar Theory/Worksheets/S0.2-06 Layers and tags test"


@pytest.mark.asyncio
async def test_browse_tag_filter_searches_subfolders():
    data = await _call_with_library(
        "remarkable_browse", {"path": "/Lunar Theory", "tags": ["test-doc"]}
    )

    assert data["recursive"] is True
    assert data["folders"] == []
    assert [d["id"] for d in data["documents"]] == ["d-tagged"]


@pytest.mark.asyncio
async def test_browse_without_tags_lists_tag_names():
    data = await _call_with_library("remarkable_browse", {"path": "/Lunar Theory/Worksheets"})

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

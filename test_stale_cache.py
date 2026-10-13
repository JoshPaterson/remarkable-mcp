"""Cached extraction results are tied to the document version they came from.

The tablet can upload page content after a sync without changing the
document's ``modified`` time; the cloud index hash changes instead.
"""

import json
import zipfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from remarkable_mcp.api import document_version
from remarkable_mcp.extract import (
    cache_ocr_result,
    clear_extraction_cache,
    extract_text_from_document_zip,
    get_cached_ocr_result,
)
from remarkable_mcp.server import mcp


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_extraction_cache()
    yield
    clear_extraction_cache()


def _notebook_zip(path, page_count: int):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "doc.content",
            json.dumps(
                {
                    "fileType": "notebook",
                    "cPages": {"pages": [{"id": f"p{i}"} for i in range(page_count)]},
                }
            ),
        )
    return path


def test_document_version_follows_cloud_hash():
    modified = datetime(2026, 10, 13, 12, 34, 22)
    before = SimpleNamespace(hash="aaa", ModifiedClient=modified)
    after = SimpleNamespace(hash="bbb", ModifiedClient=modified)

    assert document_version(before) != document_version(after)
    assert document_version(SimpleNamespace()) is None


def test_extraction_cache_is_reused_only_for_the_same_version(tmp_path):
    two_pages = _notebook_zip(tmp_path / "v1.zip", 2)
    three_pages = _notebook_zip(tmp_path / "v2.zip", 3)

    assert extract_text_from_document_zip(two_pages, doc_id="doc", version="v1")["pages"] == 2
    # Same version: the cached result is served.
    assert extract_text_from_document_zip(three_pages, doc_id="doc", version="v1")["pages"] == 2
    # New version: re-extracted.
    assert extract_text_from_document_zip(three_pages, doc_id="doc", version="v2")["pages"] == 3


def test_cached_ocr_result_requires_matching_version():
    cache_ocr_result("doc", {"pages": 2}, include_ocr=True, version="v1")

    assert get_cached_ocr_result("doc", version="v1") == {"pages": 2}
    assert get_cached_ocr_result("doc", version="v2") is None


async def test_read_reflects_upload_with_unchanged_modified_time(tmp_path):
    doc = Mock()
    doc.VissibleName = "Test notebook"
    doc.ID = "doc"
    doc.Parent = ""
    doc.is_folder = False
    doc.tags = []
    doc.ModifiedClient = datetime(2026, 10, 13, 12, 34, 22)
    doc.hash = "before-sync"
    client = Mock()
    client.get_meta_items.return_value = [doc]
    client.download.return_value = _notebook_zip(tmp_path / "v1.zip", 2).read_bytes()

    async def read() -> dict:
        with (
            patch("remarkable_mcp.tools.get_rmapi", return_value=client),
            patch("remarkable_mcp.tools.get_file_type", return_value="notebook"),
        ):
            result = await mcp.call_tool(
                "remarkable_read", {"document": "Test notebook", "content_type": "annotations"}
            )
        return json.loads(result.content[0].text)

    assert (await read())["total_pages"] == 2

    # The tablet uploads a third page; modified time is unchanged, hash is new.
    doc.hash = "after-sync"
    client.download.return_value = _notebook_zip(tmp_path / "v2.zip", 3).read_bytes()

    assert (await read())["total_pages"] == 3

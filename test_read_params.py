"""remarkable_read's part and grep, remarkable_search's folder search."""

import io
import json
import re
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pymupdf
import pytest

from remarkable_mcp.server import mcp
from remarkable_mcp.tools import _grep_text, _group_by_size, _page_starts


@pytest.fixture(autouse=True)
def no_root(monkeypatch):
    monkeypatch.delenv("REMARKABLE_ROOT_PATH", raising=False)


def _doc(doc_id, name, parent=""):
    return SimpleNamespace(
        ID=doc_id, VissibleName=name, Parent=parent, is_folder=False, ModifiedClient=None, tags=[]
    )


def _folder(doc_id, name, parent=""):
    return SimpleNamespace(
        ID=doc_id, VissibleName=name, Parent=parent, is_folder=True, ModifiedClient=None, tags=[]
    )


def _content(**fields):
    content = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": None,
        "pages": 3,
        "page_ids": [],
        "annotated_pages": [],
        "ocr_backend": None,
        "tags": [],
    }
    content.update(fields)
    return content


def _archive(content: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("doc.content", json.dumps(content))
    return out.getvalue()


def _pdf(texts) -> bytes:
    doc = pymupdf.open()
    for text in texts:
        doc.new_page().insert_text((72, 100), text, fontsize=12)
    return doc.tobytes()


async def _read(arguments, *, file_type, extracted, archive=b"", pdf=None):
    client = Mock()
    client.get_meta_items.return_value = [_doc("doc", "Doc")]
    client.download.return_value = archive or _archive({"pages": ["a", "b", "c"]})
    with (
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools.get_file_type", return_value=file_type),
        patch("remarkable_mcp.tools.download_raw_file", return_value=pdf),
        patch("remarkable_mcp.tools.extract_text_from_document_zip", return_value=extracted),
        patch("remarkable_mcp.tools.get_document_page_count", return_value=3),
    ):
        result = await mcp.call_tool("remarkable_read", {"document": "Doc", **arguments})
    return json.loads(result.content[0].text)


# --- helpers -----------------------------------------------------------------


def test_matches_know_their_page():
    text = "--- Page 1 ---\nalpha\n\n--- Page 2 ---\nbeta alpha\n\n=== Annotations ===\nalpha"
    pattern = re.compile("alpha", re.IGNORECASE)
    matches = _grep_text(text, pattern, _page_starts(text))
    assert [m["page"] for m in matches] == [1, 2, None]


def test_pdf_pages_map_to_tablet_pages():
    text = "--- Page 1 ---\none\n\n--- Page 2 ---\ntwo\n\n--- Annotated pages ---\nPage 2: two"
    starts = _page_starts(text, source_chars=text.index("--- Annotated"), page_map=[0, None, 1])
    matches = _grep_text(text, re.compile("two"), starts)
    # PDF page 2 is tablet page 3 (a page was added on the tablet); the
    # annotated pages list already numbers tablet pages.
    assert [m["page"] for m in matches] == [3, 2]


def test_groups_stay_under_the_size():
    assert _group_by_size([3, 3, 3, 9], 6, lambda n: n) == [[3, 3], [3], [9]]
    assert _group_by_size([], 6, lambda n: n) == [[]]


# --- remarkable_read ---------------------------------------------------------


async def test_part_and_its_old_name_page():
    pages = [f"--- Page {n} ---\nnote {n}" for n in (1, 2, 3)]
    extracted = _content(handwritten_text=pages)
    by_part = await _read(
        {"part": 2, "include_ocr": True}, file_type="notebook", extracted=extracted
    )
    by_page = await _read(
        {"page": 2, "include_ocr": True}, file_type="notebook", extracted=extracted
    )
    assert by_part["content"] == by_page["content"] == pages[1]
    assert by_part["part"] == by_part["page"] == 2
    assert by_part["next_part"] == by_part["next_page"] == 3
    assert by_part["parts"] == by_part["content_pages"] == 3

    both = await _read({"part": 2, "page": 3}, file_type="notebook", extracted=extracted)
    assert both["_error"]["type"] == "invalid_parameter"


async def test_notebook_grep_searches_every_page():
    pages = [f"--- Page {n} ---\nnote {n}" for n in (1, 2, 3)]
    pages[2] += "\nthe deadline is Friday"
    extracted = _content(handwritten_text=pages)
    data = await _read(
        {"grep": "deadline", "include_ocr": True}, file_type="notebook", extracted=extracted
    )
    assert data["grep_matches"] == 1
    assert [m["page"] for m in data["matches"]] == [3]
    assert data["content"].startswith("[page 3] ")

    none = await _read(
        {"grep": "absent", "include_ocr": True}, file_type="notebook", extracted=extracted
    )
    assert none["grep_matches"] == 0 and none["matches"] == []
    assert "_error" not in none


async def test_pdf_grep_gives_tablet_pages():
    archive = _archive({"pages": ["a", "b", "c"], "redirectionPageMap": [0, -1, 1]})
    data = await _read(
        {"grep": "budget"},
        file_type="pdf",
        extracted=_content(),
        archive=archive,
        pdf=_pdf(["intro", "the budget"]),
    )
    assert [m["page"] for m in data["matches"]] == [3]


async def test_grep_matches_come_in_parts():
    text = "\n".join(f"line {n} keyword " + "x" * 150 for n in range(200))
    pdf = _pdf(["only page"])
    with patch("remarkable_mcp.tools.extract_text_from_pdf", return_value=text):
        first = await _read({"grep": "keyword"}, file_type="pdf", extracted=_content(), pdf=pdf)
        second = await _read(
            {"grep": "keyword", "part": 2}, file_type="pdf", extracted=_content(), pdf=pdf
        )
    assert first["grep_matches"] == 200 and first["more"] is True
    assert len(first["matches"]) + len(second["matches"]) <= 200
    assert first["matches"][0] != second["matches"][0]


# --- remarkable_search -------------------------------------------------------


async def test_search_reads_a_folder_without_a_name_match():
    library = [
        _folder("work", "Work"),
        _doc("a", "Plan", parent="work"),
        _doc("b", "Notes", parent="work"),
        _doc("c", "Plan elsewhere"),
    ]
    client = Mock()
    client.get_meta_items.return_value = library
    read = ({"content": "[page 2] budget", "grep_matches": 1, "matches": [{"page": 2}]}, "")
    with (
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools._read_document", return_value=read) as reader,
    ):
        result = await mcp.call_tool("remarkable_search", {"folder": "/Work", "grep": "budget"})
        nothing = await mcp.call_tool("remarkable_search", {"grep": "budget"})
    data = json.loads(result.content[0].text)
    assert [d["path"] for d in data["documents"]] == ["/Work/Notes", "/Work/Plan"]
    assert data["candidates"] == 2
    assert data["documents"][0]["match_pages"] == [2]
    assert reader.call_count == 2
    assert json.loads(nothing.content[0].text)["_error"]["type"] == "invalid_parameter"

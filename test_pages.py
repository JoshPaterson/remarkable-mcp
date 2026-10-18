"""Page order skips pages deleted on the tablet (cPages entries with "deleted")."""

import json
import zipfile

from remarkable_mcp.extract import (
    _get_ordered_rm_files,
    _get_page_order,
    _resolve_pdf_page_index,
    extract_text_from_document_zip,
    get_document_page_count,
)
from remarkable_mcp.pages import content_page_ids, deleted_page_ids
from remarkable_mcp.write_tools import _page_ids_from_content

DELETED = {"timestamp": "1:9", "value": 1}

CONTENT = {
    "formatVersion": 2,
    "cPages": {
        "pages": [
            {"id": "p1", "idx": {"timestamp": "1:2", "value": "ba"}, "redir": {"value": 0}},
            {
                "id": "gone",
                "idx": {"timestamp": "1:2", "value": "bb"},
                "redir": {"value": 1},
                "deleted": DELETED,
            },
            {"id": "p3", "idx": {"timestamp": "1:2", "value": "bc"}, "redir": {"value": 2}},
            {"id": "added", "idx": {"timestamp": "1:2", "value": "bd"}},
        ]
    },
}


def test_content_page_ids_skip_deleted_entries():
    assert content_page_ids(CONTENT) == ["p1", "p3", "added"]
    assert deleted_page_ids(CONTENT) == {"gone"}
    assert content_page_ids({"pages": ["a", "b"]}) == ["a", "b"]
    assert content_page_ids({"cPages": {"pages": [{"id": "x", "deleted": False}]}}) == ["x"]


def test_write_tool_page_ids_match_reader():
    assert _page_ids_from_content(CONTENT) == ["p1", "p3", "added"]


def _extracted(tmp_path):
    root = tmp_path / "doc"
    (root / "doc").mkdir(parents=True)
    (root / "doc.content").write_text(json.dumps(CONTENT))
    for page_id in ("p1", "gone", "p3"):
        (root / "doc" / f"{page_id}.rm").write_bytes(b"not-a-valid-rm")
    return root


def test_extracted_document_page_order(tmp_path):
    root = _extracted(tmp_path)

    assert _get_page_order(root) == ["p1", "p3", "added"]
    assert [f.stem for f in _get_ordered_rm_files(root)] == ["p1", "p3"]
    # The PDF page under page 2 is the one that follows the deleted page.
    assert _resolve_pdf_page_index(root, 2) == 2
    assert _resolve_pdf_page_index(root, 3) is None


def test_document_zip_page_count_and_ids(tmp_path):
    root = _extracted(tmp_path)
    zip_path = tmp_path / "doc.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in root.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(root))

    assert get_document_page_count(zip_path) == 3
    result = extract_text_from_document_zip(zip_path)
    assert result["pages"] == 3
    assert result["page_ids"] == ["p1", "p3", "added"]


def test_added_page_counts_only_live_pages():
    from remarkable_mcp import notebooks as nb

    content = {
        "cPages": {
            "pages": [
                {"id": "a", "idx": {"timestamp": "1:2", "value": "ba"}},
                {"id": "b", "idx": {"timestamp": "1:2", "value": "bb"}, "deleted": DELETED},
                {"id": "c", "idx": {"timestamp": "1:2", "value": "bc"}},
            ]
        }
    }

    result = nb.append_page_to_content(content, "d")

    assert result["total_pages"] == 3
    assert result["page_index"] == 3
    assert content["pageCount"] == 3
    # The new page sorts after every existing entry, deleted ones included.
    assert result["idx"] > "bc"

"""remarkable_tag: adding, renaming and removing document and page tags."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from remarkable_mcp import tablet_json
from remarkable_mcp.server import mcp
from remarkable_mcp.sync import RemarkableClient, _hash_entries, _serialize_doc_index
from remarkable_mcp.tag_edit import TagEditError, edit_tags

FIXTURES = Path(__file__).parent / "fixtures" / "tablet"
DOC_ID = "d7885702-6be5-42eb-8b96-414c94d4f403"
PAGES = ["page-a", "page-b"]
WRITE_TOOLS = [
    "remarkable_upload",
    "remarkable_markdown_to_pdf",
    "remarkable_mkdir",
    "remarkable_move",
    "remarkable_rename",
    "remarkable_tag",
    "remarkable_mark_pdf",
    "remarkable_delete",
    "remarkable_refresh",
    "remarkable_author",
]


# --- tablet JSON layout ------------------------------------------------------


@pytest.mark.parametrize(
    "path", sorted(FIXTURES.glob("*/*.content")) + sorted(FIXTURES.glob("*/*.metadata"))
)
def test_tablet_json_reproduces_tablet_files(path):
    raw = path.read_text()
    assert tablet_json.dumps(json.loads(raw)) == raw


def test_tablet_json_layout():
    value = {"b": [], "a": {}, "tags": [{"name": "Café", "timestamp": 1}], "n": 1.5}
    assert tablet_json.dumps(value) == (
        "{\n"
        '    "b": [\n    ],\n'
        '    "a": {\n    },\n'
        '    "tags": [\n'
        "        {\n"
        '            "name": "Café",\n'
        '            "timestamp": 1\n'
        "        }\n"
        "    ],\n"
        '    "n": 1.5\n'
        "}\n"
    )


# --- tag editing -------------------------------------------------------------


def _content(**fields):
    content = {"fileType": "pdf", "tags": [], "pageTags": [], "pages": list(PAGES)}
    content.update(fields)
    return content


def _tag(name, ts=1):
    return {"name": name, "timestamp": ts}


def _page_tag(name, page_id, ts=1):
    return {"name": name, "pageId": page_id, "timestamp": ts}


def test_add_puts_new_tags_first():
    content = _content(tags=[_tag("old")])
    result = edit_tags(content, now_ms=9, add=["new"])

    assert content["tags"] == [_tag("new", 9), _tag("old")]
    assert result["tags"] == ["new", "old"]
    assert result["added"] == ["new"] and result["changed"]


def test_add_existing_tag_case_insensitive_is_noop():
    content = _content(tags=[_tag("Graded")])
    result = edit_tags(content, now_ms=9, add=["graded"])

    assert content["tags"] == [_tag("Graded")]
    assert not result["changed"]
    assert "'Graded' is already tagged." in result["notes"]


def test_add_reuses_library_spelling():
    content = _content()
    result = edit_tags(content, now_ms=9, add=["graded"], known_tags=["Graded", "todo"])

    assert result["added"] == ["Graded"]
    assert any("existing spelling 'Graded'" in n for n in result["notes"])


def test_remove():
    content = _content(tags=[_tag("a"), _tag("b")])
    result = edit_tags(content, now_ms=9, remove=["A", "missing"])

    assert content["tags"] == [_tag("b")]
    assert result["removed"] == ["a"]
    assert "'missing' was not tagged, so it was not removed." in result["notes"]


def test_rename_keeps_position_and_updates_timestamp():
    content = _content(tags=[_tag("a"), _tag("todo"), _tag("b")])
    result = edit_tags(content, now_ms=9, rename={"TODO": "done"})

    assert content["tags"] == [_tag("a"), _tag("done", 9), _tag("b")]
    assert result["renamed"] == [{"from": "todo", "to": "done"}]


def test_rename_capitalization_only():
    content = _content(tags=[_tag("graded")])
    result = edit_tags(content, now_ms=9, rename={"graded": "Graded"})

    assert content["tags"] == [_tag("Graded", 9)]
    assert result["changed"]


def test_rename_onto_existing_tag_merges():
    content = _content(tags=[_tag("todo"), _tag("Done")])
    result = edit_tags(content, now_ms=9, rename={"todo": "done"})

    assert content["tags"] == [_tag("Done")]
    assert result["renamed"] == [{"from": "todo", "to": "Done"}]


def test_rename_missing_tag_is_noted():
    content = _content(tags=[_tag("a")])
    result = edit_tags(content, now_ms=9, rename={"x": "y"})

    assert not result["changed"]
    assert "'x' was not tagged, so it was not renamed." in result["notes"]


def test_page_tags_touch_only_that_page():
    content = _content(pageTags=[_page_tag("keep", "page-a"), _page_tag("x", "page-b")])
    result = edit_tags(content, now_ms=9, page_id="page-b", add=["new"], remove=["x"])

    assert content["pageTags"] == [_page_tag("new", "page-b", 9), _page_tag("keep", "page-a")]
    assert content["tags"] == []
    assert result["tags"] == ["new"]


def test_page_tags_with_same_name_on_other_page_are_separate():
    content = _content(pageTags=[_page_tag("review", "page-a")])
    edit_tags(content, now_ms=9, page_id="page-b", add=["review"])

    assert [t["pageId"] for t in content["pageTags"]] == ["page-b", "page-a"]


def test_listing_without_changes():
    content = _content(tags=[_tag("a")])
    result = edit_tags(content, now_ms=9)

    assert result == {
        "tags": ["a"],
        "added": [],
        "removed": [],
        "renamed": [],
        "notes": [],
        "changed": False,
    }


@pytest.mark.parametrize(
    "kwargs",
    [{"add": [""]}, {"remove": ["  "]}, {"rename": {"a": ""}}, {"add": ["x"], "remove": ["X"]}],
)
def test_invalid_changes(kwargs):
    with pytest.raises(TagEditError):
        edit_tags(_content(), now_ms=9, **kwargs)


# --- cloud transport ---------------------------------------------------------


class FakeCloud(RemarkableClient):
    """Real sync client logic over an in-memory blob store and root."""

    def __init__(self, content: dict, metadata_raw: str):
        super().__init__()
        self.blobs = {}
        self.commits = 0
        files = [
            self._blob(tablet_json.dumps(content).encode(), f"{DOC_ID}.content"),
            self._blob(metadata_raw.encode(), f"{DOC_ID}.metadata"),
            self._blob(b"%PDF-1.4", f"{DOC_ID}.pdf"),
        ]
        doc_hash = _hash_entries(files)
        self.blobs[doc_hash] = _serialize_doc_index(files)
        self.entries = [{"hash": doc_hash, "type": "0", "id": DOC_ID, "subfiles": 3, "size": 100}]
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
        doc = SimpleNamespace(
            ID=DOC_ID, VissibleName="Tag test", Parent="", is_folder=False, tags=["Graded"]
        )
        folder = SimpleNamespace(
            ID="folder", VissibleName="Course", Parent="", is_folder=True, tags=[]
        )
        return [doc, folder]

    def stored(self, suffix: str) -> bytes:
        index = self._parse_index(self.blobs[self.entries[0]["hash"]])
        entry = next(f for f in index if f["id"].endswith(suffix))
        return self.blobs[entry["hash"]]


METADATA = tablet_json.dumps(
    {
        "createdTime": "1",
        "lastModified": "1",
        "metadatamodified": False,
        "parent": "",
        "type": "DocumentType",
        "version": 4,
        "visibleName": "Tag test",
    }
)


@pytest.fixture
def cloud(monkeypatch):
    from remarkable_mcp.write_tools import register_write_tools

    for var in ("REMARKABLE_USE_SSH", "REMARKABLE_USE_USB_WEB", "REMARKABLE_READ_ONLY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("REMARKABLE_USE_LOCAL_DIR", raising=False)
    register_write_tools()
    fake = FakeCloud(_content(tags=[_tag("old")]), METADATA)
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake):
        yield fake
    for name in WRITE_TOOLS:
        mcp._tool_manager._tools.pop(name, None)


async def _tag_tool(**arguments) -> dict:
    result = await mcp.call_tool("remarkable_tag", arguments)
    return json.loads(result.content[0].text)


async def test_cloud_add_writes_tablet_layout(cloud):
    data = await _tag_tool(document="Tag test", add="to-grade")

    assert data["tags"] == ["to-grade", "old"]
    assert data["transport"] == "cloud"
    content_raw = cloud.stored(".content").decode()
    content = json.loads(content_raw)
    assert [t["name"] for t in content["tags"]] == ["to-grade", "old"]
    assert content_raw == tablet_json.dumps(content)
    # Every other field is unchanged, in the original order.
    assert list(content) == list(_content())
    meta_raw = cloud.stored(".metadata").decode()
    meta = json.loads(meta_raw)
    assert meta_raw == tablet_json.dumps(meta)
    assert meta["lastModified"] != "1"
    assert meta["version"] == 4 and meta["metadatamodified"] is False
    assert cloud.commits == 1


async def test_cloud_add_reuses_library_spelling(cloud):
    data = await _tag_tool(document="Tag test", add="graded")

    assert data["added"] == ["Graded"]


async def test_cloud_page_tag(cloud):
    data = await _tag_tool(document="Tag test", page=2, add=["review"])

    assert data["page"] == 2 and data["tags"] == ["review"]
    content = json.loads(cloud.stored(".content"))
    assert content["pageTags"][0]["pageId"] == "page-b"


async def test_cloud_no_change_commits_nothing(cloud):
    before = cloud.stored(".content")
    data = await _tag_tool(document="Tag test", add="OLD", remove="missing")

    assert not data["changed"]
    assert cloud.commits == 0
    assert cloud.stored(".content") == before
    assert len(data["notes"]) == 2


async def test_cloud_listing(cloud):
    data = await _tag_tool(document="Tag test")

    assert data["tags"] == ["old"] and not data["changed"]
    assert cloud.commits == 0


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"document": "Tag test", "page": 3, "add": "x"}, "Page 3 does not exist"),
        ({"document": "Tag test", "page": 0, "add": "x"}, "page must be 1 or more"),
        ({"document": "Course", "add": "x"}, "is a folder"),
        ({"document": "Tag test", "add": "x", "remove": "X"}, "both added and removed"),
    ],
)
async def test_cloud_rejects_bad_requests(cloud, arguments, message):
    data = await _tag_tool(**arguments)

    assert data["_error"]["type"] == "invalid_parameter"
    assert message in data["_error"]["message"]
    assert cloud.commits == 0


async def test_cloud_unknown_document(cloud):
    data = await _tag_tool(document="Nope", add="x")

    assert data["_error"]["type"] == "document_not_found"


def test_update_refuses_changes_outside_tags():
    fake = FakeCloud(_content(), METADATA)

    def apply(content):
        content["fileType"] = "epub"
        content["tags"] = [_tag("x")]
        return {"changed": True}

    with pytest.raises(Exception, match="fields other than tags"):
        fake.update_document_content(DOC_ID, apply)
    assert fake.commits == 0


async def test_tag_tool_not_registered_over_usb(monkeypatch):
    from remarkable_mcp.write_tools import register_write_tools

    monkeypatch.delenv("REMARKABLE_USE_SSH", raising=False)
    monkeypatch.setenv("REMARKABLE_USE_USB_WEB", "1")
    register_write_tools()
    try:
        names = {t.name for t in await mcp.list_tools()}
        assert "remarkable_tag" not in names
    finally:
        for name in WRITE_TOOLS:
            mcp._tool_manager._tools.pop(name, None)


# --- several documents ------------------------------------------------------

DOC2_ID = "5b1f3f0a-7c55-4a8e-9a43-2f6f2f2d9c11"


class TwoDocCloud(FakeCloud):
    """FakeCloud with a second, one-page document tagged "Graded"."""

    def __init__(self):
        super().__init__(_content(tags=[_tag("old")]), METADATA)
        files = [
            self._blob(
                tablet_json.dumps(_content(tags=[_tag("Graded")], pages=["page-z"])).encode(),
                f"{DOC2_ID}.content",
            ),
            self._blob(METADATA.replace("Tag test", "Second").encode(), f"{DOC2_ID}.metadata"),
            self._blob(b"%PDF-1.4", f"{DOC2_ID}.pdf"),
        ]
        doc_hash = _hash_entries(files)
        self.blobs[doc_hash] = _serialize_doc_index(files)
        self.entries.append(
            {"hash": doc_hash, "type": "0", "id": DOC2_ID, "subfiles": 3, "size": 100}
        )

    def get_meta_items(self, *args, **kwargs):
        second = SimpleNamespace(
            ID=DOC2_ID, VissibleName="Second", Parent="", is_folder=False, tags=["Graded"]
        )
        return super().get_meta_items() + [second]

    def stored_for(self, doc_id: str, suffix: str) -> bytes:
        entry = next(e for e in self.entries if e["id"] == doc_id)
        index = self._parse_index(self.blobs[entry["hash"]])
        return self.blobs[next(f for f in index if f["id"].endswith(suffix))["hash"]]


@pytest.fixture
def two_docs(cloud):
    fake = TwoDocCloud()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake):
        yield fake


def _doc_tags(fake, doc_id):
    return [t["name"] for t in json.loads(fake.stored_for(doc_id, ".content"))["tags"]]


async def test_cloud_list_changes_every_document_in_one_commit(two_docs):
    data = await _tag_tool(document=["Tag test", "Second"], add="graded", remove="old")

    assert two_docs.commits == 1
    assert data["changed"] == ["Tag test"]
    by_name = {d["document"]: d for d in data["documents"]}
    assert by_name["Tag test"]["tags"] == ["Graded"]
    assert not by_name["Second"]["changed"] and by_name["Second"]["notes"]
    assert _doc_tags(two_docs, DOC_ID) == ["Graded"]
    assert _doc_tags(two_docs, DOC2_ID) == ["Graded"]


async def test_cloud_list_with_duplicates_lists_each_document_once(two_docs):
    data = await _tag_tool(document=["Tag test", "/Tag test", "Second"], add="x")

    assert [d["document"] for d in data["documents"]] == ["Tag test", "Second"]
    assert two_docs.commits == 1


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"document": ["Tag test", "Nope"], "add": "x"}, "Document not found: 'Nope'"),
        ({"document": ["Tag test", "Course"], "add": "x"}, "is a folder"),
        ({"document": ["Tag test", "Second"], "page": 2, "add": "x"}, "Page 2 does not exist"),
        ({"document": [], "add": "x"}, "a name or a list of names"),
    ],
)
async def test_cloud_list_changes_nothing_when_one_document_fails(two_docs, arguments, message):
    data = await _tag_tool(**arguments)

    assert message in data["_error"]["message"]
    assert two_docs.commits == 0
    assert _doc_tags(two_docs, DOC_ID) == ["old"]


def test_ssh_list_reads_all_then_writes_changed_with_one_restart(monkeypatch):
    from remarkable_mcp import write_tools

    files = {
        f"{write_tools.XOCHITL_PATH}/a.content": tablet_json.dumps(_content()).encode(),
        f"{write_tools.XOCHITL_PATH}/a.metadata": METADATA.encode(),
        f"{write_tools.XOCHITL_PATH}/b.content": tablet_json.dumps(
            _content(tags=[_tag("x")])
        ).encode(),
        f"{write_tools.XOCHITL_PATH}/b.metadata": METADATA.encode(),
    }
    written, restarts = [], []
    monkeypatch.setattr(write_tools, "_read_remote_bytes", lambda c, path: files.get(path))
    monkeypatch.setattr(
        write_tools, "_write_remote_bytes", lambda c, path, data: written.append(path)
    )
    monkeypatch.setattr(
        write_tools, "_maybe_restart_xochitl", lambda c, defer: restarts.append(defer) or True
    )
    monkeypatch.setattr(write_tools, "_update_deferred_ssh_cache", lambda *a, **k: None)
    client = SimpleNamespace(mark_operation_dirty=lambda: None)
    targets = [SimpleNamespace(ID=i, VissibleName=i) for i in ("a", "b")]
    apply = write_tools._tag_editor(["x"], [], {}, None, [])

    results, pending = write_tools._ssh_tag(client, targets, apply, False)

    assert results["a"]["changed"] and not results["b"]["changed"]
    assert written == [
        f"{write_tools.XOCHITL_PATH}/a.content",
        f"{write_tools.XOCHITL_PATH}/a.metadata",
    ]
    assert restarts == [False] and not pending

    missing = [SimpleNamespace(ID="a", VissibleName="a"), SimpleNamespace(ID="c", VissibleName="c")]
    written.clear()
    error = json.loads(write_tools._ssh_tag(client, missing, apply, False))
    assert error["_error"]["type"] == "no_content" and written == []

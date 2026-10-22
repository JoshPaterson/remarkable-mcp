"""Moving, renaming and trashing several documents in one write."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from remarkable_mcp.server import mcp
from remarkable_mcp.sync import RemarkableClient, _hash_entries, _serialize_doc_index

TOOLS = ("remarkable_move", "remarkable_rename", "remarkable_trash")
DOCS = {
    "doc-a": ("Week 1", "", False),
    "doc-b": ("Week 2", "", False),
    "fold-x": ("Archive", "", True),
    "fold-y": ("Inner", "fold-x", True),
}


class FakeCloud(RemarkableClient):
    """Real sync client logic over an in-memory blob store and root."""

    def __init__(self):
        super().__init__()
        self.blobs = {}
        self.commits = 0
        self.entries = []
        for doc_id, (name, parent, folder) in DOCS.items():
            meta = {
                "visibleName": name,
                "parent": parent,
                "type": "CollectionType" if folder else "DocumentType",
                "version": 1,
            }
            files = [self._blob(json.dumps(meta).encode(), f"{doc_id}.metadata")]
            doc_hash = _hash_entries(files)
            self.blobs[doc_hash] = _serialize_doc_index(files)
            self.entries.append(
                {"hash": doc_hash, "type": "0", "id": doc_id, "subfiles": 1, "size": 1}
            )
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
        items = []
        for doc_id in DOCS:
            meta = self.metadata(doc_id)
            items.append(
                SimpleNamespace(
                    ID=doc_id,
                    VissibleName=meta["visibleName"],
                    Parent=meta["parent"],
                    is_folder=meta["type"] == "CollectionType",
                )
            )
        return items

    def metadata(self, doc_id: str) -> dict:
        entry = next(e for e in self.entries if e["id"] == doc_id)
        (meta,) = self._parse_index(self.blobs[entry["hash"]])
        return json.loads(self.blobs[meta["hash"]])


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
    monkeypatch.setenv("REMARKABLE_SKIP_CONFIRM", "1")
    saved = dict(mcp._tool_manager._tools)
    register_write_tools()
    fake = FakeCloud()
    try:
        with patch("remarkable_mcp.write_tools.get_rmapi", return_value=fake):
            yield fake
    finally:
        mcp._tool_manager._tools.clear()
        mcp._tool_manager._tools.update(saved)


async def _call(name, **arguments) -> dict:
    result = await mcp.call_tool(name, arguments)
    return json.loads(result.content[0].text)


async def test_move_several_in_one_commit(cloud):
    data = await _call(
        "remarkable_move", document=["Week 1", "/Week 2", "Week 1"], dest_folder="/Archive"
    )

    assert data["count"] == 2 and cloud.commits == 1
    assert [m["name"] for m in data["moved"]] == ["Week 1", "Week 2"]
    assert cloud.metadata("doc-a")["parent"] == "fold-x"
    assert cloud.metadata("doc-b")["parent"] == "fold-x"
    assert cloud.metadata("doc-a")["version"] == 2


async def test_rename_several_in_one_commit(cloud):
    data = await _call(
        "remarkable_rename", renames={"Week 1": "Week 1 - graded", "Week 2": "Week 2 - graded"}
    )

    assert data["count"] == 2 and cloud.commits == 1
    assert cloud.metadata("doc-a")["visibleName"] == "Week 1 - graded"
    assert cloud.metadata("doc-b")["visibleName"] == "Week 2 - graded"


async def test_trash_several_in_one_commit(cloud):
    data = await _call("remarkable_trash", document=["Week 1", "Week 2"])

    assert data["count"] == 2 and cloud.commits == 1
    assert cloud.metadata("doc-a")["parent"] == "trash"
    assert cloud.metadata("doc-b")["parent"] == "trash"


@pytest.mark.parametrize(
    "tool, arguments, message",
    [
        ("remarkable_move", {"document": ["Week 1", "Nope"], "dest_folder": "/"}, "'Nope'"),
        ("remarkable_move", {"document": ["Week 1"], "dest_folder": "/Nope"}, "not found"),
        ("remarkable_move", {"document": ["Archive"], "dest_folder": "/Archive/Inner"}, "itself"),
        ("remarkable_rename", {"renames": {"Week 1": "A", "Nope": "B"}}, "'Nope'"),
        ("remarkable_rename", {"renames": {"Week 1": "A"}, "document": "Week 2"}, "not both"),
        ("remarkable_rename", {"renames": {}}, "not both"),
        ("remarkable_trash", {"document": ["Week 1", "Nope"]}, "'Nope'"),
        ("remarkable_trash", {"document": []}, "non-empty list"),
    ],
)
async def test_one_bad_item_changes_nothing(cloud, tool, arguments, message):
    data = await _call(tool, **arguments)

    assert message in data["_error"]["message"]
    assert cloud.commits == 0


async def test_single_names_keep_their_old_responses(cloud):
    data = await _call("remarkable_rename", document="Week 1", new_name="First")
    assert data["renamed"] is True and data["new_name"] == "First"


def test_one_confirmation_lists_every_item(monkeypatch):
    from remarkable_mcp.write_tools import _resolve_trash_confirmation

    monkeypatch.delenv("REMARKABLE_SKIP_CONFIRM", raising=False)
    elicit = _resolve_trash_confirmation(["Week 1", "Week 2"], True)

    assert "2 items ('Week 1', 'Week 2')" in elicit.message


def test_ssh_reads_everything_before_writing(monkeypatch):
    from remarkable_mcp import write_tools

    stored = {
        f"{write_tools.XOCHITL_PATH}/doc-a.metadata": b'{"visibleName": "Week 1", "parent": ""}',
        f"{write_tools.XOCHITL_PATH}/doc-b.metadata": b'{"visibleName": "Week 2", "parent": ""}',
    }
    written, restarts = [], []

    def download(path):
        if path not in stored:
            raise FileNotFoundError(path)
        return stored[path]

    monkeypatch.setattr(
        write_tools, "_write_metadata", lambda c, doc_id, meta: written.append((doc_id, meta))
    )
    monkeypatch.setattr(
        write_tools, "_maybe_restart_xochitl", lambda c, defer: restarts.append(defer) or True
    )
    client = SimpleNamespace(_scp_download=download)
    targets = [SimpleNamespace(ID="doc-a"), SimpleNamespace(ID="doc-b")]

    def apply(meta):
        meta["parent"] = "fold-x"

    assert write_tools._ssh_update_metadata(client, [(t, apply) for t in targets], False)
    assert [doc_id for doc_id, _ in written] == ["doc-a", "doc-b"]
    assert all(meta["parent"] == "fold-x" for _, meta in written)
    assert restarts == [False]

    written.clear()
    missing = targets + [SimpleNamespace(ID="doc-c")]
    with pytest.raises(FileNotFoundError):
        write_tools._ssh_update_metadata(client, [(t, apply) for t in missing], False)
    assert written == []


# --- one document and a batch of one reply alike ----------------------------


def _single_and_batch_of_one(tool):
    """Arguments for ``tool`` on Week 1 alone, and as a batch of one."""
    if tool == "remarkable_move":
        single = {"document": "Week 1", "dest_folder": "/Archive"}
        return single, dict(single, document=["Week 1"])
    if tool == "remarkable_rename":
        return {"document": "Week 1", "new_name": "First"}, {"renames": {"Week 1": "First"}}
    return {"document": "Week 1"}, {"document": ["Week 1"]}


def _batch_entry(tool, batch):
    if tool == "remarkable_move":
        return dict(batch["moved"][0], to=batch["to"])
    if tool == "remarkable_rename":
        return batch["renamed"][0]
    assert batch["deleted"] == batch["trashed"]
    return batch["trashed"][0]


def _single_entry(single):
    dropped = ("moved", "renamed", "trashed", "deleted", "_hint", "transport", "refresh_pending")
    return {k: v for k, v in single.items() if k not in dropped}


@pytest.mark.parametrize("tool", TOOLS)
async def test_cloud_single_and_batch_replies_agree(cloud, tool):
    single_args, batch_args = _single_and_batch_of_one(tool)
    single = await _call(tool, **single_args)
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=FakeCloud()):
        batch = await _call(tool, **batch_args)

    assert single[tool.split("_")[1].rstrip("e") + "ed"] is True
    assert single["transport"] == batch["transport"] == "cloud"
    assert "refresh_pending" not in single
    assert _single_entry(single) == _batch_entry(tool, batch)


class FakeSSH:
    """Metadata files of the DOCS library behind the SSH write helpers."""

    def __init__(self):
        from remarkable_mcp.write_tools import XOCHITL_PATH

        self.files = {
            f"{XOCHITL_PATH}/{doc_id}.metadata": json.dumps(
                {"visibleName": name, "parent": parent}
            ).encode()
            for doc_id, (name, parent, _) in DOCS.items()
        }

    def get_meta_items(self):
        return [
            SimpleNamespace(ID=doc_id, VissibleName=name, Parent=parent, is_folder=folder)
            for doc_id, (name, parent, folder) in DOCS.items()
        ]

    def _scp_download(self, path):
        return self.files[path]


@pytest.mark.parametrize("tool", TOOLS)
def test_ssh_single_and_batch_replies_agree(monkeypatch, tool):
    from remarkable_mcp import write_tools

    monkeypatch.setattr(write_tools, "_write_metadata", lambda c, doc_id, meta: None)
    monkeypatch.setattr(write_tools, "_maybe_restart_xochitl", lambda c, defer: not defer)
    monkeypatch.setattr(write_tools, "_update_deferred_ssh_cache", lambda *a, **k: None)
    monkeypatch.setattr(write_tools, "_clear_document_extraction_cache", lambda doc_id: None)

    def run(single: bool) -> dict:
        client = FakeSSH()
        if tool == "remarkable_move":
            reply = write_tools._move_many(client, ["Week 1"], "/Archive", False, True, single)
        elif tool == "remarkable_rename":
            reply = write_tools._rename_many(client, {"Week 1": "First"}, False, True, single)
        else:
            reply = write_tools._trash_many(client, ["Week 1"], False, True, single)
        return json.loads(reply)

    single, batch = run(True), run(False)
    assert single["transport"] == batch["transport"] == "ssh"
    assert single["refresh_pending"] is batch["refresh_pending"] is True
    assert _single_entry(single) == _batch_entry(tool, batch)

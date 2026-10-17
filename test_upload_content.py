"""remarkable_upload from base64 contents, and moving staged sources to the Trash."""

import base64
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from remarkable_mcp.server import mcp

PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"
EPUB = b"PK\x03\x04" + b"\x00" * 26 + b"mimetypeapplication/epub+zip"
WRITE_TOOLS = [
    "remarkable_upload",
    "remarkable_markdown_to_pdf",
    "remarkable_mkdir",
    "remarkable_move",
    "remarkable_rename",
    "remarkable_delete",
    "remarkable_refresh",
    "remarkable_author",
]
MODE_VARS = [
    "REMARKABLE_USE_SSH",
    "REMARKABLE_USE_USB_WEB",
    "REMARKABLE_USE_LOCAL_DIR",
    "REMARKABLE_LOCAL_DIR",
    "REMARKABLE_READ_ONLY",
    "REMARKABLE_MAX_INLINE_BYTES",
    "REMARKABLE_UPLOAD_STAGING_DIR",
]


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Private temp directory for the upload's decoded file; checked empty afterwards."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    yield root
    assert list(root.iterdir()) == []


def _register(monkeypatch, mode: str = "cloud"):
    from remarkable_mcp.write_tools import register_write_tools

    for var in MODE_VARS:
        monkeypatch.delenv(var, raising=False)
    if mode == "ssh":
        monkeypatch.setenv("REMARKABLE_USE_SSH", "1")
    elif mode == "usb":
        monkeypatch.setenv("REMARKABLE_USE_USB_WEB", "1")
    register_write_tools()


@pytest.fixture(autouse=True)
def _cleanup_tools():
    yield
    for name in WRITE_TOOLS:
        mcp._tool_manager._tools.pop(name, None)


def _cloud_client():
    folder = Mock()
    folder.ID = "folder-id"
    folder.VissibleName = "Course"
    folder.Parent = ""
    folder.is_folder = True
    doc = Mock()
    doc.id = "new-doc-id"
    client = Mock(spec=["get_meta_items", "upload_document"])
    client.get_meta_items.return_value = [folder]
    client.upload_document.return_value = doc
    return client


async def _upload(**arguments) -> dict:
    result = await mcp.call_tool("remarkable_upload", arguments)
    return json.loads(result.content[0].text)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_content_upload_to_cloud_folder(monkeypatch, temp_root):
    _register(monkeypatch)
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(
            file_content=_b64(PDF), document_name="Week 3", parent_folder="/Course"
        )

    assert data["uploaded"] is True
    assert data["name"] == "Week 3"
    assert data["uuid"] == "new-doc-id"
    assert data["format"] == "pdf"
    assert data["parent_folder"] == "/Course"
    assert data["transport"] == "cloud"
    assert data["source"] == "content"
    assert data["size_bytes"] == len(PDF)
    assert client.upload_document.call_args[0] == (PDF, "Week 3", "pdf", "folder-id")


@pytest.mark.parametrize(
    "encoded",
    [
        "data:application/pdf;base64," + _b64(PDF),
        "\n".join(_b64(PDF)[i : i + 16] for i in range(0, len(_b64(PDF)), 16)),
        _b64(PDF).rstrip("="),
    ],
    ids=["data-url", "line-breaks", "no-padding"],
)
async def test_content_encodings_accepted(monkeypatch, temp_root, encoded):
    _register(monkeypatch)
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_content=encoded, document_name="Doc")

    assert data["uploaded"] is True
    assert client.upload_document.call_args[0][0] == PDF


async def test_epub_content(monkeypatch, temp_root):
    _register(monkeypatch)
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_content=_b64(EPUB), document_name="Book", file_format="epub")

    assert data["format"] == "epub"
    assert client.upload_document.call_args[0][2] == "epub"


async def test_content_upload_over_ssh(monkeypatch, temp_root):
    _register(monkeypatch, "ssh")
    client = Mock(spec=["get_meta_items", "_ssh_command", "mark_operation_dirty"])
    client.get_meta_items.return_value = []
    sent = {}

    def capture(_client, local_path, remote_path):
        with open(local_path, "rb") as f:
            sent[remote_path] = f.read()

    with (
        patch("remarkable_mcp.write_tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.write_tools._upload_file_bytes", side_effect=capture),
        patch("remarkable_mcp.write_tools._write_metadata") as metadata,
        patch("remarkable_mcp.write_tools._write_content_file"),
        patch("remarkable_mcp.write_tools._restart_xochitl"),
    ):
        data = await _upload(file_content=_b64(PDF), document_name="Week 3")

    assert data["transport"] == "ssh"
    assert data["source"] == "content"
    assert list(sent.values()) == [PDF]
    assert metadata.call_args[0][2]["visibleName"] == "Week 3"


async def test_content_upload_over_usb_web(monkeypatch, temp_root):
    _register(monkeypatch, "usb")
    sent = {}

    def capture(local_path, document_name):
        with open(local_path, "rb") as f:
            sent["data"] = f.read()
        sent["name"] = document_name
        return {"status": 200, "ok": True}

    client = Mock(spec=["_documents", "_documents_by_id"])
    with (
        patch("remarkable_mcp.write_tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.write_tools._upload_via_usb_web", side_effect=capture),
    ):
        data = await _upload(file_content=_b64(PDF), document_name="Week 3")

    assert data["transport"] == "usb-web"
    assert data["source"] == "content"
    assert sent == {"data": PDF, "name": "Week 3"}


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"file_content": "not base64!!", "document_name": "D"}, "not valid base64"),
        ({"file_content": _b64(b"hello world"), "document_name": "D"}, "not a PDF"),
        (
            {"file_content": _b64(PDF), "document_name": "D", "file_format": "epub"},
            "not an EPUB",
        ),
        ({"file_content": _b64(PDF), "document_name": "D", "file_format": "docx"}, "docx"),
        ({"file_content": _b64(PDF)}, "document_name is required"),
        ({"file_content": _b64(PDF), "document_name": "  "}, "document_name is required"),
        ({"file_content": "", "document_name": "D"}, "empty"),
        ({"file_content": _b64(PDF), "file_path": "/tmp/x.pdf"}, "exactly one"),
        ({"document_name": "D"}, "exactly one"),
    ],
)
async def test_bad_content_rejected(monkeypatch, temp_root, arguments, message):
    _register(monkeypatch)
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(**arguments)

    assert data["_error"]["type"] == "invalid_parameter"
    assert message in data["_error"]["message"]
    client.upload_document.assert_not_called()


async def test_oversized_content_rejected(monkeypatch, temp_root):
    _register(monkeypatch)
    monkeypatch.setenv("REMARKABLE_MAX_INLINE_BYTES", "1000")
    client = _cloud_client()
    big = PDF + b"\x00" * 1000
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_content=_b64(big), document_name="Big")
        at_limit = await _upload(file_content=_b64(PDF + b"\x00" * 900), document_name="Ok")

    assert "1000-byte inline limit" in data["_error"]["message"]
    assert "file_path" in data["_error"]["message"]
    assert at_limit["uploaded"] is True
    assert client.upload_document.call_count == 1


async def test_failed_content_upload_leaves_no_temp_file(monkeypatch, temp_root):
    _register(monkeypatch)
    client = _cloud_client()
    client.upload_document.side_effect = RuntimeError("network down")
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_content=_b64(PDF), document_name="Doc")

    assert data["_error"]["type"] == "upload_failed"


async def test_path_upload_reports_source_and_keeps_file(monkeypatch, tmp_path):
    _register(monkeypatch)
    source = tmp_path / "paper.pdf"
    source.write_bytes(PDF)
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_path=str(source))

    assert data["uploaded"] is True
    assert data["name"] == "paper"
    assert data["source"] == "path"
    assert data["size_bytes"] == len(PDF)
    assert source.read_bytes() == PDF


@pytest.fixture
def staging(tmp_path, monkeypatch):
    """A staging folder with one PDF; send2trash is recorded, not run."""
    folder = tmp_path / "Tablet Uploads"
    folder.mkdir()
    (folder / "week3.pdf").write_bytes(PDF)
    trashed = []
    monkeypatch.setattr("send2trash.send2trash", lambda path: trashed.append(path))
    _register(monkeypatch)
    monkeypatch.setenv("REMARKABLE_UPLOAD_STAGING_DIR", str(folder))
    mcp._tool_manager._tools.pop("remarkable_upload", None)
    from remarkable_mcp.write_tools import register_write_tools

    register_write_tools()
    return SimpleNamespace(folder=folder, source=folder / "week3.pdf", trashed=trashed)


async def _upload_tool():
    return next(t for t in await mcp.list_tools() if t.name == "remarkable_upload")


async def test_trash_option_hidden_without_staging_folder(monkeypatch):
    _register(monkeypatch)
    tool = await _upload_tool()

    assert "trash_source_after_upload" not in tool.input_schema["properties"]
    assert "Trash" not in tool.description
    assert not tool.annotations.destructive_hint


async def test_trash_option_offered_with_staging_folder(staging):
    tool = await _upload_tool()

    assert tool.input_schema["properties"]["trash_source_after_upload"]["default"] is False
    assert str(staging.folder) in tool.description
    assert tool.annotations.destructive_hint is True


async def test_staged_source_trashed_after_upload(staging):
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_path=str(staging.source), trash_source_after_upload=True)

    assert data["uploaded"] is True
    assert data["source_trashed"] is True
    assert staging.trashed == [os.path.realpath(staging.source)]


async def test_staged_source_kept_by_default(staging):
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_path=str(staging.source))

    assert data["uploaded"] is True
    assert "source_trashed" not in data
    assert staging.trashed == []


async def test_staged_source_kept_when_upload_fails(staging):
    client = _cloud_client()
    client.upload_document.side_effect = RuntimeError("network down")
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_path=str(staging.source), trash_source_after_upload=True)

    assert data["_error"]["type"] == "upload_failed"
    assert staging.trashed == []
    assert staging.source.exists()


async def test_staged_source_kept_when_changed_during_upload(staging):
    client = _cloud_client()

    def upload_and_replace(*args):
        staging.source.write_bytes(PDF + b"% edited")
        return Mock(id="new-doc-id")

    client.upload_document.side_effect = upload_and_replace
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_path=str(staging.source), trash_source_after_upload=True)

    assert data["uploaded"] is True
    assert data["source_trashed"] is False
    assert "changed" in data["source_kept_reason"]
    assert staging.trashed == []


async def test_staged_source_kept_when_trash_fails(staging, monkeypatch):
    def refuse(path):
        raise OSError("Trash unavailable")

    monkeypatch.setattr("send2trash.send2trash", refuse)
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_path=str(staging.source), trash_source_after_upload=True)

    assert data["uploaded"] is True
    assert data["source_trashed"] is False
    assert "Trash unavailable" in data["source_kept_reason"]
    assert staging.source.exists()


async def test_trash_refused_outside_staging_folder(staging, tmp_path):
    outside = tmp_path / "course.pdf"
    outside.write_bytes(PDF)
    nested = staging.folder / "sub"
    nested.mkdir()
    (nested / "deep.pdf").write_bytes(PDF)
    link = staging.folder / "link.pdf"
    link.symlink_to(outside)
    staged_link = staging.folder / "alias.pdf"
    staged_link.symlink_to(staging.source)
    client = _cloud_client()

    attempts = {
        "outside": str(outside),
        "dotted": str(staging.folder / ".." / "course.pdf"),
        "subfolder": str(nested / "deep.pdf"),
        "symlink out": str(link),
        "symlink in": str(staged_link),
        "folder": str(staging.folder),
    }
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        for label, path in attempts.items():
            data = await _upload(file_path=path, trash_source_after_upload=True)
            assert data["_error"]["type"] == "invalid_parameter", label

    client.upload_document.assert_not_called()
    assert staging.trashed == []
    assert outside.exists() and staging.source.exists()


async def test_trash_refused_for_content_uploads(staging):
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(
            file_content=_b64(PDF), document_name="Doc", trash_source_after_upload=True
        )

    assert "only to file_path" in data["_error"]["message"]
    client.upload_document.assert_not_called()


async def test_trash_refused_when_staging_folder_unset_later(staging, monkeypatch):
    monkeypatch.delenv("REMARKABLE_UPLOAD_STAGING_DIR")
    client = _cloud_client()
    with patch("remarkable_mcp.write_tools.get_rmapi", return_value=client):
        data = await _upload(file_path=str(staging.source), trash_source_after_upload=True)

    assert "REMARKABLE_UPLOAD_STAGING_DIR" in data["_error"]["message"]
    assert staging.trashed == []

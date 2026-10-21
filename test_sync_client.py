"""The cloud sync client against an HTTP-level fake of the reMarkable cloud.

The fake serves the sync API's blob store and generation-gated root, so these
tests run the client's real request, retry, upload and commit code.
"""

import base64
import hashlib
import io
import json
import zipfile

import pymupdf
import pytest
import requests

from remarkable_mcp import sync
from remarkable_mcp.sync import CloudWriteError, RemarkableClient


class Response:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeCloud:
    """The sync API: blobs by hash, a root hash and a generation."""

    def __init__(self):
        self.blobs = {}
        empty = b"3\n"
        self.root = hashlib.sha256(empty).hexdigest()
        self.blobs[self.root] = empty
        self.generation = 1
        self.requests = []
        self.conflicts = 0  # root commits to refuse before accepting
        self.failures = []  # statuses to answer before handling a request
        self.user_token = "user-1"

    def handle(self, method, url, headers=None, json=None, data=None, **_):
        self.requests.append((method, url))
        if self.failures:
            status = self.failures.pop(0)
            if isinstance(status, Exception):
                raise status
            return Response(status, b"busy", {"Retry-After": "0"})
        auth = (headers or {}).get("Authorization", "")
        if url == sync.USER_TOKEN_URL:
            return Response(200, self.user_token.encode())
        if auth != f"Bearer {self.user_token}":
            return Response(401)
        if url == sync.ROOT_URL and method == "GET":
            body = {"hash": self.root, "generation": self.generation}
            return Response(200, json_bytes(body))
        if url == sync.ROOT_PUT_URL and method == "PUT":
            if self.conflicts:
                self.conflicts -= 1
                self.generation += 1  # someone else committed
                return Response(412)
            if json["generation"] != self.generation:
                return Response(412)
            assert json["hash"] in self.blobs, "root committed before its blob"
            self.root = json["hash"]
            self.generation += 1
            return Response(200, json_bytes({"hash": self.root, "generation": self.generation}))
        if url.startswith(sync.FILES_URL + "/"):
            blob = url.rsplit("/", 1)[1]
            if method == "GET":
                return Response(200, self.blobs[blob]) if blob in self.blobs else Response(404)
            if method == "PUT":
                crc = headers["x-goog-hash"]
                assert crc == sync._crc32c_header(data), "bad CRC32C header"
                self.blobs[blob] = data
                return Response(200)
        return Response(404)


def json_bytes(value) -> bytes:
    return json.dumps(value).encode()


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(sync, "_issue_request", fake.handle)
    monkeypatch.setattr(sync.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("REMARKABLE_SYNC_WORKERS", "1")
    return fake


@pytest.fixture
def client(cloud):
    return RemarkableClient(device_token="device-1", user_token=cloud.user_token)


def _pdf(text="Hello") -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def _names(client):
    return sorted(d.VissibleName for d in client.get_meta_items())


def test_upload_list_download_round_trip(cloud, client):
    pdf = _pdf()
    doc = client.upload_document(pdf, "Worksheet", "pdf")

    assert _names(client) == ["Worksheet"]
    listed = client.get_doc(doc.ID)
    assert listed.VissibleName == "Worksheet" and not listed.is_folder
    assert client.get_file_type(listed) == "pdf"
    assert client.download_raw_file(listed, "pdf") == pdf
    with zipfile.ZipFile(io.BytesIO(client.download(listed))) as archive:
        assert any(name.endswith(".pdf") for name in archive.namelist())
    assert cloud.generation == 2


def test_folders_rename_move_delete_and_restore(cloud, client):
    folder = client.create_folder("Archive")
    doc = client.upload_document(_pdf(), "Notes", "pdf")
    client.rename(doc.ID, "Notes v2")
    client.move(doc.ID, folder.ID)
    items = {d.ID: d for d in client.get_meta_items()}
    assert items[doc.ID].VissibleName == "Notes v2" and items[doc.ID].Parent == folder.ID
    assert items[folder.ID].is_folder

    client.delete(doc.ID)
    assert {d.ID: d for d in client.get_meta_items()}[doc.ID].Parent == "trash"
    client.restore(doc.ID)
    assert {d.ID: d for d in client.get_meta_items()}[doc.ID].Parent == ""
    assert cloud.generation == 7  # one commit per change


def test_metadata_changes_bump_the_version(cloud, client):
    doc = client.upload_document(_pdf(), "Notes", "pdf")
    client.rename(doc.ID, "Renamed")
    entries, _ = client._read_root_entries()
    entry = next(e for e in entries if e["id"] == doc.ID)
    files = client._parse_index(client._get_file(entry["hash"], f"{doc.ID}.docSchema"))
    meta_file = next(f for f in files if f["id"].endswith(".metadata"))
    meta = json.loads(client._get_file(meta_file["hash"], meta_file["id"]))
    assert meta["version"] >= 2 and meta["metadatamodified"] is True


def test_a_generation_conflict_is_retried_on_the_new_root(cloud, client):
    client.upload_document(_pdf(), "First", "pdf")
    cloud.conflicts = 2
    client.upload_document(_pdf("second"), "Second", "pdf")

    assert _names(client) == ["First", "Second"]
    commits = [r for r in cloud.requests if r == ("PUT", sync.ROOT_PUT_URL)]
    assert len(commits) == 4  # one for First, three for Second


def test_conflicts_that_never_end_raise(cloud, client):
    cloud.conflicts = 99
    with pytest.raises(CloudWriteError, match="generation conflict on all"):
        client.create_folder("Never")
    commits = [r for r in cloud.requests if r == ("PUT", sync.ROOT_PUT_URL)]
    assert len(commits) == sync._ROOT_COMMIT_ATTEMPTS


def test_a_mismatched_commit_reply_raises(cloud, client, monkeypatch):
    original = cloud.handle

    def handle(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if url == sync.ROOT_PUT_URL and response.ok:
            return Response(200, json_bytes({"hash": "other", "generation": 9}))
        return response

    monkeypatch.setattr(sync, "_issue_request", handle)
    with pytest.raises(CloudWriteError, match="mismatched"):
        client.create_folder("X")


def test_busy_responses_and_dropped_connections_are_retried(cloud, client):
    cloud.failures = [429, requests.ConnectionError("reset")]
    assert _names(client) == []
    assert len([r for r in cloud.requests if r[1] == sync.ROOT_URL]) == 3


def test_an_expired_token_is_renewed_once(cloud, client):
    client.user_token = "expired"
    assert _names(client) == []
    assert client.user_token == "user-1"
    assert ("POST", sync.USER_TOKEN_URL) in cloud.requests


def test_retries_give_up_with_the_last_error(cloud, client):
    cloud.failures = [requests.ConnectionError("down")] * 5
    with pytest.raises(requests.ConnectionError):
        client.get_meta_items()


def test_blobs_are_served_from_the_cache_after_one_fetch(cloud, client):
    client.upload_document(_pdf(), "Notes", "pdf")
    fresh = RemarkableClient(device_token="device-1", user_token=cloud.user_token)
    fresh.get_meta_items()
    fetched = len([r for r in cloud.requests if r[0] == "GET"])
    fresh.get_meta_items()
    again = len([r for r in cloud.requests if r[0] == "GET"]) - fetched
    assert again == 1  # only the root hash; every blob came from the cache


def test_crc32c_matches_the_known_value():
    # RFC 3720 test vector: 32 zero bytes have CRC32C 0x8A9136AA.
    assert sync._crc32c_value(bytes(32)) == 0x8A9136AA
    assert base64.b64decode(sync._crc32c_header(bytes(32)).split("=", 1)[1]) == bytes.fromhex(
        "8A9136AA"
    )


@pytest.mark.parametrize(
    "token, device",
    [
        ('{"devicetoken": "eyJdev", "usertoken": "u"}', "eyJdev"),
        ("eyJlegacy.jwt.token", "eyJlegacy.jwt.token"),
    ],
)
def test_token_formats(token, device):
    assert sync.load_client_from_token(token).device_token == device


def test_bad_tokens_are_refused(tmp_path):
    with pytest.raises(ValueError, match="Invalid token format"):
        sync.load_client_from_token("nonsense")
    with pytest.raises(RuntimeError, match="Token file not found"):
        sync.load_client_from_file(tmp_path / "missing")


def test_device_registration(monkeypatch):
    monkeypatch.setattr(sync, "_issue_request", lambda *a, **k: Response(200, b"device-token\n"))
    assert sync.register_device("abcd1234") == {"devicetoken": "device-token", "usertoken": ""}
    monkeypatch.setattr(sync, "_issue_request", lambda *a, **k: Response(400))
    with pytest.raises(RuntimeError, match="Registration failed"):
        sync.register_device("used")


@pytest.mark.parametrize("workers", ["1", "4"])
def test_failed_blob_fetch_raises_instead_of_partial_zip(monkeypatch, workers):
    monkeypatch.setenv("REMARKABLE_SYNC_WORKERS", workers)
    index = b"3\nh-content:0:d.content:0:2\nh-page:0:d/p.rm:0:2\n"
    blobs = {"doc-hash": index, "h-content": b"{}"}

    def get_file(file_hash, file_name):
        if file_hash not in blobs:
            raise requests.HTTPError("404 Not Found")
        return blobs[file_hash]

    client = RemarkableClient(user_token="u")
    client._get_file = get_file
    doc = sync.Document(id="d", hash="doc-hash", name="Doc", doc_type="DocumentType")

    with pytest.raises(RuntimeError, match=r"d/p\.rm of 'Doc'"):
        client.download(doc)


def test_failed_index_fetch_raises():
    client = RemarkableClient(user_token="u")

    def get_file(file_hash, file_name):
        raise requests.ConnectionError("offline")

    client._get_file = get_file
    doc = sync.Document(id="d", hash="doc-hash", name="Doc", doc_type="DocumentType")

    with pytest.raises(RuntimeError, match="file index of 'Doc'"):
        client.download(doc)

"""
Write tools for reMarkable tablet via cloud, SSH, or USB web interface.

The local-directory transport is always read-only and never registers these tools.

These tools are enabled by default. Disable them (expose a read-only server) via:
- CLI flag: remarkable-mcp --read-only  (works in any transport)
- Environment variable: REMARKABLE_READ_ONLY=1

The legacy --write flag and REMARKABLE_ENABLE_WRITE variable are still accepted
for backward compatibility but are now no-ops (write is the default). --write and
--read-only are mutually exclusive.

Write support by transport:
- Cloud mode: upload, mkdir, move, rename, delete (-> trash)
- SSH mode:   upload, mkdir, move, rename, delete
- USB web mode: upload only (via POST /upload endpoint)

Inspired by PR #70 from @McSchnizzle. SSH operations use the tablet filesystem,
USB uses the web upload endpoint, and cloud uses the sync v3/v4 blob protocol —
no external Go binary required.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import socket
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional, Union

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from remarkable_mcp.api import (
    get_item_path,
    get_items_by_id,
    get_rmapi,
)
from remarkable_mcp.capabilities import client_supports_elicitation
from remarkable_mcp.pages import content_page_ids
from remarkable_mcp.responses import make_error, make_response
from remarkable_mcp.ssh import XOCHITL_PATH, Document, SSHClient
from remarkable_mcp.ssh_reliability import (
    SSHExecutionUnknownError,
    SSHPreExecutionError,
    SSHReliabilityError,
)

logger = logging.getLogger(__name__)
_write_context = threading.local()

# Tool annotations for write operations
WRITE_ANNOTATIONS = ToolAnnotations(read_only_hint=False)
UPLOAD_ANNOTATIONS = WRITE_ANNOTATIONS
MKDIR_ANNOTATIONS = WRITE_ANNOTATIONS
MOVE_ANNOTATIONS = WRITE_ANNOTATIONS
RENAME_ANNOTATIONS = WRITE_ANNOTATIONS
DELETE_ANNOTATIONS = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def read_only_enabled() -> bool:
    """Check if read-only mode is enabled via the --read-only flag / env var."""
    return os.environ.get("REMARKABLE_READ_ONLY", "").lower() in (
        "1",
        "true",
        "yes",
    )


def write_enabled() -> bool:
    """Write tools are enabled by default; disabled only in read-only mode.

    The local-directory transport is unconditionally read-only: the directory
    is the desktop app's private sync cache, and writing to it directly would
    bypass sync and could corrupt the app's state.
    """
    if _is_local_dir_mode():
        return False
    return not read_only_enabled()


def _require_write_transport() -> Optional[str]:
    """Return an error string if writes are disabled, else None.

    Upload works in the three write-capable transports (cloud, SSH, USB web).
    Write tools are not registered in read-only or local-directory mode, so this
    is mostly a defensive check that returns a clear error if writes are somehow
    disabled.
    """
    if not write_enabled():
        return make_error(
            error_type="write_disabled",
            message="Write operations are disabled (read-only mode)",
            suggestion=(
                "Write tools are enabled by default. Remove the --read-only flag "
                "(or unset REMARKABLE_READ_ONLY) and restart the server to enable "
                "upload, mkdir, move, rename, and delete."
            ),
        )
    return None


def _is_ssh_mode() -> bool:
    """Check if SSH transport is active."""
    return os.environ.get("REMARKABLE_USE_SSH", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _is_usb_web_mode() -> bool:
    """Check if USB web transport is active."""
    return os.environ.get("REMARKABLE_USE_USB_WEB", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _is_local_dir_mode() -> bool:
    """Check if the local-directory transport is active."""
    return os.environ.get("REMARKABLE_USE_LOCAL_DIR", "").lower() in (
        "1",
        "true",
        "yes",
    ) or bool(os.environ.get("REMARKABLE_LOCAL_DIR"))


def _is_cloud_mode() -> bool:
    """Check if cloud transport is active (the default when other modes are off)."""
    return not _is_ssh_mode() and not _is_usb_web_mode() and not _is_local_dir_mode()


def _require_managed_write_mode() -> Optional[str]:
    """Return an error string unless in a mode that supports folder operations.

    mkdir, move, rename and delete work in cloud and SSH modes. USB web only
    supports uploads, while local-directory mode is read-only.
    """
    if _is_cloud_mode() or _is_ssh_mode():
        return None
    return make_error(
        error_type="unsupported_in_usb_web",
        message="This operation isn't available in USB web mode",
        suggestion=(
            "mkdir, move, rename and delete work in cloud mode (the "
            "default) and SSH mode. USB web supports upload only.\n"
            "Run with: remarkable-mcp  (cloud)  or  remarkable-mcp --ssh"
        ),
    )


def _get_ssh_client():
    """Get the current API client (expected to be SSHClient in SSH mode)."""
    return get_rmapi()


def _write_metadata(ssh_client: SSHClient, doc_uuid: str, metadata: dict) -> None:
    """Write a metadata JSON file to the tablet."""
    content = json.dumps(metadata, indent=2)
    remote_path = f"{XOCHITL_PATH}/{doc_uuid}.metadata"
    ssh_client._ssh_command(f"cat > '{remote_path}' << 'REMARKABLE_EOF'\n{content}\nREMARKABLE_EOF")
    ssh_client.mark_operation_dirty()


def _write_content_file(ssh_client: SSHClient, doc_uuid: str, content_data: dict) -> None:
    """Write a .content JSON file to the tablet."""
    content = json.dumps(content_data, indent=2)
    remote_path = f"{XOCHITL_PATH}/{doc_uuid}.content"
    ssh_client._ssh_command(f"cat > '{remote_path}' << 'REMARKABLE_EOF'\n{content}\nREMARKABLE_EOF")
    ssh_client.mark_operation_dirty()


def _permanently_delete_ssh_entry(ssh_client: SSHClient, doc_uuid: str) -> None:
    """Remove one resolved UUID and all of its sidecar files from the tablet."""
    entry = shlex.quote(f"{XOCHITL_PATH}/{doc_uuid}")
    sidecar_prefix = shlex.quote(f"{XOCHITL_PATH}/{doc_uuid}.")
    ssh_client._ssh_command(f"rm -rf {entry} {sidecar_prefix}*")
    ssh_client.mark_operation_dirty()


def _upload_file_bytes(ssh_client: SSHClient, local_path: str, remote_path: str) -> None:
    """Upload a local file to the tablet via SSH stdin pipe."""
    ssh_client._upload_file(local_path, remote_path)
    ssh_client.mark_operation_dirty()


# Tunables for the post-restart readiness wait (see _restart_xochitl). All are
# overridable by env var so the wait can be tightened or relaxed per device
# without code changes.
_RESTART_READY_TIMEOUT = float(os.environ.get("REMARKABLE_RESTART_TIMEOUT", "60"))
_RESTART_POLL_INTERVAL = float(os.environ.get("REMARKABLE_RESTART_POLL_INTERVAL", "1"))
_RESTART_SETTLE_SECONDS = float(os.environ.get("REMARKABLE_RESTART_SETTLE", "3"))
_RESTART_WEB_PORT = 80
_RESTART_WEB_PROBE_TIMEOUT = 1.0


def _tcp_port_open(host: str, port: int, timeout: float) -> bool:
    """Return whether a fresh TCP connection reaches ``host:port``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_xochitl_ready(
    ssh_client: SSHClient,
    expected_boot_id: str,
    require_web: bool,
    web_host: str,
) -> tuple[bool, str]:
    """Probe xochitl through a fresh SSH session and optional USB web socket."""
    try:
        boot_id = ssh_client.probe_fresh_session(
            "cat /proc/sys/kernel/random/boot_id",
            timeout=10,
        ).strip()
    except RuntimeError as exc:
        return False, f"fresh SSH boot-ID probe failed: {exc}"
    if boot_id != expected_boot_id:
        raise RuntimeError(
            "Tablet boot ID changed after the xochitl restart "
            f"({expected_boot_id} -> {boot_id or 'unknown'}). The firmware emergency "
            "target rebooted the device, commonly after xochitl start-limit-hit. "
            "Do not repeat writes; wake and unlock the tablet before recovery."
        )

    try:
        state = ssh_client.probe_fresh_session(
            "systemctl is-active xochitl",
            timeout=10,
        ).strip()
    except RuntimeError as exc:
        return False, f"xochitl status probe failed in a fresh SSH session: {exc}"
    if state != "active":
        return False, f"xochitl state is {state or 'unknown'}"
    if require_web and not _tcp_port_open(
        web_host,
        _RESTART_WEB_PORT,
        _RESTART_WEB_PROBE_TIMEOUT,
    ):
        return False, f"USB web port {_RESTART_WEB_PORT} is not accepting connections"
    return True, "fresh SSH session and xochitl are ready"


def _restart_xochitl(ssh_client: SSHClient, wait_ready: bool = True) -> None:
    """Restart the xochitl UI service so it picks up metadata changes.

    The entire sequence runs under the serialized SSH dispatcher. It verifies
    xochitl is healthy, records the tablet boot ID, clears only xochitl's
    systemd start counter, and then restarts it. Readiness requires two fresh
    SSH sessions separated by a settle interval, an unchanged boot ID, and
    port 80 recovery when USB web was reachable before the restart.

    This prevents Paper Pro firmware's four-starts-per-ten-minutes limit from
    escalating a controlled restart into emergency.target and a device reboot.
    Pass ``wait_ready=False`` only for bounded shutdown cleanup.
    """
    runner = getattr(ssh_client, "run_operation", None)
    in_dispatcher = getattr(ssh_client, "in_dispatcher_thread", lambda: False)
    if callable(runner) and not in_dispatcher():
        runner(
            "xochitl-refresh",
            lambda: _restart_xochitl_locked(ssh_client, wait_ready),
        )
        return
    _restart_xochitl_locked(ssh_client, wait_ready)


def _restart_xochitl_locked(ssh_client: SSHClient, wait_ready: bool) -> None:
    """Run restart/readiness/settle while excluding every other SSH operation."""
    try:
        initial_state = ssh_client._ssh_command(
            "systemctl is-active xochitl",
            timeout=10,
        ).strip()
    except RuntimeError as exc:
        raise RuntimeError(f"xochitl pre-restart status probe failed: {exc}") from exc
    if initial_state != "active":
        raise RuntimeError(
            "Refusing to restart xochitl because it is not currently active "
            f"(state: {initial_state or 'unknown'})."
        )

    try:
        boot_id = ssh_client._ssh_command(
            "cat /proc/sys/kernel/random/boot_id",
            timeout=10,
        ).strip()
    except RuntimeError as exc:
        raise RuntimeError(f"xochitl pre-restart boot-ID probe failed: {exc}") from exc
    if not boot_id:
        raise RuntimeError("xochitl pre-restart boot-ID probe returned an empty value")

    web_host = ssh_client.host
    require_web = False
    if wait_ready:
        try:
            web_host = ssh_client.effective_host()
        except RuntimeError as exc:
            raise RuntimeError(f"xochitl pre-restart host resolution failed: {exc}") from exc
        require_web = _tcp_port_open(
            web_host,
            _RESTART_WEB_PORT,
            _RESTART_WEB_PROBE_TIMEOUT,
        )
    logger.info("Restarting xochitl; require_web=%s", require_web)

    # Stock Paper Pro firmware limits xochitl to four starts per ten minutes and
    # maps start-limit failure to emergency.target, whose handler reboots the
    # tablet. Clear only xochitl's manual-start counter before our controlled
    # restart so repeated write batches cannot cross that firmware tripwire.
    try:
        ssh_client._ssh_command("systemctl reset-failed xochitl", timeout=10)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Failed to clear xochitl's systemd start limiter before restart: {exc}"
        ) from exc
    try:
        ssh_client._ssh_command("systemctl restart xochitl", timeout=30)
    except RuntimeError as exc:
        detail = str(exc)
        if any(
            marker in detail.lower()
            for marker in ("start-limit-hit", "start request repeated", "attempted too often")
        ):
            raise RuntimeError(
                "xochitl restart was rejected with start-limit-hit. Stock firmware "
                "routes this failure to emergency.target and may reboot the tablet. "
                "Do not repeat writes; wake and unlock the tablet before recovery."
            ) from exc
        raise RuntimeError(f"xochitl restart command failed: {detail}") from exc
    if not wait_ready:
        return

    deadline = time.monotonic() + _RESTART_READY_TIMEOUT
    last_detail = "restart command completed"
    while time.monotonic() < deadline:
        ready, last_detail = _probe_xochitl_ready(
            ssh_client,
            boot_id,
            require_web,
            web_host,
        )
        if ready:
            remaining = deadline - time.monotonic()
            if _RESTART_SETTLE_SECONDS > remaining:
                break
            if _RESTART_SETTLE_SECONDS > 0:
                time.sleep(_RESTART_SETTLE_SECONDS)
            confirmed, last_detail = _probe_xochitl_ready(
                ssh_client,
                boot_id,
                require_web,
                web_host,
            )
            if confirmed:
                logger.info("xochitl restart readiness confirmed; require_web=%s", require_web)
                return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_RESTART_POLL_INTERVAL, remaining))

    required_state = "fresh SSH sessions and active xochitl"
    if require_web:
        required_state += f", plus recovered USB web port {_RESTART_WEB_PORT}"
    raise RuntimeError(
        f"xochitl did not sustain {required_state} within "
        f"{_RESTART_READY_TIMEOUT:g}s after restart ({last_detail}). "
        "Do not repeat writes; wake and unlock the tablet before recovery."
    )


def _defer_restart_enabled() -> bool:
    """Whether xochitl restarts should be deferred by default (env override).

    Set REMARKABLE_DEFER_RESTART=1 to make every write skip its own restart, so
    a batch of writes pays the restart cost (and its race surface) exactly once
    via remarkable_refresh instead of once per write.
    """
    return os.environ.get("REMARKABLE_DEFER_RESTART", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _maybe_restart_xochitl(ssh_client: SSHClient, defer_restart: bool = False) -> bool:
    """Restart xochitl unless deferral is requested (per-call arg or env).

    Returns True if a restart was performed, False if it was deferred. When
    deferred the on-disk metadata has changed but xochitl has not yet rescanned,
    so the caller must run remarkable_refresh once the batch is complete for the
    changes to become visible in the UI.
    """
    if (
        defer_restart
        or _defer_restart_enabled()
        or getattr(_write_context, "suppress_restart", False)
    ):
        return False
    _restart_xochitl(ssh_client)
    return True


@contextmanager
def _suppress_automatic_restart():
    previous = getattr(_write_context, "suppress_restart", False)
    _write_context.suppress_restart = True
    try:
        yield
    finally:
        _write_context.suppress_restart = previous


def _response_persisted(response: str) -> bool:
    try:
        data = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        return False
    return "_error" not in data


def _validate_author_request(
    method: str,
    document: Optional[str],
    page: Optional[int],
    strokes: Optional[list],
    name: Optional[str],
) -> Optional[str]:
    """Return transport-free validation errors before resolving an SSH client."""
    if method == "draw":
        if not document or page is None:
            return make_error(
                error_type="missing_parameter",
                message="draw requires 'document' and 'page'.",
                suggestion=(
                    'Call remarkable_author(method="draw", document=..., page=1, strokes=[...]).'
                ),
            )
        if not strokes:
            return make_error(
                error_type="no_strokes",
                message="No strokes provided to write.",
                suggestion=(
                    "Pass a non-empty list of stroke dicts, e.g. "
                    '[{"points": [[0.1,0.2],[0.8,0.2]], '
                    '"tool": "fineliner", "color": "black"}].'
                ),
            )
        return None
    if method == "add_page":
        if not document:
            return make_error(
                error_type="missing_parameter",
                message="add_page requires 'document'.",
                suggestion='Call remarkable_author(method="add_page", document=...).',
            )
        return None
    if method == "create_document":
        if not name:
            return make_error(
                error_type="missing_parameter",
                message="create_document requires 'name'.",
                suggestion=('Call remarkable_author(method="create_document", name="My notes").'),
            )
        return None
    return make_error(
        error_type="unknown_method",
        message=f"Unknown method: '{method}'.",
        suggestion='Use method="draw", "add_page", or "create_document".',
        did_you_mean=["draw", "add_page", "create_document"],
    )


def _mark_response_refreshed(response: str) -> str:
    try:
        data = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        return response
    data["refresh_pending"] = False
    hint = data.get("_hint", "")
    deferred_text = "Restart deferred — call remarkable_refresh() once your batch is done."
    if deferred_text in hint:
        hint = hint.replace(
            deferred_text,
            "Concurrent writes were applied with one completed xochitl refresh.",
        )
    data["_hint"] = hint
    return json.dumps(data, indent=2)


def _mark_response_pending(response: str) -> str:
    try:
        data = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        return response
    data["refresh_pending"] = True
    hint = data.get("_hint", "")
    if "remarkable_refresh" not in hint:
        hint = f"{hint} Restart deferred — call remarkable_refresh() once your batch is done."
    data["_hint"] = hint.strip()
    return json.dumps(data, indent=2)


def _pending_refresh_error(error_type: str, message: str, suggestion: str) -> str:
    data = json.loads(make_error(error_type, message, suggestion))
    data["refresh_pending"] = True
    return json.dumps(data, indent=2)


def _partial_write_error(
    message: str,
    *,
    refresh_pending: bool,
    cause_type: Optional[str] = None,
) -> str:
    if refresh_pending:
        recovery = (
            "Restore a fresh SSH connection and call remarkable_refresh(), then "
            "browse the tablet to inspect the result."
        )
    else:
        recovery = "The safety refresh completed; browse the tablet to inspect the result."
    data = json.loads(
        make_error(
            error_type="write_partially_persisted",
            message=f"{message} One or more mutation steps may already be on disk.",
            suggestion=f"Do not repeat the write. {recovery}",
        )
    )
    if cause_type:
        data["_error"]["cause_type"] = cause_type
    data["refresh_pending"] = refresh_pending
    return json.dumps(data, indent=2)


def _partial_response_error(response: str, *, refresh_pending: bool) -> str:
    try:
        data = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        return _partial_write_error(
            "The write returned an unreadable error after a mutation step completed.",
            refresh_pending=refresh_pending,
        )
    error = data.get("_error")
    if not isinstance(error, dict):
        return response
    return _partial_write_error(
        error.get("message", "The write failed after a mutation step completed."),
        refresh_pending=refresh_pending,
        cause_type=error.get("type"),
    )


async def _refresh_ssh_client(ssh_client: SSHClient) -> None:
    try:
        await ssh_client.run_refresh_operation_async(
            "xochitl-refresh",
            lambda: _restart_xochitl(ssh_client, wait_ready=not ssh_client.is_closing()),
        )
    except BaseException:
        _invalidate_client_cache(ssh_client)
        raise
    _invalidate_client_cache(ssh_client)


async def _run_ssh_tool_operation(
    ssh_client: SSHClient,
    operation: str,
    implementation,
    *,
    defer_restart: bool = False,
) -> str:
    deferred = defer_restart or _defer_restart_enabled()
    operation_dirty = False

    def observe_dirty(dirty: bool) -> None:
        nonlocal operation_dirty
        operation_dirty = dirty

    def mutation() -> str:
        with _suppress_automatic_restart():
            return implementation()

    try:
        response, refreshed = await ssh_client._refresh_coordinator.run_write(
            lambda: ssh_client.run_operation_async(operation, mutation),
            lambda: _refresh_ssh_client(ssh_client),
            deferred=deferred,
            persisted=_response_persisted,
            observe_dirty=observe_dirty,
        )
    except Exception as e:
        _invalidate_client_cache(ssh_client)
        refresh_pending = ssh_client._refresh_coordinator.diagnostics()["deferred_dirty"]
        if getattr(e, "_remarkable_refresh_failed", False):
            return _pending_refresh_error(
                error_type="write_persisted_refresh_failed",
                message=f"Write persisted but the shared refresh failed: {e}",
                suggestion=(
                    "Do not repeat the write. Restore the SSH connection and call "
                    "remarkable_refresh() to make the pending change visible."
                ),
            )
        if operation_dirty:
            return _partial_write_error(
                str(e),
                refresh_pending=refresh_pending,
                cause_type=(
                    "ssh_unavailable" if isinstance(e, SSHPreExecutionError) else type(e).__name__
                ),
            )
        if isinstance(e, SSHPreExecutionError):
            response = make_error(
                error_type="ssh_unavailable",
                message=str(e),
                suggestion="Wake and unlock the tablet, verify the USB cable, then try again.",
            )
        elif isinstance(e, SSHExecutionUnknownError):
            response = make_error(
                error_type="write_execution_unknown",
                message=str(e),
                suggestion=(
                    "Do not repeat the write automatically. Run remarkable_refresh(), "
                    "then browse the tablet to determine whether the change was applied."
                ),
            )
        elif isinstance(e, SSHReliabilityError):
            response = make_error(
                error_type="ssh_operation_failed",
                message=str(e),
                suggestion="Check the SSH connection and inspect the tablet before retrying.",
            )
        else:
            response = make_error(
                error_type="write_failed",
                message=f"Write failed before any mutation was confirmed: {e}",
                suggestion="Check the tablet state and try again.",
            )
        return (
            _mark_response_pending(response)
            if refresh_pending
            else _mark_response_refreshed(response)
        )
    refresh_pending = ssh_client._refresh_coordinator.diagnostics()["deferred_dirty"]
    if operation_dirty and not _response_persisted(response):
        return _partial_response_error(response, refresh_pending=refresh_pending)
    if refreshed:
        if refresh_pending:
            return _mark_response_pending(response)
        return _mark_response_refreshed(response)
    return (
        _mark_response_pending(response) if refresh_pending else _mark_response_refreshed(response)
    )


def _write_result_message(
    restarted: bool,
    done: str,
    verify: str = "Use remarkable_browse() to verify.",
) -> str:
    """Build a write tool's success message, accounting for restart deferral.

    ``done`` is the past-tense summary of what happened (e.g. "Document
    moved."). When the restart ran, ``verify`` tells the user how to confirm the
    change; when it was deferred, the user is instead reminded to call
    remarkable_refresh once their batch is complete.
    """
    if restarted:
        return f"{done} {verify}"
    return f"{done} Restart deferred — call remarkable_refresh() once your batch is done."


def _page_ids_from_content(content_data: dict) -> list:
    """Return ordered page ids from a parsed ``.content`` file.

    Handles both the modern ``cPages.pages[].id`` schema (native notebooks) and
    the legacy flat ``pages`` UUID list (PDF/EPUB-backed documents). Pages
    deleted on the tablet are skipped.
    """
    return content_page_ids(content_data)


def _read_remote_bytes(ssh_client: SSHClient, remote_path: str) -> Optional[bytes]:
    """Download a remote file's bytes, or None if it does not exist / fails."""
    try:
        return ssh_client._scp_download(remote_path)
    except SSHReliabilityError:
        raise
    except Exception as e:
        logger.debug(f"Remote read failed for {remote_path}: {e}")
        return None


def _remote_file_exists(ssh_client: SSHClient, remote_path: str) -> bool:
    """Return True if a file exists on the tablet."""
    out = ssh_client._ssh_command(f"test -f '{remote_path}' && echo yes || echo no")
    return out.strip().endswith("yes")


def _write_remote_bytes(ssh_client: SSHClient, remote_path: str, data: bytes) -> None:
    """Write bytes to a remote path by staging a local temp file and uploading."""
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        _upload_file_bytes(ssh_client, tmp_path, remote_path)
    finally:
        os.unlink(tmp_path)


def _upload_filename(local_path: str, document_name: Optional[str] = None) -> str:
    """Return the multipart filename used by USB web uploads."""
    local_filename = os.path.basename(local_path)
    if not document_name or not document_name.strip():
        return local_filename

    requested = os.path.basename(document_name.strip().replace("\\", "/"))
    if not requested:
        return local_filename
    extension = os.path.splitext(local_filename)[1]
    if extension and not requested.lower().endswith(extension.lower()):
        requested += extension
    return requested


def _upload_via_usb_web(local_path: str, document_name: Optional[str] = None) -> dict:
    """Upload a file to the tablet via USB web interface POST /upload.

    Returns dict with upload result info.
    """
    import requests

    from remarkable_mcp.usb_web import DEFAULT_USB_HOST

    host = os.environ.get("REMARKABLE_USB_HOST", DEFAULT_USB_HOST).rstrip("/")
    url = f"{host}/upload"

    with open(local_path, "rb") as f:
        filename = _upload_filename(local_path, document_name)
        files = {"file": (filename, f)}
        try:
            response = requests.post(url, files=files, timeout=120)
            response.raise_for_status()
            return {"status": response.status_code, "ok": True}
        except requests.Timeout:
            raise RuntimeError("USB web upload timed out. Check USB connection.")
        except requests.ConnectionError:
            raise RuntimeError(
                f"Cannot connect to USB web interface at {host}. "
                "Make sure USB cable is connected and web interface is enabled."
            )
        except requests.HTTPError as e:
            raise RuntimeError(f"USB web upload failed: {e}")


DEFAULT_MAX_INLINE_BYTES = 2 * 1024 * 1024
UPLOAD_FORMATS = ("pdf", "epub")


def _max_inline_bytes() -> int:
    """Decoded size cap for ``file_content`` uploads (REMARKABLE_MAX_INLINE_BYTES)."""
    raw = os.environ.get("REMARKABLE_MAX_INLINE_BYTES", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_INLINE_BYTES
    return value if value > 0 else DEFAULT_MAX_INLINE_BYTES


def _decode_inline_file(file_content: str, file_format: str) -> bytes:
    """Decode base64 file contents and check they are the given format.

    Accepts line breaks and a ``data:<type>;base64,`` prefix. Raises
    ``ValueError`` with a caller-facing message on any problem.
    """
    import base64
    import binascii
    import re

    text = file_content.strip()
    if text[:5].lower() == "data:":
        header, sep, text = text.partition(",")
        if not sep or not header.lower().endswith(";base64"):
            raise ValueError("file_content data URL must be base64-encoded (';base64,').")
    text = re.sub(r"\s+", "", text)
    if not text:
        raise ValueError("file_content is empty.")

    limit = _max_inline_bytes()
    too_large = (
        f"file_content is larger than the {limit}-byte inline limit "
        "(REMARKABLE_MAX_INLINE_BYTES). Use file_path for larger files."
    )
    if len(text) // 4 * 3 > limit + 3:
        raise ValueError(too_large)
    try:
        data = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("file_content is not valid base64.")
    if len(data) > limit:
        raise ValueError(too_large)

    if file_format == "pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("file_content is not a PDF (it does not start with '%PDF-').")
    if file_format == "epub" and not data.startswith(b"PK\x03\x04"):
        raise ValueError("file_content is not an EPUB (it is not a zip archive).")
    return data


def upload_staging_dir() -> Optional[str]:
    """The resolved REMARKABLE_UPLOAD_STAGING_DIR, or None when unset or not a folder.

    Only files directly inside this folder may be moved to the Trash after an
    upload (``trash_source_after_upload``).
    """
    staging = os.environ.get("REMARKABLE_UPLOAD_STAGING_DIR", "").strip()
    if not staging:
        return None
    resolved = os.path.realpath(os.path.expanduser(staging))
    return resolved if os.path.isdir(resolved) else None


def _staged_source(file_path: str) -> str:
    """Resolve an upload source that may be moved to the Trash afterwards.

    The file must be a regular file (not a symlink) directly inside the
    staging folder. Raises ``ValueError`` otherwise.
    """
    import stat

    staging = upload_staging_dir()
    if staging is None:
        raise ValueError(
            "trash_source_after_upload needs REMARKABLE_UPLOAD_STAGING_DIR set to an "
            "existing folder."
        )
    if os.path.islink(file_path):
        raise ValueError("trash_source_after_upload does not accept symlinks.")
    source = os.path.realpath(file_path)
    if os.path.dirname(source) != staging:
        raise ValueError(
            f"trash_source_after_upload is only allowed for files directly inside {staging}."
        )
    try:
        mode = os.lstat(source).st_mode
    except OSError:
        raise ValueError(f"File not found: '{file_path}'")
    if not stat.S_ISREG(mode):
        raise ValueError("trash_source_after_upload needs a regular file.")
    return source


def _file_sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trash_uploaded_source(source: str, uploaded_sha256: str) -> dict:
    """Move an uploaded staging file to the Trash if it is unchanged since upload.

    Returns the result fields: ``source_trashed`` and, when the file is kept,
    ``source_kept_reason``.
    """
    from send2trash import send2trash

    def kept(reason: str) -> dict:
        return {"source_trashed": False, "source_kept_reason": reason}

    try:
        _staged_source(source)
        if _file_sha256(source) != uploaded_sha256:
            return kept("The file changed during the upload, so it was kept.")
        send2trash(source)
    except ValueError as e:
        return kept(str(e))
    except Exception as e:
        return kept(f"Could not move the file to the Trash: {e}")
    return {"source_trashed": True}


def _add_upload_source(response: str, fields: dict) -> str:
    """Add source details to a successful upload response, before its hint."""
    data = json.loads(response)
    if "_error" in data:
        return response
    hint = data.pop("_hint", None)
    data.update(fields)
    if hint is not None:
        data["_hint"] = hint
    return json.dumps(data, indent=2)


def _resolve_parent_id(parent_path: str, items_by_id: dict, collection: list) -> Optional[str]:
    """Resolve a folder path to its UUID.

    Args:
        parent_path: Path like "/" or "/Folder/Subfolder"
        items_by_id: Dict mapping item IDs to items
        collection: Full list of items

    Returns:
        The UUID of the folder, "" for root, or None if not found
    """
    if not parent_path or parent_path == "/":
        return ""

    from remarkable_mcp.tools import _is_cloud_archived

    # Normalize
    parent_path = parent_path.strip("/")

    for item in collection:
        # Never write into a trashed folder.
        if not item.is_folder or _is_cloud_archived(item, items_by_id):
            continue
        item_path = get_item_path(item, items_by_id).strip("/")
        if item_path.lower() == parent_path.lower():
            return item.ID

    return None


def _resolve_document(
    name_or_path: str, collection: list, items_by_id: dict, folders_only: bool = False
) -> Optional[object]:
    """Find a document or folder by name or path.

    Args:
        name_or_path: Document name or full path
        collection: Full list of items
        items_by_id: Dict mapping item IDs to items
        folders_only: If True, only match folders

    Live items win. A trashed item is only matched by its explicit
    ``/trash/...`` path (e.g. to permanently delete it over SSH), never by a
    bare name that a live item could also carry.

    Returns:
        The matching item, or None
    """
    from remarkable_mcp.tools import _is_cloud_archived

    target = name_or_path.lower().strip("/")

    trashed = []
    for item in collection:
        if folders_only and not item.is_folder:
            continue
        if _is_cloud_archived(item, items_by_id):
            trashed.append(item)
            continue
        # Match by name
        if item.VissibleName.lower() == target:
            return item
        # Match by path
        item_path = get_item_path(item, items_by_id).strip("/")
        if item_path.lower() == target:
            return item

    # Trashed items need the explicit absolute path, not "trash/...".
    if not name_or_path.lower().startswith("/trash/"):
        return None
    for item in trashed:
        if get_item_path(item, items_by_id).strip("/").lower() == target:
            return item

    return None


def _invalidate_client_cache(client) -> None:
    """Drop the in-memory document cache so the next read reflects the write."""
    client_state = getattr(client, "__dict__", {})
    invalidate_metadata = getattr(type(client), "invalidate_metadata_cache", None)
    if callable(invalidate_metadata):
        invalidate_metadata(client)
    else:
        metadata_lock = client_state.get("_metadata_lock")
        context = metadata_lock if metadata_lock is not None else nullcontext()
        with context:
            client._documents = []
            client._documents_by_id = {}
            if "_metadata_loaded_all" in client_state:
                client._metadata_loaded_all = False
            if "_metadata_generation" in client_state:
                client._metadata_generation += 1

    file_type_lock = client_state.get("_file_type_lock")
    context = file_type_lock if file_type_lock is not None else nullcontext()
    with context:
        from remarkable_mcp.local_dir import LocalDirClient

        if isinstance(client, (SSHClient, LocalDirClient)):
            client._file_type_cache = None
        elif "_file_type_cache" in client_state:
            client._file_type_cache = {}
        if "_file_type_generation" in client_state:
            client._file_type_generation += 1


def _clear_document_extraction_cache(doc_id: str) -> None:
    from remarkable_mcp.extract import clear_extraction_cache

    clear_extraction_cache(doc_id)


def _update_deferred_ssh_cache(
    client: SSHClient,
    restarted: bool,
    *,
    document: Optional[Document] = None,
    remove_id: Optional[str] = None,
    file_type: Optional[str] = None,
) -> None:
    """Keep a deferred batch resolvable without rescanning the whole library."""
    if restarted:
        _invalidate_client_cache(client)
        return

    lock = getattr(client, "_metadata_lock", None)
    context = lock if lock is not None else nullcontext()
    with context:
        documents = getattr(client, "_documents", None)
        if not isinstance(documents, list):
            return
        if remove_id is not None:
            client._documents = [item for item in documents if item.ID != remove_id]
            client._documents_by_id.pop(remove_id, None)
        elif document is not None:
            existing = client._documents_by_id.get(document.ID)
            if existing is None:
                client._documents.append(document)
            elif existing is not document:
                client._documents = [
                    document if item.ID == document.ID else item for item in client._documents
                ]
            client._documents_by_id[document.ID] = document

    file_type_lock = getattr(client, "_file_type_lock", None)
    context = file_type_lock if file_type_lock is not None else nullcontext()
    with context:
        file_type_cache = getattr(client, "_file_type_cache", None)
        if remove_id is not None and file_type_cache is not None:
            file_type_cache.pop(remove_id, None)
        elif document is not None and file_type is not None and file_type_cache is not None:
            file_type_cache[document.ID] = file_type


def _cloud_mkdir(folder_name: str, parent: str) -> str:
    """Create a folder in the reMarkable cloud."""
    client = get_rmapi()
    collection = client.get_meta_items()
    items_by_id = get_items_by_id(collection)
    parent_id = _resolve_parent_id(parent, items_by_id, collection)
    if parent_id is None:
        return make_error(
            error_type="folder_not_found",
            message=f"Parent folder not found: '{parent}'",
            suggestion="Use remarkable_browse('/') to see available folders.",
        )
    doc = client.create_folder(folder_name, parent_id)
    _invalidate_client_cache(client)
    return make_response(
        {
            "created": True,
            "folder_name": folder_name,
            "uuid": doc.id,
            "parent": parent,
            "transport": "cloud",
        },
        "Folder created in the reMarkable cloud. Use remarkable_browse() to verify.",
    )


def _cloud_move(document: str, dest_folder: str) -> str:
    """Move a document or folder to another folder in the cloud."""
    client = get_rmapi()
    collection = client.get_meta_items()
    items_by_id = get_items_by_id(collection)

    target = _resolve_document(document, collection, items_by_id)
    if not target:
        from remarkable_mcp.extract import find_similar_documents

        similar = find_similar_documents(document, collection)
        return make_error(
            error_type="document_not_found",
            message=f"Document not found: '{document}'",
            suggestion="Use remarkable_browse() to find the correct name.",
            did_you_mean=similar if similar else None,
        )

    dest_id = _resolve_parent_id(dest_folder, items_by_id, collection)
    if dest_id is None:
        return make_error(
            error_type="folder_not_found",
            message=f"Destination folder not found: '{dest_folder}'",
            suggestion="Use remarkable_browse('/') to see available folders.",
        )

    # Prevent moving a folder into itself or one of its descendants
    if target.is_folder:
        if dest_id == target.ID:
            return make_error(
                error_type="invalid_move",
                message="Cannot move a folder into itself",
                suggestion="Choose a different destination folder.",
            )
        check_id = dest_id
        while check_id and check_id in items_by_id:
            if check_id == target.ID:
                return make_error(
                    error_type="invalid_move",
                    message="Cannot move a folder into one of its subfolders",
                    suggestion="Choose a destination that is not inside the folder being moved.",
                )
            check_id = getattr(items_by_id[check_id], "Parent", "")

    old_path = get_item_path(target, items_by_id)
    client.move(target.ID, dest_id)
    _invalidate_client_cache(client)
    return make_response(
        {
            "moved": True,
            "name": target.VissibleName,
            "from": old_path,
            "to": dest_folder,
            "transport": "cloud",
        },
        "Document moved in the reMarkable cloud. Use remarkable_browse() to verify.",
    )


def _cloud_rename(document: str, new_name: str) -> str:
    """Rename a document or folder in the cloud."""
    client = get_rmapi()
    collection = client.get_meta_items()
    items_by_id = get_items_by_id(collection)

    target = _resolve_document(document, collection, items_by_id)
    if not target:
        from remarkable_mcp.extract import find_similar_documents

        similar = find_similar_documents(document, collection)
        return make_error(
            error_type="document_not_found",
            message=f"Document not found: '{document}'",
            suggestion="Use remarkable_browse() to find the correct name.",
            did_you_mean=similar if similar else None,
        )

    old_name = target.VissibleName
    client.rename(target.ID, new_name)
    _invalidate_client_cache(client)
    return make_response(
        {
            "renamed": True,
            "old_name": old_name,
            "new_name": new_name,
            "transport": "cloud",
        },
        "Document renamed in the reMarkable cloud. Use remarkable_browse() to verify.",
    )


def _as_list(value) -> list:
    """A tool argument given as one name or a list of names, as a list."""
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _library_tag_names(collection: list) -> list:
    """Tag names used anywhere in the library (document and page tags)."""
    names: list = []
    for item in collection:
        for attr in ("tags", "page_tags"):
            for tag in getattr(item, attr, None) or ():
                name = tag.get("name") if isinstance(tag, dict) else tag
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
    return names


def _tag_editor(add: list, remove: list, rename: dict, page: Optional[int], known: list):
    """``apply(content)`` for ``update_document_content`` / the SSH path."""
    from remarkable_mcp.tag_edit import TagEditError, edit_tags

    def apply(content: dict) -> dict:
        page_id = None
        if page is not None:
            page_ids = _page_ids_from_content(content)
            if not 1 <= page <= len(page_ids):
                raise TagEditError(
                    f"Page {page} does not exist; the document has {len(page_ids)} page(s)."
                )
            page_id = page_ids[page - 1]
        return edit_tags(
            content,
            now_ms=int(time.time() * 1000),
            add=add,
            remove=remove,
            rename=rename,
            page_id=page_id,
            known_tags=known,
        )

    return apply


def _tag_data(target, page: Optional[int], result: dict) -> dict:
    data = {
        "document": target.VissibleName,
        "page": page,
        "tags": result["tags"],
        "added": result["added"],
        "removed": result["removed"],
        "renamed": result["renamed"],
        "changed": result["changed"],
    }
    if page is None:
        del data["page"]
    if result["notes"]:
        data["notes"] = result["notes"]
    return data


def _tag_response(target, page: Optional[int], result: dict, transport: str, **extra) -> str:
    data = dict(_tag_data(target, page, result), transport=transport)
    data.update(extra)
    scope = f"page {page} of '{target.VissibleName}'" if page else f"'{target.VissibleName}'"
    if result["changed"]:
        hint = f"Tags on {scope} updated. Changes reach the tablet on its next sync."
    else:
        hint = f"No tag changes made on {scope}."
    return make_response(data, hint)


def _tags_response(targets: list, page: Optional[int], results: dict, transport: str, **extra):
    """The response for tag changes on several documents."""
    documents = [_tag_data(t, page, results[t.ID]) for t in targets]
    changed = [d["document"] for d in documents if d["changed"]]
    data = {"documents": documents, "changed": changed, "transport": transport, **extra}
    scope = f"page {page} of " if page else ""
    if changed:
        hint = (
            f"Tags on {scope}{len(changed)} of {len(targets)} documents updated. "
            "Changes reach the tablet on its next sync."
        )
    else:
        hint = f"No tag changes made on {scope}{len(targets)} documents."
    return make_response(data, hint)


def _ssh_tag(ssh_client, targets: list, apply, defer_restart: bool) -> tuple:
    """Change documents' tags on the tablet's filesystem, with one restart.

    Every document is read and edited before any is written. Returns
    ``(results by document id, refresh_pending)``, or an error response.
    """
    from remarkable_mcp import tablet_json
    from remarkable_mcp.tag_edit import other_fields

    writes, results = [], {}
    for target in targets:
        content_path = f"{XOCHITL_PATH}/{target.ID}.content"
        meta_path = f"{XOCHITL_PATH}/{target.ID}.metadata"
        content_raw = _read_remote_bytes(ssh_client, content_path)
        meta_raw = _read_remote_bytes(ssh_client, meta_path)
        if not content_raw or not meta_raw:
            return make_error(
                error_type="no_content",
                message=(
                    f"Could not read the .content or .metadata file for '{target.VissibleName}'."
                ),
                suggestion="Check the document exists on the tablet, then try again.",
            )
        content = json.loads(content_raw)
        before = json.loads(json.dumps(other_fields(content)))
        results[target.ID] = result = apply(content)
        if not result["changed"]:
            continue
        if other_fields(content) != before:
            raise RuntimeError("Refusing to write: fields other than tags changed.")
        metadata = json.loads(meta_raw)
        metadata["lastModified"] = str(int(time.time() * 1000))
        writes.append((target, content_path, content, meta_path, metadata))

    if not writes:
        return results, False
    for target, content_path, content, meta_path, metadata in writes:
        _write_remote_bytes(ssh_client, content_path, tablet_json.dumps(content).encode("utf-8"))
        _write_remote_bytes(ssh_client, meta_path, tablet_json.dumps(metadata).encode("utf-8"))
    ssh_client.mark_operation_dirty()
    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)
    for target, *_ in writes:
        target.last_modified = datetime.now()
        _update_deferred_ssh_cache(ssh_client, restarted, document=target)
    return results, not restarted


MARK_PREVIEW_MAX_PAGES = 4
MARK_PREVIEW_DPI = 100


def _page_obstacles(client, files: list, pdf: bytes):
    """``obstacles(pdf_page, page_map)``: ``{"ink", "text"}`` regions of the
    user's ink and the PDF's printed text on a page, for placing marks with
    ``near``."""
    from remarkable_mcp.extract import page_ink_regions
    from remarkable_mcp.pdf_marks import original_pdf, tablet_page, text_lines

    base = original_pdf(pdf)
    cache: dict = {}

    def obstacles(pdf_page: int, page_map: list) -> list:
        if pdf_page not in cache:
            page = tablet_page(pdf_page, page_map)
            ink = []
            if page:
                with _document_dir(client, files, base) as root:
                    ink = page_ink_regions(root, page)
            text = [line["region"] for line in text_lines(base, pdf_page)]
            cache[pdf_page] = {"ink": ink, "text": text}
        return cache[pdf_page]

    return obstacles


def _mark_editor(
    add: list,
    remove: list,
    clear: bool,
    color: Optional[str],
    build: bool,
    obstacles=None,
):
    """``edit(pdf, content)`` for ``replace_document_pdf``, or a dry run.

    ``obstacles`` (see ``_page_obstacles``) is needed for marks placed with
    ``near``."""
    from remarkable_mcp import pdf_marks
    from remarkable_mcp.pages import pdf_page_map

    if color:
        pdf_marks.check_color(color)

    def edit(pdf: bytes, content: dict, read_ink=None) -> dict:
        """``read_ink(page_id)`` returns a page's ink file (or None); it is
        needed to write marks' covers and report their state."""
        from remarkable_mcp import cover_layers
        from remarkable_mcp.pages import content_page_ids

        base, marks = pdf_marks.load_state(pdf)
        page_map = pdf_page_map(content)
        page_ids = content_page_ids(content)
        result = pdf_marks.edit_marks(
            marks,
            add=add,
            remove=remove,
            clear=clear,
            page_map=page_map,
            sizes=pdf_marks.page_sizes(base),
            color=color,
            obstacles=(lambda i: obstacles(i, page_map)) if obstacles else None,
        )
        touched = {m["pdf_page"] for m in marks if m["id"] in result["removed"]}
        touched |= {m["pdf_page"] for m in result["marks"] if m["id"] in result["added"]}
        result["page_map"] = page_map
        result["touched_pages"] = sorted(
            p for p in (pdf_marks.tablet_page(i, page_map) for i in touched) if p
        )
        result["ink"] = {}
        covers = [m.get("cover") for m in marks + result["marks"]]
        if build and result["changed"] and any(isinstance(c, dict) for c in covers):
            if read_ink is None:
                raise pdf_marks.MarkError("Covers cannot be written here.")
            try:
                result["ink"] = cover_layers.write_mark_covers(
                    marks,
                    result["marks"],
                    result["removed"],
                    page_ids=page_ids,
                    page_map=page_map,
                    sizes=pdf_marks.page_sizes(base),
                    read_ink=read_ink,
                    author=cover_layers.author_uuid(),
                )
            except cover_layers.CoverError as e:
                raise pdf_marks.MarkError(str(e)) from None
        result["cover_states"], cover_notes = {}, []
        if read_ink is not None:
            written = result["ink"]
            result["cover_states"], cover_notes = cover_layers.mark_cover_states(
                result["marks"],
                page_ids=page_ids,
                page_map=page_map,
                read_ink=lambda page_id: written.get(page_id) or read_ink(page_id),
            )
        result["notes"] = result["notes"] + cover_notes
        result["pdf"] = (
            pdf_marks.build(base, result["marks"], outline=pdf_marks.load_outline(pdf))
            if result["changed"]
            else None
        )
        if not build:
            result["preview_pdf"], result["pdf"] = result["pdf"], None
        return result

    return edit


def _ink_reader(client, doc_id: str, files: list):
    """``read_ink(page_id)``: a page's ``.rm`` file from a document's index
    ``files``, or None when the page has no ink."""
    by_id = {entry["id"]: entry for entry in files}

    def read_ink(page_id: str) -> Optional[bytes]:
        entry = by_id.get(f"{doc_id}/{page_id}.rm")
        return None if entry is None else client._get_file(entry["hash"], entry["id"])

    return read_ink


@contextmanager
def _document_dir(client, files: list, pdf: bytes):
    """A temporary directory holding a document's files from its index
    ``files``, with ``pdf`` in place of its PDF."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="remarkable-mark-") as tmp:
        root = Path(tmp).resolve()
        for entry in files:
            path = (root / entry["id"]).resolve()
            if not path.is_relative_to(root):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if entry["id"].endswith(".pdf"):
                path.write_bytes(pdf)
            else:
                path.write_bytes(client._get_file(entry["hash"], entry["id"]))
        yield root


def _mark_preview_images(client, files: list, pdf: bytes, pages: list) -> list:
    """Merged page images of a document with ``pdf`` in place of its PDF."""
    import base64

    from mcp.types import ImageContent

    from remarkable_mcp.extract import render_merged_page_from_extracted_document

    images = []
    with _document_dir(client, files, pdf) as root:
        for page in pages[:MARK_PREVIEW_MAX_PAGES]:
            png, _ = render_merged_page_from_extracted_document(
                root, page=page, dpi=MARK_PREVIEW_DPI
            )
            if png:
                images.append(
                    ImageContent(
                        type="image",
                        data=base64.b64encode(png).decode("ascii"),
                        mimeType="image/png",
                    )
                )
    return images


def _mark_layout(client, files: list, pdf: bytes, page: int, page_map: list) -> dict:
    """The original PDF's text lines, the user's ink and the free space on a
    tablet page."""
    from remarkable_mcp.extract import page_ink_regions
    from remarkable_mcp.pdf_marks import (
        MarkError,
        free_regions,
        original_pdf,
        stored_marks,
        text_lines,
    )

    if not 1 <= page <= len(page_map):
        raise MarkError(f"Page {page} does not exist; the document has {len(page_map)}.")
    pdf_page = page_map[page - 1]
    base = original_pdf(pdf)
    with _document_dir(client, files, base) as root:
        ink = page_ink_regions(root, page)
    lines = text_lines(base, pdf_page) if pdf_page is not None else []
    marks = [m["box"] for m in stored_marks(pdf) if m.get("pdf_page") == pdf_page]
    return {
        "page": page,
        "text_lines": lines,
        "ink": ink,
        "free": free_regions(ink + [line["region"] for line in lines] + marks),
    }


def _mark_response(target, result: dict, dry_run: bool) -> str:
    from remarkable_mcp.pdf_marks import summarize, tablet_page

    page_map = result["page_map"]
    data = {
        "document": target.VissibleName,
        "marks": [
            summarize(
                m,
                tablet_page(m["pdf_page"], page_map),
                result.get("cover_states", {}).get(m["id"]),
            )
            for m in result["marks"]
        ],
        "added": result["added"],
        "removed": result["removed"],
        "changed": result["changed"],
        "transport": "cloud",
    }
    if dry_run:
        data["dry_run"] = True
        data["preview_pages"] = result["touched_pages"][:MARK_PREVIEW_MAX_PAGES]
    if result["notes"]:
        data["notes"] = result["notes"]
    if "layout" in result:
        data["layout"] = result["layout"]
    name = target.VissibleName
    if not result["changed"]:
        hint = f"No mark changes made on '{name}'."
    elif dry_run:
        hint = (
            f"Dry run: nothing was written to '{name}'. Check the preview images, "
            "then repeat the call without dry_run to apply it."
        )
    else:
        hint = f"Marks on '{name}' updated. Changes reach the tablet on its next sync."
    return make_response(data, hint)


def _outline_editor(outline, build: bool):
    """``edit(pdf, content)`` for ``replace_document_pdf`` that sets a PDF's
    outline (None lists it, "original" restores the PDF's own)."""
    from remarkable_mcp import pdf_marks
    from remarkable_mcp.pages import pdf_page_map

    def edit(pdf: bytes, content: dict) -> dict:
        base, marks = pdf_marks.load_state(pdf)
        current = pdf_marks.load_outline(pdf)
        page_map = pdf_page_map(content)
        if outline is None:
            new = current
        elif outline == "original":
            new = None
        else:
            new = pdf_marks.check_outline(outline, page_map)
        changed = outline is not None and new != current
        entries = new if new is not None else pdf_marks.pdf_outline(base)
        opening = _page_openings(
            base, {e["pdf_page"] for e in entries if e["pdf_page"] is not None}
        )
        listed = []
        for entry in entries:
            index = entry["pdf_page"]
            listed.append(
                {
                    "level": entry["level"],
                    "title": entry["title"],
                    "page": (pdf_marks.tablet_page(index, page_map) if index is not None else None),
                    "first_lines": opening.get(index, []),
                }
            )
        return {
            "outline": listed,
            "source": "set with remarkable_pdf_outline" if new is not None else "the PDF's own",
            "changed": changed,
            "pdf": pdf_marks.build(base, marks, outline=new) if changed and build else None,
        }

    return edit


OUTLINE_OPENING_LINES = 3


# Running headers and footers sit in these top and bottom bands of the page.
MARGIN_BAND = 0.08


def _page_openings(pdf: bytes, pages) -> dict:
    """The first lines of text on each PDF page in ``pages``, by page index,
    for checking outline targets. Left out: running headers and footers (a
    line in the top or bottom band that, ignoring digits, is on over half of
    these pages), page numbers and lines without letters or digits."""
    import re

    from remarkable_mcp.pdf_marks import text_lines

    def key(text: str) -> str:
        return re.sub(r"\d+", "#", text.strip().lower())

    def in_margin(line: dict) -> bool:
        top, bottom = line["region"][1], line["region"][3]
        return bottom < MARGIN_BAND or top > 1 - MARGIN_BAND

    lines = {index: text_lines(pdf, index) for index in pages}
    seen: dict = {}
    for page_lines in lines.values():
        for k in {key(line["text"]) for line in page_lines if in_margin(line)}:
            seen[k] = seen.get(k, 0) + 1
    repeated = {k for k, n in seen.items() if len(lines) >= 3 and n > len(lines) / 2}
    out = {}
    for index, page_lines in lines.items():
        kept = [
            line["text"]
            for line in page_lines
            if not (in_margin(line) and key(line["text"]) in repeated)
            and re.search(r"[^\W_]", line["text"])
            and not line["text"].strip().isdigit()
        ]
        out[index] = kept[:OUTLINE_OPENING_LINES]
    return out


def _outline_response(target, result: dict, dry_run: bool) -> str:
    data = {
        "document": target.VissibleName,
        "outline": result["outline"],
        "entries": len(result["outline"]),
        "source": result["source"],
        "changed": result["changed"],
        "transport": "cloud",
    }
    if dry_run:
        data["dry_run"] = True
    name = target.VissibleName
    if dry_run and result["changed"]:
        hint = f"Dry run: nothing was written to '{name}'. Repeat without dry_run to apply."
    elif result["changed"]:
        hint = f"Outline of '{name}' updated. The tablet shows it after its next sync."
    elif not result["outline"]:
        hint = f"'{name}' has no outline."
    else:
        hint = f"No outline changes made on '{name}'."
    return make_response(data, hint)


def _cloud_delete(document: str) -> str:
    """Move a document or folder to the cloud trash (recoverable on-device)."""
    client = get_rmapi()
    collection = client.get_meta_items()
    items_by_id = get_items_by_id(collection)

    target = _resolve_document(document, collection, items_by_id)
    if not target:
        from remarkable_mcp.extract import find_similar_documents

        similar = find_similar_documents(document, collection)
        return make_error(
            error_type="document_not_found",
            message=f"Document not found: '{document}'",
            suggestion="Use remarkable_browse() to find the correct name.",
            did_you_mean=similar if similar else None,
        )

    doc_path = get_item_path(target, items_by_id)
    client.delete(target.ID)
    _invalidate_client_cache(client)
    return make_response(
        {
            "deleted": True,
            "name": target.VissibleName,
            "path": doc_path,
            "type": "folder" if target.is_folder else "document",
            "transport": "cloud",
        },
        "Moved to the trash on your reMarkable (recoverable from the device's Trash).",
    )


class _DeleteConfirmation(BaseModel):
    """Schema for confirming a destructive delete via elicitation."""

    confirm: bool = Field(
        default=False,
        description="Set to true to confirm this destructive delete operation.",
    )


def _delete_confirmation_available(ctx: Context) -> bool:
    """Return whether deletion may proceed via elicitation or explicit bypass."""
    if os.environ.get("REMARKABLE_SKIP_CONFIRM", "").lower() in ("1", "true", "yes"):
        return True
    return client_supports_elicitation(ctx)


def _resolve_delete_confirmation(
    document: str,
    permanent: bool,
    confirmation_available: Annotated[bool, Resolve(_delete_confirmation_available)],
) -> _DeleteConfirmation | Elicit[_DeleteConfirmation]:
    """Resolve confirmation over a legacy back-channel or a modern input round."""
    skip = os.environ.get("REMARKABLE_SKIP_CONFIRM", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if skip:
        return _DeleteConfirmation(confirm=True)
    if not confirmation_available:
        return _DeleteConfirmation(confirm=False)
    if permanent:
        message = (
            f"Permanently delete '{document}' from your reMarkable? "
            "This is irreversible and cannot be recovered from Trash."
        )
    else:
        message = f"Delete '{document}' from your reMarkable? This moves it to the trash."
    return Elicit(
        message,
        _DeleteConfirmation,
    )


def _delete_confirmation_abort(
    confirmation: ElicitationResult[_DeleteConfirmation],
    confirmation_available: bool,
    document: str,
) -> Optional[str]:
    """Return an abort response unless a destructive delete was confirmed.

    Returns None to proceed with the delete, or a response string (a
    cancellation or a refusal) to abort.

    Delete is destructive, so confirmation is required. It is bypassed only when
    REMARKABLE_SKIP_CONFIRM=1 (for headless automation). When the client cannot
    show a confirmation prompt — no elicitation support, or elicitation fails at
    runtime — the delete is REFUSED rather than performed silently, so a client
    that can't prompt the user cannot delete documents unattended. Write tools
    are on by default, and most MCP clients don't support elicitation yet, so
    this refusal path is the common case for unattended setups.
    """
    if not confirmation_available:
        return make_error(
            "confirmation_unavailable",
            (
                f"Refused to delete '{document}': this client cannot show a "
                "confirmation prompt (no MCP elicitation support) and delete is "
                "a destructive operation."
            ),
            (
                "Confirm the delete from a client that supports elicitation, or "
                "set REMARKABLE_SKIP_CONFIRM=1 to allow deletes without a prompt "
                "(e.g. for headless automation)."
            ),
        )
    if confirmation.action != "accept" or not getattr(confirmation.data, "confirm", False):
        return make_response(
            {"deleted": False, "cancelled": True, "document": document},
            "Delete cancelled — nothing was changed.",
        )
    return None


def _author_draw(
    document: str,
    page: int,
    strokes: list,
    ui_submitted: bool,
    defer_restart: bool = False,
) -> str:
    """Append strokes to a page, auto-creating a blank drawable layer if absent."""
    if not document or page is None:
        return make_error(
            error_type="missing_parameter",
            message="draw requires 'document' and 'page'.",
            suggestion=(
                'Call remarkable_author(method="draw", document=..., page=1, strokes=[...]).'
            ),
        )
    if not strokes:
        return make_error(
            error_type="no_strokes",
            message="No strokes provided to write.",
            suggestion=(
                "Pass a non-empty list of stroke dicts, e.g. "
                '[{"points": [[0.1,0.2],[0.8,0.2]], "tool": "fineliner", "color": "black"}].'
            ),
        )

    from remarkable_mcp import notebooks as nb
    from remarkable_mcp import strokes as strokes_mod

    ssh_client = _get_ssh_client()
    collection = ssh_client.get_meta_items()
    items_by_id = get_items_by_id(collection)
    target = _resolve_document(document, collection, items_by_id)
    if not target:
        from remarkable_mcp.extract import find_similar_documents

        similar = find_similar_documents(document, collection)
        return make_error(
            error_type="document_not_found",
            message=f"Document not found: '{document}'",
            suggestion="Use remarkable_browse() to find the correct name.",
            did_you_mean=similar or None,
        )

    doc_uuid = target.ID
    content_raw = _read_remote_bytes(ssh_client, f"{XOCHITL_PATH}/{doc_uuid}.content")
    try:
        content_data = json.loads(content_raw) if content_raw else {}
    except (ValueError, TypeError):
        content_data = {}
    page_ids = _page_ids_from_content(content_data)
    if not page_ids:
        return make_error(
            error_type="no_pages",
            message=f"Could not read the page list for '{target.VissibleName}'.",
            suggestion="The document may have no pages, or an unexpected .content format.",
        )
    if page < 1 or page > len(page_ids):
        return make_error(
            error_type="page_out_of_range",
            message=f"Page {page} does not exist. Document has {len(page_ids)} page(s).",
            suggestion=f"Use page=1 to {len(page_ids)}.",
        )

    page_id = page_ids[page - 1]
    file_type = str(content_data.get("fileType", "") or "")
    rm_path = f"{XOCHITL_PATH}/{doc_uuid}/{page_id}.rm"
    original = _read_remote_bytes(ssh_client, rm_path)
    # A native notebook page that has never been drawn on (or a PDF/EPUB page
    # with no annotation overlay yet) has no .rm file. Seed a blank drawable
    # layer so the strokes have somewhere to land.
    page_existed = original is not None
    if not page_existed:
        original = nb.blank_page_rm_bytes()

    try:
        geom = strokes_mod.page_geometry(original)
        new_bytes = strokes_mod.append_strokes(original, strokes)
    except strokes_mod.StrokeError as e:
        return make_error(
            error_type="stroke_write_failed",
            message=str(e),
            suggestion=(
                "Check the stroke format (points normalized [0,1]) and that "
                "the page has a drawable layer."
            ),
        )
    except Exception as e:  # noqa: BLE001 - surface as structured guidance
        return make_error(
            error_type="stroke_write_failed",
            message=f"Failed to build strokes for page {page}: {e}",
            suggestion="Verify the stroke payload and retry.",
        )

    # Preserve the pristine original once (only if the page pre-existed), then
    # write the appended bytes.
    if page_existed:
        bak_path = rm_path + ".bak"
        if not _remote_file_exists(ssh_client, bak_path):
            _write_remote_bytes(ssh_client, bak_path, original)
    _write_remote_bytes(ssh_client, rm_path, new_bytes)
    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)
    _update_deferred_ssh_cache(ssh_client, restarted)
    _clear_document_extraction_cache(doc_uuid)

    result = {
        "written": True,
        "document": target.VissibleName,
        "page": page,
        "total_pages": len(page_ids),
        "strokes_added": len(strokes),
        "paper_size": [geom["paper_width"], geom["paper_height"]],
        "file_type": file_type or "notebook",
        "created_overlay": not page_existed,
        "writable": True,
        "transport": "ssh",
        "ui_submitted": bool(ui_submitted),
        "refresh_pending": not restarted,
    }
    if file_type.lower() == "epub":
        result["caveat"] = (
            "This is a reflowable EPUB; annotations are anchored to the "
            "current layout and may shift if the font size or layout changes."
        )
    hint = _write_result_message(
        restarted,
        (f"Appended {len(strokes)} stroke(s) to page {page} of '{target.VissibleName}'."),
        "Call remarkable_canvas(document, page) to view the updated page.",
    )
    return make_response(result, hint)


def _author_add_page(document: str, defer_restart: bool = False) -> str:
    """Append a blank, drawable page to the end of a native notebook."""
    if not document:
        return make_error(
            error_type="missing_parameter",
            message="add_page requires 'document'.",
            suggestion='Call remarkable_author(method="add_page", document=...).',
        )

    from remarkable_mcp import notebooks as nb

    ssh_client = _get_ssh_client()
    collection = ssh_client.get_meta_items()
    items_by_id = get_items_by_id(collection)
    target = _resolve_document(document, collection, items_by_id)
    if not target:
        from remarkable_mcp.extract import find_similar_documents

        similar = find_similar_documents(document, collection)
        return make_error(
            error_type="document_not_found",
            message=f"Document not found: '{document}'",
            suggestion="Use remarkable_browse() to find the correct name.",
            did_you_mean=similar or None,
        )

    doc_uuid = target.ID
    content_raw = _read_remote_bytes(ssh_client, f"{XOCHITL_PATH}/{doc_uuid}.content")
    try:
        content_data = json.loads(content_raw) if content_raw else {}
    except (ValueError, TypeError):
        content_data = {}
    if not content_data:
        return make_error(
            error_type="no_content",
            message=f"Could not read the .content file for '{target.VissibleName}'.",
            suggestion="The document may be missing or have an unexpected format.",
        )

    # Reuse the notebook's existing author uuid so CRDT author ids stay aligned.
    author_uuid = None
    uuids = content_data.get("cPages", {}).get("uuids")
    if isinstance(uuids, list) and uuids and isinstance(uuids[0], dict):
        author_uuid = uuids[0].get("first")

    new_page_id = nb.new_uuid()
    try:
        updated = nb.append_page_to_content(content_data, new_page_id)
    except ValueError as e:
        return make_error(
            error_type="not_a_notebook",
            message=str(e),
            suggestion=(
                "Pages can only be added to native notebooks. PDF/EPUB pages "
                "already exist — open the page and draw on it directly."
            ),
        )

    try:
        page_bytes = nb.blank_page_rm_bytes(author_uuid=author_uuid)
    except (ValueError, TypeError):
        page_bytes = nb.blank_page_rm_bytes()

    _write_remote_bytes(ssh_client, f"{XOCHITL_PATH}/{doc_uuid}/{new_page_id}.rm", page_bytes)
    _write_content_file(ssh_client, doc_uuid, updated["content"])
    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)
    _update_deferred_ssh_cache(ssh_client, restarted)
    _clear_document_extraction_cache(doc_uuid)

    result = {
        "added": True,
        "document": target.VissibleName,
        "page_added": updated["page_index"],
        "total_pages": updated["total_pages"],
        "paper_size": list(nb.DEFAULT_PAPER),
        "transport": "ssh",
        "refresh_pending": not restarted,
    }
    hint = _write_result_message(
        restarted,
        (f"Added a blank page (now page {updated['page_index']} of {updated['total_pages']})."),
        (
            f"Call remarkable_canvas('{target.VissibleName}', "
            f"{updated['page_index']}) to draw on it."
        ),
    )
    return make_response(result, hint)


def _author_create_document(
    name: str,
    text: Optional[str],
    folder: Optional[str],
    defer_restart: bool = False,
) -> str:
    """Create a new native notebook (blank, or seeded with typed text)."""
    if not name:
        return make_error(
            error_type="missing_parameter",
            message="create_document requires 'name'.",
            suggestion='Call remarkable_author(method="create_document", name="My notes").',
        )

    from remarkable_mcp import notebooks as nb

    ssh_client = _get_ssh_client()
    collection = ssh_client.get_meta_items()
    items_by_id = get_items_by_id(collection)
    parent_id = _resolve_parent_id(folder or "/", items_by_id, collection)
    if parent_id is None:
        folders = [get_item_path(i, items_by_id) for i in collection if i.is_folder]
        return make_error(
            error_type="folder_not_found",
            message=f"Folder not found: '{folder}'",
            suggestion="Use remarkable_browse('/') to see available folders.",
            did_you_mean=folders[:5] if folders else None,
        )

    doc_uuid = nb.new_uuid()
    page_id = nb.new_uuid()
    author_uuid = nb.new_uuid()
    page_bytes = nb.page_rm_bytes(text or "", author_uuid=author_uuid)
    content_data = nb.new_notebook_content([page_id], author_uuid)
    metadata = nb.new_document_metadata(name, parent=parent_id)

    ssh_client._ssh_command(f"mkdir -p '{XOCHITL_PATH}/{doc_uuid}'")
    ssh_client.mark_operation_dirty()
    _write_remote_bytes(ssh_client, f"{XOCHITL_PATH}/{doc_uuid}/{page_id}.rm", page_bytes)
    _write_content_file(ssh_client, doc_uuid, content_data)
    _write_metadata(ssh_client, doc_uuid, metadata)
    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)
    cached_doc = Document(
        id=doc_uuid,
        hash=doc_uuid,
        name=name,
        doc_type="DocumentType",
        parent=parent_id,
        synced=False,
        last_modified=datetime.now(),
        local_path=f"{XOCHITL_PATH}/{doc_uuid}",
    )
    _update_deferred_ssh_cache(ssh_client, restarted, document=cached_doc, file_type="notebook")

    result = {
        "created": True,
        "document": name,
        "document_id": doc_uuid,
        "page_id": page_id,
        "total_pages": 1,
        "paper_size": list(nb.DEFAULT_PAPER),
        "has_text": bool(text),
        "folder": folder or "/",
        "transport": "ssh",
        "refresh_pending": not restarted,
    }
    hint = _write_result_message(
        restarted,
        f"Created notebook '{name}'.",
        f"Call remarkable_canvas('{name}', 1) to view or draw on it.",
    )
    return make_response(result, hint)


def register_write_tools():
    """Register all write tools with the MCP server."""
    # Imported lazily to break a circular import: server.py imports this module
    # at load time and immediately calls register_write_tools(), so importing
    # `mcp` at module top would re-enter a partially-initialized server module.
    from remarkable_mcp.server import mcp

    async def remarkable_author(
        method: str,
        document: Optional[str] = None,
        page: Optional[int] = None,
        strokes: Optional[list] = None,
        name: Optional[str] = None,
        text: Optional[str] = None,
        folder: Optional[str] = None,
        defer_restart: bool = False,
        ui_submitted: bool = False,
        ctx: Context = None,
    ) -> str:
        """
        <usecase>Author native reMarkable ink and notebooks. ONE compound write
        primitive with three methods: "draw" appends pen/highlighter strokes to a
        page, "add_page" appends a blank drawable page to a notebook, and
        "create_document" creates a new notebook (optionally seeded with typed
        text). This is the single tool behind the interactive canvas (Save → draw,
        ＋Page → add_page, new notebook → create_document) AND model-driven markup
        (highlighting, underlining, marking) — the model composes the strokes.</usecase>
        <instructions>
        Requires SSH mode (the default tablet-filesystem transport for write-back)
        and write mode (the default; disabled with --read-only). All methods are
        non-destructive (draw appends to existing ink and backs the page up to
        {pageId}.rm.bak the first time), so no confirmation prompt is required.

        Pick the method, then pass only that method's parameters:

        - method="draw": requires `document` + `page` + `strokes`. Strokes are
          APPENDED to the page's ink. If the page has no ink layer yet (a fresh
          notebook page, or a PDF/EPUB page never annotated), a blank drawable
          layer is created automatically. Coordinates: each point is [nx, ny] or
          [nx, ny, pressure] with nx, ny in [0,1] from the page's TOP-LEFT; the
          tool maps them to the page's own paper_size so the same payload works on
          any device. (PDF-backed pages currently use the same map; precise PDF
          overlay placement is a follow-up.)

        - method="add_page": requires `document`. Appends one blank page to the END
          of a native notebook and returns its 1-based page number. Only native
          notebooks support this (PDF/EPUB pages already exist).

        - method="create_document": requires `name`. Creates a new native notebook
          in `folder` (default root). Leave `text` EMPTY for a blank notebook —
          that is the default and the correct choice. Only pass `text` when the
          user EXPLICITLY asks for specific typed content; never invent a title,
          placeholder, or "created by" line. Returns the new document id and
          first-page geometry.

        The interactive canvas calls this exact tool via the MCP Apps bridge,
        passing ui_submitted=true for draw (the human already chose Save). A model
        calling it directly should omit ui_submitted.
        </instructions>
        <parameters>
        - method: "draw" | "add_page" | "create_document".
        - document: Target document name/path/id (draw, add_page).
        - page: 1-based page number (draw).
        - strokes: List of stroke dicts (draw). Each:
            {"points": [[nx, ny], [nx, ny, pressure], ...],
             "tool": "fineliner" | "highlighter" | int,
             "color": "black" | "yellow" | ...,
             "width": <optional float>, "thickness_scale": <optional float>}
        - name: Title of the new notebook (create_document).
        - text: Optional typed text for the first page (create_document). Omit it
            unless the user explicitly requested specific content; never fabricate
            placeholder text. Paragraphs split on newlines.
        - folder: Destination folder path (create_document; default "/").
        - ui_submitted: Set by the canvas app when the user clicked Save. Models omit it.
        - defer_restart: Skip the xochitl restart for this write. Call
          remarkable_refresh() once after the batch. The response sets
          refresh_pending=true while a refresh is owed.
        </parameters>
        <examples>
        - remarkable_author(method="draw", document="Ideas", page=1,
            strokes=[{"points": [[0.1,0.2],[0.8,0.2]], "tool": "highlighter", "color": "yellow"}])
        - remarkable_author(method="add_page", document="Ideas")
        - remarkable_author(method="create_document", name="Sketches")
        - remarkable_author(method="create_document", name="Meeting notes",
            text="Agenda\nFollow-ups")  # only when the user supplied that text
        </examples>
        """

        validation_error = _validate_author_request(
            method,
            document,
            page,
            strokes,
            name,
        )
        if validation_error:
            return validation_error

        def _impl() -> str:
            error = _require_write_transport()
            if error:
                return error

            if method == "draw":
                return _author_draw(document, page, strokes, ui_submitted, defer_restart)
            if method == "add_page":
                return _author_add_page(document, defer_restart)
            if method == "create_document":
                return _author_create_document(name, text, folder, defer_restart)
            raise AssertionError(f"Validated author method is unsupported: {method}")

        ssh_client = _get_ssh_client()
        if isinstance(ssh_client, SSHClient):
            return await _run_ssh_tool_operation(
                ssh_client,
                f"author-{method}",
                _impl,
                defer_restart=defer_restart,
            )
        return await asyncio.to_thread(_impl)

    # Native ink/notebook authoring is SSH-only today (cloud/USB-web write-back
    # is not implemented yet), so only expose the tool in SSH mode rather than
    # registering it everywhere and erroring at call time. Clients on other
    # transports simply don't see a tool they couldn't use.
    if _is_ssh_mode():
        mcp.tool(annotations=WRITE_ANNOTATIONS)(remarkable_author)

    async def _upload_path(
        file_path: str,
        parent_folder: str,
        document_name: Optional[str],
        defer_restart: bool,
    ) -> str:
        """Upload a local PDF/EPUB file over the active transport."""

        def _impl() -> str:
            error = _require_write_transport()
            if error:
                return error

            try:
                # Validate file
                if not os.path.isfile(file_path):
                    return make_error(
                        error_type="file_not_found",
                        message=f"File not found: '{file_path}'",
                        suggestion="Provide an absolute path to an existing PDF or EPUB file.",
                    )

                ext = os.path.splitext(file_path)[1].lower().lstrip(".")
                if ext not in ("pdf", "epub"):
                    return make_error(
                        error_type="unsupported_format",
                        message=f"Unsupported file format: '.{ext}'",
                        suggestion="Only PDF and EPUB files can be uploaded to reMarkable.",
                    )

                # Cloud mode: upload via the sync v3/v4 blob protocol
                if _is_cloud_mode():
                    client = get_rmapi()
                    collection = client.get_meta_items()
                    items_by_id = get_items_by_id(collection)
                    parent_id = _resolve_parent_id(parent_folder, items_by_id, collection)
                    if parent_id is None:
                        folders = [get_item_path(i, items_by_id) for i in collection if i.is_folder]
                        return make_error(
                            error_type="folder_not_found",
                            message=f"Folder not found: '{parent_folder}'",
                            suggestion="Use remarkable_browse('/') to see available folders.",
                            did_you_mean=folders[:5] if folders else None,
                        )

                    name = document_name or os.path.splitext(os.path.basename(file_path))[0]
                    with open(file_path, "rb") as f:
                        data = f.read()
                    doc = client.upload_document(data, name, ext, parent_id)
                    return make_response(
                        {
                            "uploaded": True,
                            "name": name,
                            "uuid": doc.id,
                            "format": ext,
                            "parent_folder": parent_folder,
                            "transport": "cloud",
                        },
                        "Document uploaded to the reMarkable cloud. "
                        "Use remarkable_browse() to verify it appears.",
                    )

                # USB web mode: simple HTTP upload
                if _is_usb_web_mode():
                    _upload_via_usb_web(file_path, document_name)

                    name = document_name or os.path.splitext(os.path.basename(file_path))[0]

                    # Clear cached documents
                    client = get_rmapi()
                    _invalidate_client_cache(client)

                    result = {
                        "uploaded": True,
                        "name": name,
                        "format": ext,
                        "transport": "usb-web",
                    }
                    if parent_folder != "/":
                        result["note"] = (
                            "USB web ignores parent_folder. The tablet web "
                            "service chooses the destination folder. Use SSH or "
                            "cloud mode for explicit folder placement."
                        )
                    return make_response(
                        result,
                        "Document uploaded via USB web interface. "
                        "Use remarkable_browse() to verify.",
                    )

                # SSH mode: full upload with metadata
                ssh_client = _get_ssh_client()
                collection = ssh_client.get_meta_items()
                items_by_id = get_items_by_id(collection)

                # Resolve parent folder
                parent_id = _resolve_parent_id(parent_folder, items_by_id, collection)
                if parent_id is None:
                    folders = [get_item_path(i, items_by_id) for i in collection if i.is_folder]
                    return make_error(
                        error_type="folder_not_found",
                        message=f"Folder not found: '{parent_folder}'",
                        suggestion="Use remarkable_browse('/') to see available folders.",
                        did_you_mean=folders[:5] if folders else None,
                    )

                # Generate UUID and set name
                doc_uuid = str(uuid.uuid4())
                name = document_name or os.path.splitext(os.path.basename(file_path))[0]
                timestamp_ms = str(int(time.time() * 1000))

                # Upload the file
                remote_file = f"{XOCHITL_PATH}/{doc_uuid}.{ext}"
                _upload_file_bytes(ssh_client, file_path, remote_file)

                # Create metadata
                metadata = {
                    "visibleName": name,
                    "type": "DocumentType",
                    "parent": parent_id,
                    "deleted": False,
                    "pinned": False,
                    "lastModified": timestamp_ms,
                    "metadatamodified": True,
                    "modified": True,
                    "synced": False,
                    "version": 0,
                }
                _write_metadata(ssh_client, doc_uuid, metadata)

                # Create content file
                content_data = {
                    "fileType": ext,
                }
                _write_content_file(ssh_client, doc_uuid, content_data)

                # Create the document directory (required by xochitl)
                ssh_client._ssh_command(f"mkdir -p '{XOCHITL_PATH}/{doc_uuid}'")
                ssh_client.mark_operation_dirty()

                # Restart xochitl to pick up changes (unless deferred for a batch)
                restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                cached_doc = Document(
                    id=doc_uuid,
                    hash=doc_uuid,
                    name=name,
                    doc_type="DocumentType",
                    parent=parent_id,
                    synced=False,
                    last_modified=datetime.now(),
                    local_path=f"{XOCHITL_PATH}/{doc_uuid}",
                )
                _update_deferred_ssh_cache(
                    ssh_client, restarted, document=cached_doc, file_type=ext
                )

                return make_response(
                    {
                        "uploaded": True,
                        "name": name,
                        "uuid": doc_uuid,
                        "format": ext,
                        "parent_folder": parent_folder,
                        "remote_path": remote_file,
                        "transport": "ssh",
                        "refresh_pending": not restarted,
                    },
                    _write_result_message(
                        restarted,
                        "Document uploaded successfully.",
                        "Use remarkable_browse() to verify it appears.",
                    ),
                )

            except SSHReliabilityError:
                raise
            except Exception as e:
                transport = "USB web" if _is_usb_web_mode() else "SSH"
                return make_error(
                    error_type="upload_failed",
                    message=f"Upload failed: {e}",
                    suggestion=f"Check {transport} connection and try again.",
                )

        if _is_ssh_mode():
            ssh_client = _get_ssh_client()
            if isinstance(ssh_client, SSHClient):
                return await _run_ssh_tool_operation(
                    ssh_client,
                    "upload",
                    _impl,
                    defer_restart=defer_restart,
                )
        if _is_usb_web_mode():
            usb_client = get_rmapi()
            run_operation = getattr(type(usb_client), "run_operation_async", None)
            if callable(run_operation):
                return await run_operation(usb_client, "upload", _impl)
        return await asyncio.to_thread(_impl)

    async def _upload_tool(
        file_path: Optional[str] = None,
        parent_folder: str = "/",
        document_name: Optional[str] = None,
        defer_restart: bool = False,
        file_content: Optional[str] = None,
        file_format: str = "pdf",
        trash_source_after_upload: bool = False,
    ) -> str:
        """
        <usecase>Upload a PDF or EPUB file to the reMarkable tablet.</usecase>
        <instructions>
        Uploads a PDF or EPUB to the tablet, given either a path on the machine
        running this server or the file's contents. Give exactly one of:
        - file_path: a file on the server's machine. Use this when the server
          can read the file, and for files larger than the inline limit.
        - file_content: the file's bytes, base64-encoded. Use this when the
          caller cannot place files on the server's disk. Decoded size is
          limited to 2 MB by default (REMARKABLE_MAX_INLINE_BYTES); use
          file_path for anything larger. Requires document_name.

        Works in all three transports:
        - Cloud: uploaded via the sync protocol; supports parent_folder + document_name
        - SSH: transferred over SSH, metadata created; supports parent_folder + document_name
        - USB web: uploaded via POST /upload. The multipart filename carries
          document_name, parent_folder is ignored, and the tablet web service
          chooses the destination folder.

        The result also reports source ("path" or "content") and size_bytes.

        Requires write mode (the default; disabled with --read-only).
        </instructions>
        <parameters>
        - file_path: Absolute path to the local PDF or EPUB file
        - parent_folder: Destination folder path on tablet (default: root "/").
          Honored in cloud and SSH modes; ignored by the USB web interface.
        - document_name: Display name on tablet (default: filename without
          extension). Required with file_content. Honored in every mode; USB web
          uses it as the uploaded filename.
        - defer_restart: SSH only. When True, skip the xochitl restart that
          normally makes the upload visible. Use this for every upload in a
          batch, then call remarkable_refresh() ONCE at the end. Each restart
          forces a full document-store rescan that briefly destabilises the SSH
          link, so deferring turns N restarts (and N races) into one. The
          response sets "refresh_pending": true while a refresh is owed. Ignored
          in cloud/USB modes (no xochitl).
        - file_content: Base64-encoded file bytes, instead of file_path. Line
          breaks and a "data:application/pdf;base64," prefix are accepted.
        - file_format: "pdf" (default) or "epub"; the format of file_content.
          The decoded bytes must match it.
        </parameters>
        <examples>
        - remarkable_upload("/tmp/paper.pdf")
        - remarkable_upload("/tmp/book.epub", parent_folder="/Books")
        - remarkable_upload("/tmp/report.pdf", document_name="Q4 Report")
        - remarkable_upload(file_content="JVBERi0xLjQK...", document_name="Notes",
          parent_folder="/Course")
        - # batch import: defer every upload, refresh once at the end
          remarkable_upload("/tmp/a.pdf", defer_restart=True)
          remarkable_upload("/tmp/b.pdf", defer_restart=True)
          remarkable_refresh()
        </examples>
        """
        import tempfile

        def invalid(message: str, suggestion: str) -> str:
            return make_error(
                error_type="invalid_parameter", message=message, suggestion=suggestion
            )

        if (file_path is None) == (file_content is None):
            return invalid(
                "Give exactly one of file_path or file_content.",
                "Use file_path for a file on this machine, or file_content for "
                "base64-encoded bytes (up to the inline size limit).",
            )

        if file_content is not None:
            if trash_source_after_upload:
                return invalid(
                    "trash_source_after_upload applies only to file_path uploads.",
                    "Omit trash_source_after_upload when using file_content.",
                )
            if not document_name or not document_name.strip():
                return invalid(
                    "document_name is required with file_content.",
                    "Pass the display name the document should have on the tablet.",
                )
            fmt = (file_format or "").strip().lower()
            if fmt not in UPLOAD_FORMATS:
                return invalid(
                    f"Unsupported file_format: '{file_format}'",
                    "Use file_format='pdf' or file_format='epub'.",
                )
            try:
                data = _decode_inline_file(file_content, fmt)
            except ValueError as e:
                return invalid(
                    str(e),
                    "Send the whole file base64-encoded, or use file_path for large files.",
                )
            with tempfile.TemporaryDirectory(prefix="remarkable-upload-") as temp_dir:
                temp_path = os.path.join(temp_dir, f"document.{fmt}")
                with open(temp_path, "wb") as temp_file:
                    temp_file.write(data)
                response = await _upload_path(
                    temp_path, parent_folder, document_name, defer_restart
                )
            return _add_upload_source(response, {"source": "content", "size_bytes": len(data)})

        source = None
        uploaded_sha256 = None
        if trash_source_after_upload:
            try:
                source = _staged_source(file_path)
                uploaded_sha256 = await asyncio.to_thread(_file_sha256, source)
            except (ValueError, OSError) as e:
                return invalid(
                    str(e),
                    "Upload without trash_source_after_upload, or put the file directly "
                    "in the staging folder.",
                )
        size = os.path.getsize(file_path) if os.path.isfile(file_path) else None
        response = await _upload_path(file_path, parent_folder, document_name, defer_restart)
        fields = {"source": "path", "size_bytes": size}
        if source is not None:
            # Only a confirmed upload releases the source; errors and partial
            # writes keep it.
            if json.loads(response).get("uploaded") is True:
                fields.update(
                    await asyncio.to_thread(_trash_uploaded_source, source, uploaded_sha256)
                )
            else:
                fields.update(
                    {"source_trashed": False, "source_kept_reason": "The upload did not succeed."}
                )
        return _add_upload_source(response, fields)

    upload_doc = _upload_tool.__doc__
    staging_dir = upload_staging_dir()
    if staging_dir:

        async def remarkable_upload(
            file_path: Optional[str] = None,
            parent_folder: str = "/",
            document_name: Optional[str] = None,
            defer_restart: bool = False,
            file_content: Optional[str] = None,
            file_format: str = "pdf",
            trash_source_after_upload: bool = False,
        ) -> str:
            return await _upload_tool(
                file_path,
                parent_folder,
                document_name,
                defer_restart,
                file_content,
                file_format,
                trash_source_after_upload,
            )

        # Docstrings may or may not keep their indentation (Python 3.13 strips
        # it), so the option's lines take the indentation of </parameters>.
        trash_param = (
            "- trash_source_after_upload: file_path only. When True, move the\n"
            "  source file to the Trash after a successful upload, so the caller\n"
            "  needs no delete permission. Only for files directly inside the\n"
            f"  staging folder {staging_dir}; the file is kept if the upload fails\n"
            "  or the file changed during it. The result reports source_trashed\n"
            "  (and source_kept_reason when kept). Use it only when asked to clean\n"
            "  up the staged copy.\n"
        )
        remarkable_upload.__doc__ = re.sub(
            r"^([ \t]*)</parameters>",
            lambda m: "".join(m.group(1) + line for line in trash_param.splitlines(True))
            + m.group(0),
            upload_doc,
            count=1,
            flags=re.M,
        )
        # The staging option can remove a local file (recoverably, via the Trash).
        upload_annotations = ToolAnnotations(read_only_hint=False, destructive_hint=True)
    else:

        async def remarkable_upload(
            file_path: Optional[str] = None,
            parent_folder: str = "/",
            document_name: Optional[str] = None,
            defer_restart: bool = False,
            file_content: Optional[str] = None,
            file_format: str = "pdf",
        ) -> str:
            return await _upload_tool(
                file_path, parent_folder, document_name, defer_restart, file_content, file_format
            )

        remarkable_upload.__doc__ = upload_doc
        upload_annotations = UPLOAD_ANNOTATIONS

    mcp.tool(annotations=upload_annotations)(remarkable_upload)

    @mcp.tool(annotations=UPLOAD_ANNOTATIONS)
    async def remarkable_markdown_to_pdf(
        markdown: str,
        document_name: str = "Markdown",
        parent_folder: str = "/",
        defer_restart: bool = False,
    ) -> str:
        """
        <usecase>Render Markdown text as a PDF and upload it to the reMarkable tablet.</usecase>
        <instructions>
        Use this when the user wants notes, reports, summaries, or other Markdown
        content written back to the tablet without first creating a local file.
        Headings, lists, tables, links, blockquotes, and code blocks are rendered
        into a paginated A4 PDF. Raw HTML is escaped and image assets are omitted
        so rendering never reads local files or fetches remote content.

        Works in all three transports:
        - Cloud: sync upload with parent_folder and document_name
        - SSH: filesystem upload with parent_folder and document_name
        - USB web: upload whose multipart filename is document_name + ".pdf";
          parent_folder is ignored and the tablet web service chooses the folder.

        Requires write mode (the default; disabled with --read-only).
        </instructions>
        <parameters>
        - markdown: Markdown source text. Must not be empty.
        - document_name: Display name on the tablet (default: "Markdown").
        - parent_folder: Destination folder (default: root "/"). USB web ignores
          this parameter because its upload endpoint has no folder argument.
        - defer_restart: SSH only. When True, skip the xochitl restart and call
          remarkable_refresh() once after the batch. Forwarded exactly like
          remarkable_upload; ignored in cloud and USB web modes.
        </parameters>
        <examples>
        - remarkable_markdown_to_pdf("# Meeting Notes\\n\\n- Follow up with Sam")
        - remarkable_markdown_to_pdf(
            "# Q4 Report\\n\\nRevenue increased **20%**.",
            document_name="Q4 Report",
            parent_folder="/Reports",
          )
        - remarkable_markdown_to_pdf(
            "# Batch item", document_name="Item 1", defer_restart=True
          )
          remarkable_refresh()
        </examples>
        """
        from remarkable_mcp.markdown_pdf import render_markdown_pdf

        try:
            pdf_bytes = await asyncio.to_thread(render_markdown_pdf, markdown)
        except Exception as e:
            return make_error(
                error_type="render_failed",
                message=f"Failed to render Markdown as PDF: {e}",
                suggestion="Provide non-empty Markdown text and try again.",
            )

        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = os.path.join(temp_dir, "document.pdf")
            with open(pdf_path, "wb") as pdf_file:
                pdf_file.write(pdf_bytes)
            return await _upload_path(pdf_path, parent_folder, document_name, defer_restart)

    if _is_ssh_mode() or _is_cloud_mode():

        @mcp.tool(annotations=MKDIR_ANNOTATIONS)
        async def remarkable_mkdir(
            folder_name: str,
            parent: str = "/",
            defer_restart: bool = False,
        ) -> str:
            """
            <usecase>Create a new folder on the reMarkable tablet.</usecase>
            <instructions>
            Creates a folder in the tablet's document hierarchy. In SSH mode the
            folder appears after xochitl restarts; in cloud mode it syncs to all
            your devices.

            Works in cloud and SSH modes (default; --read-only disables). Not available over the
            USB web interface (the firmware exposes no folder-create endpoint).
            </instructions>
            <parameters>
            - folder_name: Name of the new folder
            - parent: Parent folder path (default: root "/")
            - defer_restart: SSH only. Skip the xochitl restart so a batch of
              writes can refresh once via remarkable_refresh() instead of once
              per write. Sets "refresh_pending": true. Ignored in cloud mode.
            </parameters>
            <examples>
            - remarkable_mkdir("Projects")
            - remarkable_mkdir("2024", parent="/Archive")
            </examples>
            """

            def _impl() -> str:
                error = _require_managed_write_mode()
                if error:
                    return error

                if _is_cloud_mode():
                    return _cloud_mkdir(folder_name, parent)

                try:
                    ssh_client = _get_ssh_client()
                    collection = ssh_client.get_meta_items()
                    items_by_id = get_items_by_id(collection)

                    # Resolve parent
                    parent_id = _resolve_parent_id(parent, items_by_id, collection)
                    if parent_id is None:
                        return make_error(
                            error_type="folder_not_found",
                            message=f"Parent folder not found: '{parent}'",
                            suggestion="Use remarkable_browse('/') to see available folders.",
                        )

                    # Generate UUID
                    doc_uuid = str(uuid.uuid4())
                    timestamp_ms = str(int(time.time() * 1000))

                    # Create metadata for folder
                    metadata = {
                        "visibleName": folder_name,
                        "type": "CollectionType",
                        "parent": parent_id,
                        "deleted": False,
                        "pinned": False,
                        "lastModified": timestamp_ms,
                        "metadatamodified": True,
                        "modified": True,
                        "synced": False,
                        "version": 0,
                    }
                    _write_metadata(ssh_client, doc_uuid, metadata)

                    # Restart xochitl (unless deferred for a batch)
                    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                    cached_folder = Document(
                        id=doc_uuid,
                        hash=doc_uuid,
                        name=folder_name,
                        doc_type="CollectionType",
                        parent=parent_id,
                        synced=False,
                        last_modified=datetime.now(),
                        local_path=f"{XOCHITL_PATH}/{doc_uuid}",
                    )
                    _update_deferred_ssh_cache(ssh_client, restarted, document=cached_folder)

                    return make_response(
                        {
                            "created": True,
                            "folder_name": folder_name,
                            "uuid": doc_uuid,
                            "parent": parent,
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(restarted, "Folder created."),
                    )

                except SSHReliabilityError:
                    raise
                except Exception as e:
                    return make_error(
                        error_type="mkdir_failed",
                        message=f"Failed to create folder: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            if _is_ssh_mode():
                ssh_client = _get_ssh_client()
                if isinstance(ssh_client, SSHClient):
                    return await _run_ssh_tool_operation(
                        ssh_client,
                        "mkdir",
                        _impl,
                        defer_restart=defer_restart,
                    )
            return await asyncio.to_thread(_impl)

        @mcp.tool(annotations=MOVE_ANNOTATIONS)
        async def remarkable_move(
            document: str,
            dest_folder: str,
            defer_restart: bool = False,
        ) -> str:
            """
            <usecase>Move a document or folder to a different location.</usecase>
            <instructions>
            Moves a document or folder by updating its parent reference in the metadata.
            Find the document name with remarkable_browse() first.

            Works in cloud and SSH modes (default; --read-only disables). Not available over the
            USB web interface.
            </instructions>
            <parameters>
            - document: Name or path of the document/folder to move
            - dest_folder: Destination folder path (use "/" for root)
            - defer_restart: SSH only. Skip the xochitl restart so a batch of
              writes can refresh once via remarkable_refresh() instead of once
              per write. Sets "refresh_pending": true. Ignored in cloud mode.
            </parameters>
            <examples>
            - remarkable_move("Meeting Notes", "/Archive")
            - remarkable_move("Old Project", "/Archive/2023")
            </examples>
            """

            def _impl() -> str:
                error = _require_managed_write_mode()
                if error:
                    return error

                if _is_cloud_mode():
                    return _cloud_move(document, dest_folder)

                try:
                    ssh_client = _get_ssh_client()
                    collection = ssh_client.get_meta_items()
                    items_by_id = get_items_by_id(collection)

                    # Find the document
                    target = _resolve_document(document, collection, items_by_id)
                    if not target:
                        from remarkable_mcp.extract import find_similar_documents

                        similar = find_similar_documents(document, collection)
                        return make_error(
                            error_type="document_not_found",
                            message=f"Document not found: '{document}'",
                            suggestion="Use remarkable_browse() to find the correct name.",
                            did_you_mean=similar if similar else None,
                        )

                    # Find the destination folder
                    dest_id = _resolve_parent_id(dest_folder, items_by_id, collection)
                    if dest_id is None:
                        return make_error(
                            error_type="folder_not_found",
                            message=f"Destination folder not found: '{dest_folder}'",
                            suggestion="Use remarkable_browse('/') to see available folders.",
                        )

                    # Prevent moving a folder into itself or a descendant
                    if target.is_folder:
                        if dest_id == target.ID:
                            return make_error(
                                error_type="invalid_move",
                                message="Cannot move a folder into itself",
                                suggestion="Choose a different destination folder.",
                            )
                        # Walk up from dest to check for cycles
                        check_id = dest_id
                        while check_id and check_id in items_by_id:
                            if check_id == target.ID:
                                return make_error(
                                    error_type="invalid_move",
                                    message="Cannot move a folder into one of its subfolders",
                                    suggestion=(
                                        "Choose a destination that is not inside the folder "
                                        "being moved."
                                    ),
                                )
                            parent_item = items_by_id[check_id]
                            check_id = parent_item.Parent if hasattr(parent_item, "Parent") else ""

                    # Read existing metadata
                    meta_content = ssh_client._scp_download(f"{XOCHITL_PATH}/{target.ID}.metadata")
                    metadata = json.loads(meta_content.decode("utf-8"))

                    old_path = get_item_path(target, items_by_id)

                    # Update parent
                    metadata["parent"] = dest_id
                    metadata["lastModified"] = str(int(time.time() * 1000))
                    metadata["metadatamodified"] = True
                    _write_metadata(ssh_client, target.ID, metadata)

                    # Restart xochitl (unless deferred for a batch)
                    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                    target.parent = dest_id
                    target.last_modified = datetime.now()
                    _update_deferred_ssh_cache(ssh_client, restarted, document=target)

                    return make_response(
                        {
                            "moved": True,
                            "name": target.VissibleName,
                            "from": old_path,
                            "to": dest_folder,
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(restarted, "Document moved."),
                    )

                except SSHReliabilityError:
                    raise
                except Exception as e:
                    return make_error(
                        error_type="move_failed",
                        message=f"Failed to move document: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            if _is_ssh_mode():
                ssh_client = _get_ssh_client()
                if isinstance(ssh_client, SSHClient):
                    return await _run_ssh_tool_operation(
                        ssh_client,
                        "move",
                        _impl,
                        defer_restart=defer_restart,
                    )
            return await asyncio.to_thread(_impl)

        @mcp.tool(annotations=RENAME_ANNOTATIONS)
        async def remarkable_rename(
            document: str,
            new_name: str,
            defer_restart: bool = False,
        ) -> str:
            """
            <usecase>Rename a document or folder on the reMarkable tablet.</usecase>
            <instructions>
            Changes the display name of a document or folder by updating its metadata.
            Find the document name with remarkable_browse() first.

            Works in cloud and SSH modes (default; --read-only disables). Not available over the
            USB web interface.
            </instructions>
            <parameters>
            - document: Current name or path of the document/folder
            - new_name: New display name
            - defer_restart: SSH only. Skip the xochitl restart so a batch of
              writes can refresh once via remarkable_refresh() instead of once
              per write. Sets "refresh_pending": true. Ignored in cloud mode.
            </parameters>
            <examples>
            - remarkable_rename("Untitled", "Meeting Notes 2024-01-15")
            - remarkable_rename("/Work/Draft", "Final Report")
            </examples>
            """

            def _impl() -> str:
                error = _require_managed_write_mode()
                if error:
                    return error

                if _is_cloud_mode():
                    return _cloud_rename(document, new_name)

                try:
                    ssh_client = _get_ssh_client()
                    collection = ssh_client.get_meta_items()
                    items_by_id = get_items_by_id(collection)

                    # Find the document
                    target = _resolve_document(document, collection, items_by_id)
                    if not target:
                        from remarkable_mcp.extract import find_similar_documents

                        similar = find_similar_documents(document, collection)
                        return make_error(
                            error_type="document_not_found",
                            message=f"Document not found: '{document}'",
                            suggestion="Use remarkable_browse() to find the correct name.",
                            did_you_mean=similar if similar else None,
                        )

                    # Read existing metadata
                    meta_content = ssh_client._scp_download(f"{XOCHITL_PATH}/{target.ID}.metadata")
                    metadata = json.loads(meta_content.decode("utf-8"))

                    old_name = metadata.get("visibleName", target.VissibleName)

                    # Update name
                    metadata["visibleName"] = new_name
                    metadata["lastModified"] = str(int(time.time() * 1000))
                    metadata["metadatamodified"] = True
                    _write_metadata(ssh_client, target.ID, metadata)

                    # Restart xochitl (unless deferred for a batch)
                    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                    target.name = new_name
                    target.last_modified = datetime.now()
                    _update_deferred_ssh_cache(ssh_client, restarted, document=target)

                    return make_response(
                        {
                            "renamed": True,
                            "old_name": old_name,
                            "new_name": new_name,
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(restarted, "Document renamed."),
                    )

                except SSHReliabilityError:
                    raise
                except Exception as e:
                    return make_error(
                        error_type="rename_failed",
                        message=f"Failed to rename document: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            if _is_ssh_mode():
                ssh_client = _get_ssh_client()
                if isinstance(ssh_client, SSHClient):
                    return await _run_ssh_tool_operation(
                        ssh_client,
                        "rename",
                        _impl,
                        defer_restart=defer_restart,
                    )
            return await asyncio.to_thread(_impl)

        @mcp.tool(annotations=WRITE_ANNOTATIONS)
        async def remarkable_tag(
            document: Union[str, List[str]],
            add: Optional[Union[str, List[str]]] = None,
            remove: Optional[Union[str, List[str]]] = None,
            rename: Optional[Dict[str, str]] = None,
            page: Optional[int] = None,
            defer_restart: bool = False,
        ) -> str:
            """
            <usecase>Add, rename or remove tags on reMarkable documents or pages.</usecase>
            <instructions>
            Changes document tags, or with `page` that page's tags. Give a list
            of documents to make the same changes on each in one write; every
            name is checked first, and if any cannot be found nothing changes.
            Tag names
            match case-insensitively, and a new tag reuses the spelling of a tag
            already used in the library. Renames run first, then removals, then
            additions. With no changes requested it reports the current tags.
            The response lists the resulting tags and what changed.

            Tag changes replace the tablet's copy of the document's tag list:
            tag edits made on the tablet that have not synced yet are lost, as
            with the reMarkable desktop app. Avoid tagging a document the user
            is editing on the tablet while it is offline.

            Works in cloud and SSH modes (default; --read-only disables). Not
            available over the USB web interface. Folders cannot be tagged.
            </instructions>
            <parameters>
            - document: Document name or path, or a list of them
            - add: Tag name or list of names to add
            - remove: Tag name or list of names to remove
            - rename: Mapping of current name to new name, e.g. {"todo": "done"}.
              Renaming onto a tag the document already has merges the two.
            - page: 1-based page number to change page tags instead of
              document tags
            - defer_restart: SSH only. Skip the xochitl restart so a batch of
              writes can refresh once via remarkable_refresh(). Ignored in
              cloud mode.
            </parameters>
            <examples>
            - remarkable_tag("Week 3 worksheet", add="to-grade")
            - remarkable_tag("Week 3 worksheet", rename={"to-grade": "graded"})
            - remarkable_tag("/Course/Notes", page=4, add=["review", "exam"])
            - remarkable_tag("Week 3 worksheet", remove="graded")
            - remarkable_tag(["Week 3 worksheet", "Week 4 worksheet"],
                             rename={"Done": "Graded"})
            - remarkable_tag("Week 3 worksheet")  # list current tags
            </examples>
            """
            from remarkable_mcp.tag_edit import TagEditError

            def invalid(message: str, suggestion: str) -> str:
                return make_error(
                    error_type="invalid_parameter", message=message, suggestion=suggestion
                )

            if page is not None and page < 1:
                return invalid("page must be 1 or more.", "Pages are numbered from 1.")
            if rename is not None and not isinstance(rename, dict):
                return invalid(
                    "rename must map current names to new names.",
                    'Example: rename={"to-grade": "graded"}',
                )
            adds, removes, renames = _as_list(add), _as_list(remove), rename or {}
            single = isinstance(document, str)
            names = [document] if single else list(document or [])
            if not names or not all(isinstance(n, str) and n for n in names):
                return invalid(
                    "document must be a name or a list of names.",
                    'Example: document=["Week 3 worksheet", "Week 4 worksheet"]',
                )

            def _impl() -> str:
                error = _require_managed_write_mode()
                if error:
                    return error

                cloud = _is_cloud_mode()
                client = get_rmapi() if cloud else _get_ssh_client()
                try:
                    collection = client.get_meta_items()
                    items_by_id = get_items_by_id(collection)
                    targets = []
                    for name in names:
                        target = _resolve_document(name, collection, items_by_id)
                        if not target:
                            from remarkable_mcp.extract import find_similar_documents

                            similar = find_similar_documents(name, collection)
                            return make_error(
                                error_type="document_not_found",
                                message=f"Document not found: '{name}'",
                                suggestion=(
                                    "Use remarkable_browse() to find the correct name. "
                                    "No tags were changed."
                                ),
                                did_you_mean=similar if similar else None,
                            )
                        if target.is_folder:
                            return invalid(
                                f"'{target.VissibleName}' is a folder; only documents have tags.",
                                "Tag the documents inside the folder instead. "
                                "No tags were changed.",
                            )
                        if all(t.ID != target.ID for t in targets):
                            targets.append(target)
                    apply = _tag_editor(
                        adds, removes, renames, page, _library_tag_names(collection)
                    )

                    if cloud:
                        results = client.update_documents_content([t.ID for t in targets], apply)
                        if any(r["changed"] for r in results.values()):
                            _invalidate_client_cache(client)
                        if single:
                            return _tag_response(targets[0], page, results[targets[0].ID], "cloud")
                        return _tags_response(targets, page, results, "cloud")
                    done = _ssh_tag(client, targets, apply, defer_restart)
                    if isinstance(done, str):
                        return done
                    results, pending = done
                    if single:
                        return _tag_response(
                            targets[0],
                            page,
                            results[targets[0].ID],
                            "ssh",
                            refresh_pending=pending,
                        )
                    return _tags_response(targets, page, results, "ssh", refresh_pending=pending)
                except TagEditError as e:
                    return invalid(str(e), "Check the tag names and page number.")
                except SSHReliabilityError:
                    raise
                except Exception as e:
                    return make_error(
                        error_type="tag_failed",
                        message=f"Failed to change tags: {e}",
                        suggestion="Check the connection and try again.",
                    )

            if _is_ssh_mode():
                ssh_client = _get_ssh_client()
                if isinstance(ssh_client, SSHClient):
                    return await _run_ssh_tool_operation(
                        ssh_client,
                        "tag",
                        _impl,
                        defer_restart=defer_restart,
                    )
            return await asyncio.to_thread(_impl)

        if _is_cloud_mode():

            @mcp.tool(annotations=WRITE_ANNOTATIONS)
            async def remarkable_mark_pdf(
                document: str,
                add: Optional[List[Dict]] = None,
                remove: Optional[Union[str, List[str]]] = None,
                clear: bool = False,
                color: Optional[str] = None,
                dry_run: bool = False,
                layout: Optional[int] = None,
            ):
                """
                <usecase>Add or remove marks on the pages of a PDF document: text,
                shapes, highlights, freeform paths, LaTeX equations and graphics.
                For example to grade or comment on a worksheet the user filled in,
                annotate a paper, or add diagrams and worked solutions.</usecase>
                <instructions>
                Marks are drawn into the document's PDF, under the user's ink,
                which is not changed. Each mark has an id; removing a mark
                rebuilds the PDF without it, so outdated marks can be replaced
                cleanly. With no changes requested it lists the current marks.
                Marks cannot be erased on the tablet, only with this tool (or
                hidden there, with a cover; see below).

                Positions are fractions of the page (0-1 from the top-left): a
                point is [x, y] and a region is [left, top, right, bottom], the
                same regions remarkable_image takes and layout, remarkable_read
                and this tool's listing return, so they can be passed straight
                back. Sizes and widths are in points. Before marking a
                page, call with layout=<page> for the positions of the PDF's text
                lines and of the user's ink on it, and look at the page with
                remarkable_image. Place marks beside the user's ink rather than
                on it, and use dry_run=True to check the result: it returns
                images of the changed pages without writing anything.

                Name marks after what they mark (e.g. "q3", "q3-note", "intro-hl")
                so they can be found and replaced later. Listings give each mark's
                content, color, position and style (size, bold, anchor, width
                and so on), so it can be re-created exactly. Any mark can take a
                description (up to 200 characters) saying what it is and why,
                for later sessions; it is shown when marks are listed and by
                remarkable_read. Describe path marks, which are otherwise only
                path data, e.g. "gold star: neat, complete working".

                Any mark can take "cover": true so the user can hide it on the
                tablet: white ink goes over it on a "Mark cover" layer at the
                bottom of the page, made hidden, and showing that layer hides
                the mark. A layer name instead of true (e.g. "Answer key")
                makes a group the user hides separately; "covered": true makes
                a new layer shown, so its marks start hidden (e.g. answers to
                reveal later). Listings give each covered mark's layer and
                state: hidden on tablet, shown on tablet, or cover erased on
                tablet. Treat marks the user has hidden as dismissed. Removing
                a mark removes its cover.

                Each mark in `add` has a `page` (1-based), an optional `id`, and
                exactly one of:
                - text: string ("\n" starts a new line); at, or region to wrap
                  within; options size (default 11), bold, italic, underline,
                  border (a rounded box with a white background)
                - shape: "check" or "cross" with at (option size) or region;
                  "circle", "box" or "highlight" with region, stretched to fill
                  it; "line" or "arrow" with from [x, y] and to [x, y].
                  Options: width (line width), opacity, dot (a dot at the to
                  end of a line or arrow)
                - path: SVG path data for any other shape, in any units. With
                  region it is scaled to fit, centered, keeping its aspect
                  (stretch: true fills the region instead); with at it is
                  scaled so its larger side is size points (default 12).
                  E.g. "M 0 10 L 5 0 L 10 10" is a caret. Options: width
                  (stays in points), fill (a color), opacity. Several subpaths
                  (each starting with M) share one style; for drawings with
                  several colors or text, or output from other tools, use
                  graphic.
                - latex: LaTeX math, e.g. "\\frac{a}{b} = \\sqrt{2}"; at, or
                  region to scale into; options size (default 12), border
                - graphic: content rendered elsewhere: SVG markup, or base64 PDF
                  (one page), PNG or JPEG (up to 2 MB); with region, scaled to
                  fit at its top-left. SVG may only reference its own elements
                  or data: URIs.
                - graphic_file: path of such a file on the machine running this
                  server (up to 10 MB), with region
                Instead of at or region, text, latex, check, cross and path marks
                can take near [x, y]: the mark is put in the closest free space to
                that point, clear of the user's ink, the printed text and other
                marks, and "leader": true draws a thin line from the mark to the
                point, ending in a dot; it is drawn only when the mark cannot sit
                beside the point; positions giving a diagonal leader are
                preferred. It starts at the middle of the note's top or bottom
                for a point above or below the note, otherwise
                just past the end that faces the point, at the text's top,
                middle or baseline, so it never crosses the note (a bordered
                note's leader starts at its border). Use near, with a leader, on
                pages full of writing; give the point of what the mark is about,
                at its edge (such as the end of an answer) rather than its
                middle. A leader never crosses the user's handwriting or other
                marks, and crosses printed text only when nothing else reaches
                the point; if no place fits, the tool says so: shorten the note
                or make it smaller.
                Marks placed with `at` take an optional anchor: the point of the
                mark placed at `at`: top-left, top, top-right, left, center,
                right, bottom-left, bottom or bottom-right. Default center for
                check and cross, top-left otherwise. Use anchor "left" with at
                at the right end of a text line's middle to put a note beside it.
                Every mark but graphics takes a color: #RRGGBB or one of the
                tablet's colors. Pen colors (all marks but highlights): black,
                gray, white, blue, red, green, yellow, cyan, magenta.
                Highlighter colors (highlights): yellow, blue, pink, orange,
                green, gray. The default is black, and yellow for highlights.
                Named colors are tuned to look right on the tablet's screen; PDF
                exports (remarkable_export) re-draw them in the desktop and
                browser apps' colors.
                Highlights darken like the tablet's highlighter: the color shows
                on the paper and text under it stays dark.

                Pick colors for the task. When grading, for example, use green
                for check marks and full scores, and red for X marks, notes about
                mistakes and partial scores.

                Removals run before additions, so a mark can be replaced in one
                call by removing its id and adding a new mark with that id.
                clear=True removes every mark and restores the original PDF.

                Cloud mode only. PDF documents only; pages added on the tablet
                have no PDF page and cannot be marked.
                </instructions>
                <parameters>
                - document: Document name or path
                - add: List of marks to add (see above)
                - remove: Mark id or list of ids to remove
                - clear: Remove every mark, restoring the original PDF
                - color: Color for marks in this call that set none: a pen or
                  highlighter color name (each mark takes the matching shade),
                  or #RRGGBB
                - dry_run: Return preview images of the changed pages without
                  writing
                - layout: Tablet page number; the response adds that page's text
                  lines (from the PDF before marks), each with its region, the
                  regions of the user's ink, and free: the largest empty regions
                  (clear of ink, text and marks), largest first. Not combined
                  with a write; use it alone or with dry_run.
                </parameters>
                <examples>
                - remarkable_mark_pdf("Worksheet", color="red", add=[
                    {"page": 1, "id": "score", "text": "3/4", "at": [0.8, 0.06],
                     "size": 24, "bold": true},
                    {"page": 1, "id": "q1", "shape": "check", "at": [0.38, 0.2],
                     "color": "green"},
                    {"page": 1, "id": "q3", "shape": "cross", "at": [0.38, 0.33]},
                    {"page": 1, "id": "q3-note", "text": "Orange is secondary.",
                     "at": [0.41, 0.33], "anchor": "left", "size": 9}],
                  dry_run=True)
                - remarkable_mark_pdf("Paper", add=[
                    {"page": 3, "id": "key-claim", "shape": "highlight",
                     "region": [0.12, 0.41, 0.88, 0.43]},
                    {"page": 3, "id": "key-claim-note", "text": "Compare with Fig. 2",
                     "at": [0.9, 0.42], "anchor": "right", "size": 8}])
                - remarkable_mark_pdf("Notes", add=[
                    {"page": 2, "id": "formula",
                     "latex": "x = \\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}",
                     "at": [0.45, 0.52]},
                    {"page": 2, "id": "formula-arrow", "shape": "arrow",
                     "from": [0.44, 0.53], "to": [0.36, 0.53]},
                    {"page": 2, "id": "brace", "path":
                     "M 0 0 C 8 0 4 20 10 20 C 4 20 8 40 0 40",
                     "region": [0.8, 0.3, 0.83, 0.38]}])
                - remarkable_mark_pdf("Worksheet", remove=["q3", "q3-note"], add=[
                    {"page": 1, "id": "q3", "shape": "check", "at": [0.38, 0.33],
                     "color": "green"}])  # the answer was revised
                - remarkable_mark_pdf("Worksheet", color="red", add=[
                    {"page": 2, "id": "q4-note", "text": "check the sign",
                     "near": [0.62, 0.41], "leader": true, "size": 9}])
                - remarkable_mark_pdf("Worksheet", layout=1)  # where to place marks
                - remarkable_mark_pdf("Worksheet")  # list marks
                - remarkable_mark_pdf("Worksheet", clear=True)
                </examples>
                """
                from remarkable_mcp.pdf_marks import MarkError

                def invalid(message: str, suggestion: str) -> str:
                    return make_error(
                        error_type="invalid_parameter", message=message, suggestion=suggestion
                    )

                adds, removes = add or [], _as_list(remove)
                writing = bool(adds or removes or clear) and not dry_run
                if layout is not None and writing:
                    return invalid(
                        "layout cannot be combined with a write.",
                        "Call with layout alone (or with dry_run=True) first, then write.",
                    )

                def _impl():
                    error = _require_managed_write_mode()
                    if error:
                        return error
                    client = get_rmapi()
                    try:
                        collection = client.get_meta_items()
                        items_by_id = get_items_by_id(collection)
                        target = _resolve_document(document, collection, items_by_id)
                        if not target:
                            from remarkable_mcp.extract import find_similar_documents

                            similar = find_similar_documents(document, collection)
                            return make_error(
                                error_type="document_not_found",
                                message=f"Document not found: '{document}'",
                                suggestion="Use remarkable_browse() to find the correct name.",
                                did_you_mean=similar if similar else None,
                            )
                        if target.is_folder:
                            return invalid(
                                f"'{target.VissibleName}' is a folder.",
                                "Mark a PDF document instead.",
                            )
                        obstacles = None
                        if any(isinstance(m, dict) and "near" in m for m in adds):
                            near_pdf, _, near_files = client.read_document_pdf(target.ID)
                            obstacles = _page_obstacles(client, near_files, near_pdf)
                        edit = _mark_editor(
                            adds, removes, clear, color, build=writing, obstacles=obstacles
                        )
                        if writing:
                            result = client.replace_document_pdf(target.ID, edit, with_ink=True)
                            if result["changed"]:
                                _invalidate_client_cache(client)
                            return _mark_response(target, result, dry_run=False)
                        pdf, content, files = client.read_document_pdf(target.ID)
                        result = edit(pdf, content, _ink_reader(client, target.ID, files))
                        if layout is not None:
                            result["layout"] = _mark_layout(
                                client, files, pdf, layout, result["page_map"]
                            )
                        response = _mark_response(target, result, dry_run=dry_run)
                        preview = result.get("preview_pdf")
                        if not dry_run or not preview:
                            return response
                        from mcp.types import TextContent

                        images = _mark_preview_images(
                            client, files, preview, result["touched_pages"]
                        )
                        return [TextContent(type="text", text=response), *images]
                    except MarkError as e:
                        return invalid(str(e), "Fix the mark and try again.")
                    except Exception as e:
                        return make_error(
                            error_type="mark_failed",
                            message=f"Failed to change marks: {e}",
                            suggestion="Check that the document is a PDF and try again.",
                        )

                return await asyncio.to_thread(_impl)

            @mcp.tool(annotations=WRITE_ANNOTATIONS)
            async def remarkable_pdf_outline(
                document: str,
                outline: Optional[Union[List[Dict[str, Any]], str]] = None,
                dry_run: bool = False,
            ):
                """
                <usecase>Read, set or remove a PDF document's outline: the table of
                contents the tablet shows for it, e.g. chapter entries for a book
                that has none.</usecase>
                <instructions>
                With no outline, lists the document's current outline. Each entry
                gives its level, title, tablet page and the first lines of text on
                that page (running headers and page numbers left out), so it can
                be checked against the page it points to.

                outline replaces the whole outline: a list of entries
                {"title": ..., "page": <tablet page, 1-based>, "level": <1 for
                top-level, 2 for an entry under the one before, ...>} in reading
                order; level defaults to 1 and may go at most one deeper than the
                entry before it. [] removes the outline, and "original" restores
                the PDF's own (or none, if it had none).

                To build one for a book, find the chapter headings with
                remarkable_read (its grep option finds lines such as "Chapter"),
                then set every entry in one call. Use dry_run=True to check the
                entries, with each page's first lines, before writing.

                The PDF's pages, the user's ink and marks added with
                remarkable_mark_pdf are not changed; the tablet shows the new
                table of contents after its next sync. Pages added on the tablet
                are not in the PDF and cannot be outline targets.

                Cloud mode only. PDF documents only.
                </instructions>
                <parameters>
                - document: Document name or path
                - outline: List of entries, [] to remove the outline, or "original"
                - dry_run: Return the resulting outline without writing
                </parameters>
                <examples>
                - remarkable_pdf_outline("Moby Dick")  # list the outline
                - remarkable_pdf_outline("Moby Dick", outline=[
                    {"title": "Etymology", "page": 3},
                    {"title": "Chapter 1: Loomings", "page": 9},
                    {"title": "Chapter 2: The Carpet-Bag", "page": 15}], dry_run=True)
                - remarkable_pdf_outline("Field Guide", outline=[
                    {"title": "Part I: Birds", "page": 5},
                    {"title": "Raptors", "page": 7, "level": 2}])
                - remarkable_pdf_outline("Moby Dick", outline="original")
                </examples>
                """
                from remarkable_mcp.pdf_marks import MarkError

                def invalid(message: str, suggestion: str) -> str:
                    return make_error(
                        error_type="invalid_parameter", message=message, suggestion=suggestion
                    )

                if isinstance(outline, str) and outline != "original":
                    return invalid(
                        'outline must be a list of entries, [] or "original".',
                        'Example: outline=[{"title": "Chapter 1", "page": 3}]',
                    )
                writing = outline is not None and not dry_run

                def _impl():
                    error = _require_managed_write_mode()
                    if error:
                        return error
                    client = get_rmapi()
                    try:
                        collection = client.get_meta_items()
                        items_by_id = get_items_by_id(collection)
                        target = _resolve_document(document, collection, items_by_id)
                        if not target:
                            from remarkable_mcp.extract import find_similar_documents

                            similar = find_similar_documents(document, collection)
                            return make_error(
                                error_type="document_not_found",
                                message=f"Document not found: '{document}'",
                                suggestion="Use remarkable_browse() to find the correct name.",
                                did_you_mean=similar if similar else None,
                            )
                        if target.is_folder:
                            return invalid(
                                f"'{target.VissibleName}' is a folder.",
                                "Give a PDF document instead.",
                            )
                        edit = _outline_editor(outline, build=writing)
                        if writing:
                            result = client.replace_document_pdf(target.ID, edit)
                            if result["changed"]:
                                _invalidate_client_cache(client)
                        else:
                            pdf, content, _ = client.read_document_pdf(target.ID)
                            result = edit(pdf, content)
                        return _outline_response(target, result, dry_run)
                    except MarkError as e:
                        return invalid(str(e), "Fix the outline and try again.")
                    except Exception as e:
                        return make_error(
                            error_type="outline_failed",
                            message=f"Failed to change the outline: {e}",
                            suggestion="Check that the document is a PDF and try again.",
                        )

                return await asyncio.to_thread(_impl)

        @mcp.tool(annotations=DELETE_ANNOTATIONS)
        async def remarkable_delete(
            document: str,
            defer_restart: bool = False,
            permanent: bool = False,
            *,
            confirmation: Annotated[
                ElicitationResult[_DeleteConfirmation],
                Resolve(_resolve_delete_confirmation),
            ],
            confirmation_available: Annotated[bool, Resolve(_delete_confirmation_available)],
        ) -> str:
            """
            <usecase>Delete a document or folder on the reMarkable tablet.</usecase>
            <instructions>
            DESTRUCTIVE operation. By default the item is moved to the tablet's
            Trash and remains recoverable. In SSH mode only, permanent=True removes
            the resolved UUID and all sidecar files instead; use that only when the
            user explicitly asked for permanent deletion.

            Works in cloud and SSH modes (default; --read-only disables). Not available over the
            USB web interface.

            If the client supports elicitation, this tool asks the user to confirm
            before deleting. If the client cannot show a confirmation prompt, the
            delete is refused unless REMARKABLE_SKIP_CONFIRM=1 is set (for headless
            automation).
            </instructions>
            <parameters>
            - document: Name or path of the document/folder to delete
            - defer_restart: SSH only. Skip the xochitl restart so a batch of
              deletes can refresh once via remarkable_refresh() instead of once
              per delete. Sets "refresh_pending": true. Ignored in cloud mode.
            - permanent: SSH only. Permanently remove the resolved document/folder
              files instead of moving the item to Trash (default: False).
            </parameters>
            <examples>
            - remarkable_delete("Old Notes")
            - remarkable_delete("/Books/Archive/Old Draft")
            </examples>
            """
            error = _require_managed_write_mode()
            if error:
                return error

            abort = _delete_confirmation_abort(confirmation, confirmation_available, document)
            if abort:
                return abort

            if _is_cloud_mode():
                if permanent:
                    return make_error(
                        error_type="permanent_delete_unsupported",
                        message="Permanent deletion is not supported in cloud mode.",
                        suggestion=(
                            "Delete normally to move the item to Trash, then empty "
                            "Trash from a reMarkable app or tablet."
                        ),
                    )
                return await asyncio.to_thread(_cloud_delete, document)

            def _impl() -> str:
                try:
                    ssh_client = _get_ssh_client()
                    collection = ssh_client.get_meta_items()
                    items_by_id = get_items_by_id(collection)

                    # Find the document
                    target = _resolve_document(document, collection, items_by_id)
                    if not target:
                        from remarkable_mcp.extract import find_similar_documents

                        similar = find_similar_documents(document, collection)
                        return make_error(
                            error_type="document_not_found",
                            message=f"Document not found: '{document}'",
                            suggestion="Use remarkable_browse() to find the correct name.",
                            did_you_mean=similar if similar else None,
                        )

                    doc_path = get_item_path(target, items_by_id)

                    if permanent:
                        if target.is_folder and any(
                            item.Parent == target.ID for item in collection
                        ):
                            return make_error(
                                error_type="folder_not_empty",
                                message=(
                                    f"Cannot permanently delete non-empty folder "
                                    f"'{target.VissibleName}'."
                                ),
                                suggestion=(
                                    "Permanently delete its child documents and "
                                    "folders first, or omit permanent=True to move "
                                    "the whole folder to Trash."
                                ),
                            )
                        _permanently_delete_ssh_entry(ssh_client, target.ID)
                    else:
                        meta_content = ssh_client._scp_download(
                            f"{XOCHITL_PATH}/{target.ID}.metadata"
                        )
                        metadata = json.loads(meta_content.decode("utf-8"))
                        metadata["parent"] = "trash"
                        metadata["deleted"] = False
                        metadata["lastModified"] = str(int(time.time() * 1000))
                        metadata["metadatamodified"] = True
                        try:
                            metadata["version"] = int(metadata.get("version", 0)) + 1
                        except (TypeError, ValueError):
                            metadata["version"] = 1
                        _write_metadata(ssh_client, target.ID, metadata)

                    # Restart xochitl (unless deferred for a batch)
                    restarted = _maybe_restart_xochitl(ssh_client, defer_restart)

                    _update_deferred_ssh_cache(ssh_client, restarted, remove_id=target.ID)
                    _clear_document_extraction_cache(target.ID)

                    return make_response(
                        {
                            "deleted": True,
                            "name": target.VissibleName,
                            "path": doc_path,
                            "type": "folder" if target.is_folder else "document",
                            "permanent": permanent,
                            "refresh_pending": not restarted,
                        },
                        _write_result_message(
                            restarted,
                            (
                                "Document permanently deleted."
                                if permanent
                                else "Document moved to Trash."
                            ),
                            "It will no longer appear in the active library.",
                        ),
                    )

                except SSHReliabilityError:
                    raise
                except Exception as e:
                    return make_error(
                        error_type="delete_failed",
                        message=f"Failed to delete document: {e}",
                        suggestion="Check SSH connection and try again.",
                    )

            ssh_client = _get_ssh_client()
            if isinstance(ssh_client, SSHClient):
                return await _run_ssh_tool_operation(
                    ssh_client,
                    "delete",
                    _impl,
                    defer_restart=defer_restart,
                )
            return await asyncio.to_thread(_impl)

    async def remarkable_refresh() -> str:
        """
        <usecase>Restart xochitl once to apply writes made with
        defer_restart=True (or with REMARKABLE_DEFER_RESTART set).</usecase>
        <instructions>
        SSH only. Each write normally restarts xochitl so the change becomes
        visible, but every restart forces a full document-store rescan that
        briefly destabilises the SSH link. For a batch of writes, pass
        defer_restart=True to each one and then call this tool ONCE at the end:
        it pays the restart cost a single time instead of once per write,
        eliminating the per-write race and the redundant rescans.

        Once this server process has confirmed a clean refresh state, calling it
        again with nothing pending is a no-op. The first explicit refresh in a
        new process still restarts xochitl because pending work from an earlier
        process cannot be inferred safely from memory.
        Requires write mode (the default; disabled with --read-only).
        </instructions>
        <parameters>(none)</parameters>
        <examples>
        - remarkable_upload("/tmp/a.pdf", defer_restart=True)
          remarkable_upload("/tmp/b.pdf", defer_restart=True)
          remarkable_refresh()
        </examples>
        """

        error = _require_write_transport()
        if error:
            return error

        ssh_client = None
        try:
            ssh_client = _get_ssh_client()
            did_refresh = True
            refresh_pending = False
            if isinstance(ssh_client, SSHClient):
                did_refresh = await ssh_client._refresh_coordinator.refresh_explicit(
                    lambda: _refresh_ssh_client(ssh_client)
                )
                refresh_pending = ssh_client._refresh_coordinator.diagnostics()["deferred_dirty"]
            else:
                await asyncio.to_thread(_restart_xochitl, ssh_client)
                _invalidate_client_cache(ssh_client)
            if not did_refresh:
                return make_response(
                    {"refreshed": False, "transport": "ssh", "refresh_pending": False},
                    "No deferred SSH writes were pending, so xochitl was not restarted.",
                )
            if refresh_pending:
                return make_response(
                    {"refreshed": True, "transport": "ssh", "refresh_pending": True},
                    "xochitl restarted successfully, but a concurrent deferred write "
                    "finished outside that refresh barrier. Do not repeat the write; "
                    "call remarkable_refresh() once more.",
                )
            return make_response(
                {"refreshed": True, "transport": "ssh", "refresh_pending": False},
                "xochitl restarted and fresh SSH readiness was confirmed; "
                "any deferred changes are now visible. "
                "Use remarkable_browse() to verify.",
            )
        except Exception as e:
            if ssh_client is not None:
                _invalidate_client_cache(ssh_client)
            return _pending_refresh_error(
                error_type="refresh_failed",
                message=f"Failed to refresh tablet UI: {e}",
                suggestion=(
                    "Do not repeat any writes. Wake and unlock the tablet, verify a "
                    "fresh SSH connection, then retry only the refresh."
                ),
            )

    # Refreshing is meaningful only for the SSH transport's xochitl restart;
    # cloud/USB clients have no equivalent, so the tool is hidden there.
    if _is_ssh_mode():
        mcp.tool(annotations=WRITE_ANNOTATIONS)(remarkable_refresh)

"""
reMarkable MCP Server initialization.
"""

import logging
import os
import re
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from ipaddress import ip_address
from typing import AsyncIterator, List
from urllib.parse import quote, unquote

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent

from remarkable_mcp import ocr
from remarkable_mcp.responses import is_error_response

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class RemarkableMCP(MCPServer):
    """Custom MCP server that handles VS Code's URI quirks.

    VS Code:
    - Appends ?version=... to resource URIs for cache busting
    - May send URIs with spaces or URL-encoded (%20)

    Pydantic's AnyUrl stores URIs with URL-encoded paths, so we need to
    normalize incoming URIs to match.
    """

    async def read_resource(self, uri, context: Context | None = None):
        """Read a resource, normalizing the URI for lookup.

        Handles:
        - Query parameters: ?version=timestamp -> stripped
        - Spaces in path: encode to %20 to match stored URIs
        """
        uri_str = str(uri)

        # Strip query parameters (e.g., ?version=1764625282944)
        if "?" in uri_str:
            uri_str = uri_str.split("?")[0]
            logger.debug("Stripped query params from resource URI")

        # Normalize path encoding - Pydantic AnyUrl stores with %20 for spaces
        # VS Code may send either spaces or %20, so normalize to %20
        if ":///" in uri_str:
            scheme_end = uri_str.index(":///") + 4
            scheme = uri_str[:scheme_end]
            path = uri_str[scheme_end:]

            # First decode any existing encoding, then re-encode consistently
            # This handles both "November 2025" and "November%202025" inputs
            decoded_path = unquote(path)
            # quote with safe='/' preserves path separators but encodes spaces
            encoded_path = quote(decoded_path, safe="/:")
            uri_str = scheme + encoded_path
            logger.debug(f"Normalized resource URI path: {path} -> {encoded_path}")

        return await super().read_resource(uri_str, context)

    async def call_tool(self, name, arguments, context: Context | None = None):
        """Call a tool; a reply whose first text block is a JSON ``_error``
        object is returned with ``isError`` set, its body unchanged."""
        result = await super().call_tool(name, arguments, context)
        return mark_tool_error(result)


def mark_tool_error(result):
    """``result`` with ``is_error`` set when its first block is an error reply."""
    content = getattr(result, "content", None)
    if not content or getattr(result, "is_error", True):
        return result
    first = content[0]
    if isinstance(first, TextContent) and is_error_response(first.text):
        return result.model_copy(update={"is_error": True})
    return result


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def tool_summary(description: str) -> str:
    """The first sentence of a tool docstring's ``<usecase>`` block (or of
    its first paragraph when it has none), on one line."""
    match = re.search(r"<usecase>(.*?)</usecase>", description or "", re.S)
    text = match.group(1) if match else (description or "").strip().split("\n\n")[0]
    text = " ".join(text.split())
    sentence = re.match(r"(.+?)[.:!?](\s|$)", text)
    return (sentence.group(1) if sentence else text) + "."


def tool_parameters(parameters: dict) -> List[str]:
    """Parameter names a tool's input schema shows, without deprecated aliases."""
    properties = (parameters or {}).get("properties", {})
    return [name for name, schema in properties.items() if not schema.get("deprecated")]


def _tool_list(registered) -> str:
    """The "Available Tools" section, one line per registered tool."""
    lines = ["## Available Tools", ""]
    for tool in sorted(registered, key=lambda t: TOOL_ORDER.get(t.name, len(TOOL_ORDER))):
        params = ", ".join(tool_parameters(tool.parameters))
        lines.append(f"- `{tool.name}({params})` - {tool_summary(tool.description)}")
    return "\n".join(lines) + "\n"


# Reading tools first, then writes from the least to the most destructive.
TOOL_ORDER = {
    name: index
    for index, name in enumerate(
        [
            "remarkable_status",
            "remarkable_browse",
            "remarkable_search",
            "remarkable_recent",
            "remarkable_read",
            "remarkable_image",
            "remarkable_canvas",
            "remarkable_export",
            "remarkable_upload",
            "remarkable_markdown_to_pdf",
            "remarkable_mkdir",
            "remarkable_move",
            "remarkable_rename",
            "remarkable_tag",
            "remarkable_mark_pdf",
            "remarkable_pdf_outline",
            "remarkable_pdf_pages",
            "remarkable_pdf_ocr",
            "remarkable_author",
            "remarkable_delete",
            "remarkable_refresh",
        ]
    )
}


def _build_instructions(registered=None) -> str:
    """Build server instructions for the current configuration.

    ``registered`` are the tools to list (``mcp``'s registry when
    omitted); the workflow notes come from the configuration alone.
    """
    if registered is None:
        server = globals().get("mcp")
        registered = server._tool_manager.list_tools() if server is not None else []
    ssh_mode = _env_flag("REMARKABLE_USE_SSH")
    usb_web_mode = _env_flag("REMARKABLE_USE_USB_WEB")
    local_dir_mode = _env_flag("REMARKABLE_USE_LOCAL_DIR") or bool(
        os.environ.get("REMARKABLE_LOCAL_DIR")
    )
    # Local-directory mode is unconditionally read-only (the folder is the
    # desktop app's private sync cache).
    write_mode = not _env_flag("REMARKABLE_READ_ONLY") and not local_dir_mode
    names = {tool.name for tool in registered}

    read_only_note = "" if write_mode else " All tablet and library operations are read-only."
    instructions = (
        "# reMarkable MCP Server\n\n"
        f"Access documents from your reMarkable tablet.{read_only_note}\n\n"
        + _tool_list(registered)
        + """
Names and paths: a document is named by its name or its path ("/Folder/Name").
Paths are relative to the configured root folder, if any.

## Recommended Workflows

### Finding and Reading Documents
1. `remarkable_browse(query="keyword")` finds documents and folders by name
   (names only, not content); `remarkable_browse("/Folder", tags=["tag"])`
   finds tagged documents in a folder and its subfolders
2. `remarkable_search("keyword", grep="pattern")` reads documents whose names
   contain the keyword and returns the matches of the pattern;
   `remarkable_search(folder="/Folder", grep="pattern")` searches the content
   of every document in a folder whatever its name (up to 20 documents)
3. `remarkable_read("Document Name")` gets the text; a long document comes in
   parts (`part=2` continues; `more` and `next_part` say whether there is more)
4. `remarkable_read("Document", grep="pattern")` finds every match in one
   document, with the tablet page of each
5. `remarkable_recent()` lists what was modified recently

### Reading Annotations
`remarkable_read("Document", content_type="annotations")` lists each annotated
page with:
- highlights with their color, e.g. `[pink] passage text`
- freehand highlighter strokes with color and `region` (page fractions)
- page tags, and the page's layers when it has several or a hidden one
The response also returns `highlights`, `highlighter_strokes`, `page_tags` and
`layers` as structured data.

### Getting Page Images
Use `remarkable_image` when you need visual context: hand-drawn diagrams,
sketches, mockups, or anything text extraction might miss.
- `remarkable_image("Notes", page=2, dpi=300)` renders a page at 300 dpi,
  enough to read small handwriting
- `region=[left, top, right, bottom]` (page fractions) crops to part of the
  page; a highlighter stroke's `region` from remarkable_read can be passed
  directly
- `layers="all"` includes hidden layers; by default only visible layers are drawn
- `include_ocr=True` adds the page's handwriting as text
Notebook page templates (dots, lines, grids) are not drawn; remarkable_read
lists each page's template.

### Exporting
`remarkable_export` never modifies the tablet:
- one document (or `pages="3-5,9"` of a PDF export) as a temporary PDF or
  Markdown resource link that expires after 15 minutes
- with `save_to`, saved to a path inside REMARKABLE_EXPORT_DIRS instead
- with `folder` and/or `tags` and `save_to` a folder, every matching document
"""
    )
    instructions += """
### Page Layout, Marks and Outlines
- `remarkable_page_layout("Worksheet", page=1)` gives a page's printed text
  lines, boxes around the user's handwriting and free space, as regions
  `remarkable_image` can crop to
- `remarkable_read` returns a PDF's `marks` (added with remarkable_mark_pdf)
  and its `outline` (table of contents)
"""
    if "remarkable_pdf_outline" in names:
        instructions += (
            '- `remarkable_pdf_outline("Book", outline=[...])` replaces the table of'
            " contents the tablet shows\n"
        )
    instructions += """
### Combining Tools
- Browse -> Read: find documents first, then read them
- Recent -> Read: check what changed recently, then read specific documents
- Read with grep: find specific content within a large document
- Browse -> Image: find a document, then look at a page

## MCP Resources

- `remarkable:///{path}.txt` - full extracted text in one request
- `remarkableraw:///{path}.{pdf|epub}.txt` - original PDF/EPUB text
- `remarkableimg:///{path}.page-{N}.png` - PNG of page N (notebooks only)
- `remarkableexport:///{format}/{id}` - a temporary generated export
"""
    if "remarkable_mark_pdf" in names:
        instructions += (
            "- `remarkable://help/marks` - placement, leader, cover and erasable rules"
            " for remarkable_mark_pdf\n"
        )

    if local_dir_mode:
        instructions += """
## Local Directory Mode (Active)

Reading from a local reMarkable data directory (typically the official
desktop app's sync folder). Fully offline and device-free. Write tools are
disabled: the folder is the desktop app's sync cache. Content freshness
depends on the desktop app syncing (keep it running and signed in).
"""
    elif ssh_mode:
        instructions += """
## SSH Mode (Active)

Connected directly to the tablet over SSH: 10-100x faster than the cloud.
"""
        if write_mode:
            instructions += """
### Write Safety
- **Delete is destructive**: the MCP client should confirm with the user first
- Each write restarts xochitl so the tablet shows it. Concurrent writes are
  serialized and share one restart; every call waits for it before returning
- For a batch, pass `defer_restart=True` to each write (or set
  `REMARKABLE_DEFER_RESTART=1`) and call `remarkable_refresh()` once at the end
- Use `remarkable_browse()` to verify changes
- Run with `--read-only` to disable all write tools
"""
    elif usb_web_mode:
        instructions += """
## USB Web Mode (Active)

Connected through the tablet's USB web interface.
"""
        if write_mode:
            instructions += """Only uploads are available: the web interface
puts them in the folder open on the tablet. mkdir, move, rename, tag and
delete need SSH or cloud mode.
"""
    else:
        instructions += """
## Cloud Mode (Active)

Connected via the reMarkable Cloud API. Every read feature works, including
`content_type="raw"`; SSH mode (`uvx remarkable-mcp --ssh`) is faster.
"""
        if write_mode:
            instructions += """
### Write Safety
Writes change your cloud library and sync to all your devices.
- **Delete is destructive** (it moves items to the Trash): the MCP client
  should confirm with the user first
- Use `remarkable_browse()` to verify changes
- Run with `--read-only` to disable all write tools

### Marking Up a PDF (e.g. grading a worksheet)
1. `remarkable_image("Worksheet", page=1)` and `remarkable_read` to see the page
2. `remarkable_mark_pdf("Worksheet", layout_page=1)` for text line and ink positions
3. `remarkable_mark_pdf("Worksheet", add=[...], dry_run=True)` to preview, then
   repeat without dry_run. Marks are black by default; pick colors for the task
   (when grading: green for check marks and full scores, red for mistakes).
4. To update, remove outdated marks by id and add new ones in one call.
   `remarkable_read` lists current marks.
"""

    if ocr.preferred_backend() == "google":
        instructions += """
## OCR (Google Vision Selected)

Google Vision will be tried first. If it is unavailable or detects no text,
OCR falls back to local Tesseract.
Use `include_ocr=True` with `remarkable_read()` to extract handwritten content.
"""
    else:
        instructions += """
## OCR (Tesseract Active)

Tesseract will be used for local OCR but works poorly on handwriting.
For better handwriting recognition, configure GOOGLE_VISION_API_KEY and use
REMARKABLE_OCR_BACKEND=auto (the default) or google.
"""

    return instructions


def refresh_instructions() -> None:
    """Rebuild the server instructions from the tools registered now."""
    mcp._lowlevel_server.instructions = _build_instructions()


@asynccontextmanager
async def lifespan(app: MCPServer) -> AsyncIterator[None]:
    """Lifespan context manager for the MCP server."""
    import asyncio
    import os

    # Import here to avoid circular imports
    from remarkable_mcp.resources import (
        _is_ssh_mode,
        load_all_documents_sync,
        start_background_loader,
        stop_background_loader,
    )

    task = None
    ssh_mode = _is_ssh_mode()
    logger.info(f"REMARKABLE_USE_SSH env: {os.environ.get('REMARKABLE_USE_SSH')}")
    logger.info(f"SSH mode detected: {ssh_mode}")

    if ssh_mode:
        # SSH mode: load documents in the background so we do NOT block the MCP
        # initialize handshake. Tools query the device live, so they work before
        # the load finishes; this load only pre-populates browsable resources.
        logger.info("SSH mode: starting background document load...")

        async def _ssh_background_load():
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, load_all_documents_sync)
                logger.info("SSH mode: documents loaded")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"SSH mode: failed to load documents on startup: {e}")
                logger.warning("Server will start, but tools will show connection errors")

        task = asyncio.create_task(_ssh_background_load())
    else:
        # Cloud/USB/local-directory mode: load in background to not block startup
        logger.info("Cloud/USB/local-directory mode: starting background loader...")
        task = start_background_loader()

    try:
        yield
    finally:
        # Stop background loader on shutdown (if running)
        await stop_background_loader(task)
        from remarkable_mcp.api import close_device_client
        from remarkable_mcp.export_resources import cleanup_export_resources

        await close_device_client()
        cleanup_export_resources()


try:
    _SERVER_VERSION = version("remarkable-mcp")
except PackageNotFoundError:
    _SERVER_VERSION = ""

# One MCPServer serves modern 2026-07-28 requests and legacy initialize/session
# clients concurrently. reMarkable credentials and transport selection remain
# process configuration; MCP HTTP authorization headers are intentionally unused.
mcp = RemarkableMCP(
    "remarkable",
    version=_SERVER_VERSION,
    lifespan=lifespan,
)

# Import tools, resources, and prompts to register them
from remarkable_mcp import (  # noqa: E402
    prompts,  # noqa: F401
    resources,  # noqa: F401
    tools,  # noqa: F401
)

# Conditionally register write tools when enabled.
# Write works in three transports: cloud (default), SSH, and USB web.
# Cloud and SSH support the full set (upload/mkdir/move/rename/delete); the USB
# web interface only supports upload, so only that tool registers in USB mode
# (see the per-tool gating in write_tools.register_write_tools). Local-directory
# mode is strictly read-only and registers no write tools.
from remarkable_mcp import write_tools as _write_tools  # noqa: E402

if _write_tools.write_enabled():
    _write_tools.register_write_tools()

# Register the interactive MCP App canvas (remarkable_canvas + ui:// resource).
# There is no feature flag: app-capable clients (those advertising the MCP Apps
# UI extension at initialize) open an interactive viewer, while other clients
# ignore the UI metadata and receive the rendered page as an embedded image.
from remarkable_mcp import app_canvas as _app_canvas  # noqa: E402

_app_canvas.register_app_tools()

# The tool list in the instructions comes from the tools registered above.
refresh_instructions()


def _transport_security_for_host(host: str) -> TransportSecuritySettings:
    """Build a strict Host/Origin allowlist for the actual HTTP bind address."""
    normalized = host.strip().strip("[]")
    if normalized in ("0.0.0.0", "::"):
        raise ValueError("Wildcard HTTP bind addresses are not supported; use a concrete address.")
    if normalized in _LOOPBACK_HOSTS:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )

    try:
        address = ip_address(normalized)
    except ValueError:
        authority = normalized
    else:
        authority = f"[{normalized}]" if address.version == 6 else normalized

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[authority, f"{authority}:*"],
        allowed_origins=[
            f"http://{authority}",
            f"http://{authority}:*",
            f"https://{authority}",
            f"https://{authority}:*",
        ],
    )


def run(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
):
    """Run the MCP server over stdio or Streamable HTTP."""
    if transport == "streamable-http":
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            transport_security=_transport_security_for_host(host),
        )
        return
    mcp.run(transport=transport)

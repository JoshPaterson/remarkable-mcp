"""
MCP Tools for reMarkable tablet access.

Tools in this module never modify the reMarkable library. Most are read-only
and idempotent; ``remarkable_export`` additionally creates a bounded temporary
file on the MCP server host and returns it as a resource link.
"""

import base64
import bisect
import io
import json
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    ResourceLink,
    TextContent,
    TextResourceContents,
    ToolAnnotations,
)
from pydantic.json_schema import SkipJsonSchema

from remarkable_mcp import export_save, ocr
from remarkable_mcp.api import (
    REMARKABLE_TOKEN,
    document_version,
    download_raw_file,
    get_file_type,
    get_item_path,
    get_items_by_id,
    get_items_by_parent,
    get_rmapi,
)
from remarkable_mcp.concurrency import run_blocking
from remarkable_mcp.export_resources import PublishedExport, export_store
from remarkable_mcp.exporters import (
    ExportBuildResult,
    ExportMetadata,
    PdfMode,
    write_archive_pdf_export,
    write_markdown_export,
    write_native_pdf_export,
)
from remarkable_mcp.extract import (
    extract_text_from_document_zip,
    extract_text_from_epub,
    extract_text_from_pdf,
    get_background_color,
    get_cached_ocr_result,
    get_document_file_type,
    get_document_page_count,
    get_page_layers_from_document_zip,
    render_mapped_pdf_page_from_document_zip,
    render_merged_page_from_document_zip,
    render_page_from_document_zip,
    render_page_from_document_zip_svg,
    render_page_full_page_from_document_zip,
    render_tablet_pdf_page_to_png,
)
from remarkable_mcp.lookup import find_item, lookup, lookup_folder, scoped_items
from remarkable_mcp.lookup import is_cloud_archived as _is_cloud_archived
from remarkable_mcp.pages import tablet_page
from remarkable_mcp.pdf_marks import (
    for_export,
    original_pdf,
    pdf_outline,
    stored_marks,
)
from remarkable_mcp.pdf_ocr import pages_needing_ocr
from remarkable_mcp.responses import invalid_param, make_error, make_response, resolve_alias
from remarkable_mcp.roots import get_root as _get_root_path
from remarkable_mcp.roots import is_under_root as _is_within_root
from remarkable_mcp.roots import to_library_path as _resolve_root_path
from remarkable_mcp.roots import to_root_relative as _apply_root_filter
from remarkable_mcp.server import mcp
from remarkable_mcp.tags import matches_tags, tag_names
from remarkable_mcp.templates import group_pages

logger = logging.getLogger(__name__)


def _find_target_document(collection, items_by_id: dict, document: str):
    """Find an in-scope document by display name or root-relative path."""
    return find_item(document, collection, items_by_id, kind="document")


# Base annotations for read-only operations
_BASE_ANNOTATIONS = {
    "read_only_hint": True,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": False,  # Private cloud account, not open world
}

# Unique annotations for each tool with descriptive titles
READ_ANNOTATIONS = ToolAnnotations(
    title="Read reMarkable Document",
    **_BASE_ANNOTATIONS,
)

BROWSE_ANNOTATIONS = ToolAnnotations(
    title="Browse reMarkable Library",
    **_BASE_ANNOTATIONS,
)

SEARCH_ANNOTATIONS = ToolAnnotations(
    title="Search reMarkable Documents",
    **_BASE_ANNOTATIONS,
)

RECENT_ANNOTATIONS = ToolAnnotations(
    title="Get Recent reMarkable Documents",
    **_BASE_ANNOTATIONS,
)

STATUS_ANNOTATIONS = ToolAnnotations(
    title="Check reMarkable Connection",
    **_BASE_ANNOTATIONS,
)

IMAGE_ANNOTATIONS = ToolAnnotations(
    title="Get reMarkable Page Image",
    **_BASE_ANNOTATIONS,
)

LAYOUT_ANNOTATIONS = ToolAnnotations(
    title="Get reMarkable Page Layout",
    **_BASE_ANNOTATIONS,
)

EXPORT_ANNOTATIONS = ToolAnnotations(
    title="Export reMarkable Document",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)

# Default page size for pagination (characters) - used for PDFs/EPUBs
DEFAULT_PAGE_SIZE = 8000

# remarkable_search reads at most this many documents, and returns this much
# text of each.
MAX_SEARCH_DOCUMENTS = 20
SEARCH_CONTENT_CHARS = 2000


def _item_tags(item) -> List[str]:
    """Tag names for a listed document (empty when the transport has none)."""
    tags = getattr(item, "tags", None)
    return tag_names(tags) if isinstance(tags, (list, tuple)) else []


def _item_page_tags(item) -> List[str]:
    """Page tag names for a listed document (empty when the transport has none)."""
    tags = getattr(item, "page_tags", None)
    return tag_names(tags) if isinstance(tags, (list, tuple)) else []


def _matches_item_tags(item, wanted: List[str]) -> bool:
    """True if a document or one of its pages carries any wanted tag."""
    return matches_tags(_item_tags(item), wanted) or matches_tags(_item_page_tags(item), wanted)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("s" if count != 1 else "")


def _page_shows_layers(layers: list) -> bool:
    """Layer detail is worth showing when a page has several or hidden layers."""
    return len(layers) > 1 or any(not la.get("visible", True) for la in layers)


def _annotated_page_lines(ap: dict) -> List[str]:
    """Text lines describing one annotated page for remarkable_read."""
    layers = ap.get("layers") or []
    show_layers = _page_shows_layers(layers)
    hidden = {la["name"] for la in layers if not la.get("visible", True)}

    def on_layer(entry: dict) -> str:
        layer = entry.get("layer")
        if not show_layers or not layer:
            return ""
        return f" ({layer}{', hidden' if layer in hidden else ''})"

    marks = []
    if ap.get("has_handwriting"):
        marks.append("handwritten notes")
    n_hl = len(ap.get("highlights") or [])
    if n_hl:
        marks.append(_plural(n_hl, "highlight"))
    strokes = ap.get("highlighter_strokes") or []
    if strokes:
        colors = list(dict.fromkeys(str(h.get("color")) for h in strokes))
        marks.append(f"{_plural(len(strokes), 'highlighter stroke')} ({', '.join(colors)})")
    images = ap.get("images") or []
    if images:
        marks.append(_plural(len(images), "pasted image"))
    summary = ", ".join(marks)
    page_tags = ap.get("page_tags") or []
    if page_tags:
        tag_text = "page tags: " + ", ".join(page_tags)
        summary = f"{summary}; {tag_text}" if summary else tag_text
    lines = [f"Page {ap['page']}: " + (summary or "annotated")]

    if show_layers:
        for layer in layers:
            parts = []
            if layer.get("pen_strokes"):
                parts.append(
                    f"{_plural(layer['pen_strokes'], 'pen stroke')} "
                    f"({', '.join(layer.get('pen_colors') or [])})"
                )
            if layer.get("highlights"):
                parts.append(_plural(layer["highlights"], "highlight"))
            if layer.get("highlighter_strokes"):
                parts.append(_plural(layer["highlighter_strokes"], "highlighter stroke"))
            if layer.get("images"):
                parts.append(_plural(layer["images"], "pasted image"))
            state = "" if layer.get("visible", True) else " [hidden]"
            lines.append(f"  {layer['name']}{state}: " + (", ".join(parts) or "empty"))

    details = ap.get("highlight_details")
    if details is None:
        details = [{"text": h} for h in ap.get("highlights") or []]
    for h in details:
        color = h.get("color")
        prefix = f"[{color}] " if color else ""
        location = f"   region {h['region']}" if h.get("region") else ""
        lines.append(f"  • {prefix}{h['text']}{on_layer(h)}{location}")
    for h in strokes:
        location = f", region {h['region']}" if h.get("region") else ""
        lines.append(f"  ~ [{h.get('color')}] highlighter stroke{location}{on_layer(h)}")
    for image in images:
        location = f", region {image['region']}" if image.get("region") else ""
        lines.append(f"  + pasted image{location}{on_layer(image)}")
    return lines


def _descendant_items(items_by_parent: dict, parent_id: str) -> List:
    """All items below ``parent_id`` in the folder tree, depth-first."""
    found = []
    stack = [parent_id]
    seen = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for item in items_by_parent.get(current, []):
            found.append(item)
            if item.is_folder:
                stack.append(item.ID)
    return found


def _modified_sort_key(item) -> float:
    """Return a sortable timestamp for an item's modified date.

    Normalizes ``ModifiedClient`` to a float epoch timestamp so documents with a
    missing/null modified date (e.g. freshly-created notebooks) sort to the
    bottom instead of crashing the comparator.

    A plain sentinel ``datetime`` cannot be used here: USB ``ModifiedClient``
    values are timezone-aware (parsed via ``fromisoformat``) while cloud and SSH
    values are naive (parsed via ``fromtimestamp``), and Python refuses to
    compare naive and aware datetimes. Reducing everything to ``.timestamp()``
    sidesteps both the str/datetime and the naive/aware comparison errors.
    """
    modified = getattr(item, "ModifiedClient", None)
    if isinstance(modified, datetime):
        try:
            return modified.timestamp()
        except (OverflowError, OSError, ValueError):
            return 0.0
    return 0.0


def _is_pdf_payload(data: Optional[bytes]) -> bool:
    """Return whether transport bytes are a native PDF rather than an rmdoc zip."""
    return bool(data and data.lstrip().startswith(b"%PDF"))


def _count_pdf_pages(pdf_bytes: bytes) -> int:
    """Count pages in a native PDF returned instead of an rmdoc archive."""
    import fitz

    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        return len(document)


# Page headings in extracted text: "--- Page N ---" (PDF text and OCR) and
# "Page N: ..." (the annotated pages list). Other section headings end a page.
_PAGE_MARK = re.compile(r"^--- Page (\d+) ---$|^Page (\d+): ", re.MULTILINE)
_SECTION_MARK = re.compile(r"^(?:=== .+ ===|--- (?!Page \d+ ---).+ ---)$", re.MULTILINE)
GREP_CONTEXT = 100


def _page_starts(text: str, source_chars: int = 0, page_map: Optional[list] = None) -> list:
    """``[(offset, tablet_page or None)]`` where each page's text starts in
    ``text``, in order. In the first ``source_chars`` (a PDF's own text) the
    headings number PDF pages, mapped to tablet pages with ``page_map``."""
    starts = [(m.start(), None) for m in _SECTION_MARK.finditer(text)]
    for m in _PAGE_MARK.finditer(text):
        number = int(m.group(1) or m.group(2))
        if m.group(1) and m.start() < source_chars and page_map:
            number = tablet_page(number - 1, page_map)
        starts.append((m.start(), number))
    return sorted(starts, key=lambda start: start[0])


def _grep_text(text: str, pattern, starts: list) -> List[dict]:
    """Each match of ``pattern`` in ``text``: ``{"page", "text"}``, the text
    being the match with up to GREP_CONTEXT characters around it."""
    offsets = [offset for offset, _ in starts]
    matches = []
    for match in pattern.finditer(text):
        if match.start() == match.end():
            continue
        index = bisect.bisect_right(offsets, match.start()) - 1
        start = max(0, match.start() - GREP_CONTEXT)
        end = min(len(text), match.end() + GREP_CONTEXT)
        context = (
            ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
        )
        matches.append({"page": starts[index][1] if index >= 0 else None, "text": context})
    return matches


def _match_line(match: dict) -> str:
    return f"[page {match['page']}] {match['text']}" if match["page"] else match["text"]


def _grep_hint(grep: str, matches: List[dict]) -> str:
    if not matches:
        return f"No matches for '{grep}' in the document."
    pages = list(dict.fromkeys(m["page"] for m in matches if m["page"]))
    where = f" on page(s) {pages}" if pages else ""
    return f"Found {len(matches)} match(es) for '{grep}'{where}."


def _group_by_size(items: list, size: int, length) -> List[list]:
    """``items`` in consecutive groups of at most ``size`` total ``length``
    (a longer item gets a group of its own); at least one group."""
    groups: List[list] = [[]]
    used = 0
    for item in items:
        n = length(item)
        if groups[-1] and used + n > size:
            groups.append([])
            used = 0
        groups[-1].append(item)
        used += n
    return groups


def _text_chunks(text: str, size: int) -> List[str]:
    """``text`` in parts of ``size`` characters; one empty part for no text."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


@mcp.tool(annotations=READ_ANNOTATIONS)
async def remarkable_read(
    document: str,
    content_type: Literal["text", "raw", "annotations"] = "text",
    part: int = 1,
    grep: Optional[str] = None,
    include_ocr: bool = False,
    page: SkipJsonSchema[Optional[int]] = None,
) -> str:
    """
    <usecase>Read and extract text content from a reMarkable document.</usecase>
    <instructions>
    Extracts a document's text in parts, so a long document does not fill the
    context window.

    Content types:
    - "text" (default): Full extracted text (PDF/EPUB content + annotations)
    - "raw": Original PDF/EPUB text only (no annotations). Works in every
      transport, as long as the source file is present (very large PDFs/EPUBs
      may not be synced to the cloud).
    - "annotations": Only the annotations listed below

    Annotations ("text" adds them after the document text):
    - Notebook typed text (keyboard or type tool) comes first.
    - "--- Annotated pages ---": one line per annotated page, e.g. "Page 3:
      handwritten notes, 2 highlights, 1 highlighter stroke (yellow), 1 pasted
      image; page tags: Review", then one line per item:
      - highlight: "[color] text   region [...]". JSON "highlights": page,
        text, color, color_id, color_hex, layer, rects_mm (one box per line
        or piece), bbox_mm and region. A highlight on notebook typed text
        carries that text too.
      - highlighter stroke that did not snap to text (e.g. drawn over a
        display equation): "~ [color] highlighter stroke, region [...]". JSON
        "highlighter_strokes": page, color, color_id, color_hex, layer,
        bbox_mm and region. It has no text.
      - pasted image (a capture): "+ pasted image, region [...]". JSON
        "images": page, file, layer, bbox_mm and region. Ink and highlights
        copied inside a capture are part of the image, not listed.
      - a page with several layers or a hidden one also gets a line per layer
        (name, [hidden], counts), and each item names its layer. JSON
        "layers": [{page, layers: [{name, visible, pen_strokes, pen_colors,
        highlights, highlighter_strokes, images}]}].
    - "--- Page templates ---": non-blank notebook templates, e.g. "Page 2:
      P Dots S" or "Pages 3-5: P Lines medium". JSON "page_templates":
      [{page, template}].
    - "--- Handwritten (OCR) ---": with include_ocr.
    - "--- Marks (added with remarkable_mark_pdf) ---": PDFs only; JSON
      "marks". These are the agent's marks, not the user's ink. Erasable
      marks and covers are tablet ink, so the page lines count them too.
    bbox_mm is millimetres and region page fractions, both [left, top, right,
    bottom] from the page's top-left; region can be passed to remarkable_image.

    The list summarizes; handwriting is reported but not transcribed without
    OCR. When reviewing the user's work, render each page with remarkable_image
    as well.

    Highlight text can be wrong (it often misses mathematical symbols and
    equations, or is short, garbled or has gaps); the region is reliable. When
    the exact highlighted content matters, render the highlight's region with
    remarkable_image(document, page, region=..., dpi=300).

    Long text comes in parts (about 8000 characters each; one OCR'd page
    each for handwritten notebooks):
    - Start with part=1 (default)
    - "more" is true when there is more; "next_part" is the part to read next
    - "total_pages" is the document's tablet page count; "parts" is the
      number of text parts (also given as "content_pages")
    - "total_pages_known" reports whether the physical count was available.
      Raw PDF reads count the downloaded PDF directly. If a raw EPUB archive
      cannot be read, content still returns with total_pages=null.
    - If older USB firmware returns only a native PDF, "text" returns source
      text without annotations and "annotations" reports that the archive is
      unavailable instead of trying to parse the PDF as a zip.

    grep searches the whole document (a regex, case-insensitive) and returns
    each match with about 100 characters of context and the tablet page it is
    on ("matches": [{"page", "text"}]), in parts like the text. page is null
    for text with no page, such as EPUB text.

    OCR uses Google Vision when configured, otherwise Tesseract.
    </instructions>
    <parameters>
    - document: Document name or path (use remarkable_browse to find documents)
    - content_type: "text" (full), "raw" (PDF/EPUB only), "annotations" (notes only)
    - part: Text part to read (default: 1); not a tablet page
    - grep: Optional regex to find in the whole document
    - include_ocr: Enable handwriting OCR for annotations (default: False)
    </parameters>
    <examples>
    - remarkable_read("Meeting Notes")  # Get first page of text
    - remarkable_read("Book.pdf", content_type="raw")  # Get raw PDF text
    - remarkable_read("Notes", content_type="annotations")  # Only annotations
    - remarkable_read("Report", part=2)  # Get the second part
    - remarkable_read("Manual", grep="installation")  # Find a keyword
    </examples>
    """
    part, error = resolve_alias(part, page, "part", "page", default=1)
    if error:
        return error
    out = await _read_document(document, content_type, part, grep, include_ocr)
    if isinstance(out, str):
        return out
    return make_response(*out)


async def _read_document(
    document: str, content_type: str, page: int, grep: Optional[str], include_ocr: bool
):
    """remarkable_read: ``(data, hint)``, or an error reply. ``page`` is the
    text part to read."""
    pattern = None
    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            return invalid_param(
                f"grep: invalid regex pattern: {e}",
                "Use a valid regex pattern or simple text string.",
            )
    try:
        client = await run_blocking(get_rmapi)
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)

        # Validate parameters
        page = max(1, page)
        # Internal page size for PDF/EPUB character-based pagination
        page_size = DEFAULT_PAGE_SIZE

        target_doc, error = lookup(
            client, document, kind="document", collection=collection, items_by_id=items_by_id
        )
        if error:
            return error

        doc_path = get_item_path(target_doc, items_by_id)
        file_type = await run_blocking(get_file_type, client, target_doc)
        archive_bytes: Optional[bytes] = None

        async def load_archive() -> bytes:
            nonlocal archive_bytes
            if archive_bytes is None:
                archive_bytes = await run_blocking(client.download, target_doc)
            return archive_bytes

        async def count_archive_pages() -> int:
            raw_doc = await load_archive()
            if _is_pdf_payload(raw_doc):
                return await run_blocking(_count_pdf_pages, raw_doc)
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(raw_doc)
                tmp_path = Path(tmp.name)
            try:
                return await run_blocking(get_document_page_count, tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)

        # Collect content based on content_type
        text_parts = []
        source_chars = 0  # leading characters of text_parts that are the PDF's own text
        raw_available = False
        raw_data: Optional[bytes] = None
        page_count_note = None
        mark_list: list = []
        mark_summaries: list = []
        outline_entries: list = []
        scanned_pages: list = []

        # Get raw PDF/EPUB content if requested or for "text" mode
        if content_type in ("text", "raw") and file_type in ("pdf", "epub"):
            raw_data = await run_blocking(download_raw_file, client, target_doc, file_type)
            if raw_data and file_type == "pdf":
                mark_list = stored_marks(raw_data)
                try:
                    outline_entries = pdf_outline(raw_data)
                except Exception:
                    outline_entries = []
                raw_data = original_pdf(raw_data)
                try:
                    scanned_pages = pages_needing_ocr(raw_data)["scanned"]
                except Exception:
                    scanned_pages = []
            if raw_data:
                raw_available = True
                with tempfile.NamedTemporaryFile(suffix=f".{file_type}", delete=False) as tmp:
                    tmp.write(raw_data)
                    tmp_path = Path(tmp.name)
                try:
                    if file_type == "pdf":
                        raw_text = await run_blocking(extract_text_from_pdf, tmp_path)
                    else:
                        raw_text = await run_blocking(extract_text_from_epub, tmp_path)
                    if raw_text:
                        text_parts.append(raw_text)
                        source_chars = len(raw_text)
                finally:
                    tmp_path.unlink(missing_ok=True)
            elif content_type == "raw":
                # The document has no source PDF/EPUB blob (e.g. a pure notebook).
                return make_error(
                    error_type="raw_not_available",
                    message=f"No raw {file_type.upper()} source file found for this document",
                    suggestion=(
                        "Use content_type='text' for extracted content, "
                        "or content_type='annotations' for handwritten notes."
                    ),
                )

        if content_type == "annotations" and file_type == "pdf":
            marked_pdf = await run_blocking(download_raw_file, client, target_doc, "pdf")
            if marked_pdf:
                mark_list = stored_marks(marked_pdf)

        # Get annotations/typed text (for "text" or "annotations" mode)
        notebook_pages = []  # List of page content for notebook pagination
        ocr_backend_used = None  # Track which OCR backend was used
        content = None  # Will hold extraction result

        async def read_native_pdf_content(pdf_bytes: bytes):
            nonlocal file_type, page_count_note, raw_available
            if content_type == "annotations":
                return None, make_error(
                    error_type="annotations_not_available",
                    message=(
                        "Annotations are unavailable because this transport "
                        "returned a native PDF instead of a document archive."
                    ),
                    suggestion=(
                        "Use content_type='raw' or 'text', or connect through "
                        "cloud/SSH or newer USB firmware that supports rmdoc."
                    ),
                )

            file_type = "pdf"
            if not raw_available:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(pdf_bytes)
                    pdf_path = Path(tmp.name)
                try:
                    native_text = await run_blocking(extract_text_from_pdf, pdf_path)
                    if native_text:
                        text_parts.append(native_text)
                    raw_available = True
                finally:
                    pdf_path.unlink(missing_ok=True)

            try:
                native_pdf_pages = await run_blocking(_count_pdf_pages, pdf_bytes)
            except Exception as e:
                logger.warning(
                    "Could not count native PDF pages for %s: %s",
                    target_doc.ID,
                    e,
                )
                native_pdf_pages = 0
                page_count_note = (
                    "Physical page count unavailable; source PDF text was "
                    "returned without annotations."
                )

            return {
                "typed_text": [],
                "highlights": [],
                "handwritten_text": None,
                "pages": native_pdf_pages,
                "page_ids": [],
                "annotated_pages": [],
                "ocr_backend": None,
                "tags": [],
            }, None

        if content_type in ("text", "annotations"):
            # For notebooks (no PDF/EPUB), use page-based pagination
            is_notebook = file_type not in ("pdf", "epub")

            if is_notebook and include_ocr:
                cached = await run_blocking(
                    get_cached_ocr_result,
                    target_doc.ID,
                    include_ocr=True,
                    ocr_backend=None,
                    version=document_version(target_doc),
                )
                if cached and cached.get("handwritten_text"):
                    notebook_pages = cached["handwritten_text"]
                    ocr_backend_used = cached.get("ocr_backend")
                    content = cached

            if not notebook_pages and is_notebook:
                raw_doc = await load_archive()
                if _is_pdf_payload(raw_doc):
                    content, native_error = await read_native_pdf_content(raw_doc)
                    if native_error:
                        return native_error
                    is_notebook = False
                else:
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp.write(raw_doc)
                        tmp_path = Path(tmp.name)

                    try:
                        content = await run_blocking(
                            extract_text_from_document_zip,
                            tmp_path,
                            include_ocr=include_ocr,
                            doc_id=target_doc.ID,
                            version=document_version(target_doc),
                        )
                        if content.get("handwritten_text"):
                            notebook_pages = content["handwritten_text"]
                            ocr_backend_used = content.get("ocr_backend")
                    finally:
                        tmp_path.unlink(missing_ok=True)

            # For non-notebooks or when no OCR pages, build annotation sections
            if not (is_notebook and notebook_pages):
                if content is None:
                    # Need to extract if we haven't already
                    raw_doc = await load_archive()
                    if _is_pdf_payload(raw_doc):
                        content, native_error = await read_native_pdf_content(raw_doc)
                        if native_error:
                            return native_error
                    else:
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                            tmp.write(raw_doc)
                            tmp_path = Path(tmp.name)
                        try:
                            content = await run_blocking(
                                extract_text_from_document_zip,
                                tmp_path,
                                include_ocr=include_ocr,
                                doc_id=target_doc.ID,
                                version=document_version(target_doc),
                            )
                        finally:
                            tmp_path.unlink(missing_ok=True)

                # Add annotations section
                annotation_parts = []
                if content.get("typed_text"):
                    annotation_parts.extend(content["typed_text"])
                # Per-page index of highlights / handwritten notes: surfaces only
                # the annotated pages (and their highlighted text) so the reader
                # need not page through the whole document to find them.
                annotated_pages = content.get("annotated_pages") or []
                shown_highlights = set()
                if annotated_pages:
                    annotation_parts.append("\n--- Annotated pages ---")
                    for ap in annotated_pages:
                        annotation_parts.extend(_annotated_page_lines(ap))
                        shown_highlights.update(ap.get("highlights") or [])
                # Highlights not covered by the per-page index above — e.g. from
                # the legacy .highlights JSON (older firmware), which has no page
                # attribution. Without this, a doc whose pages carry pen strokes
                # would suppress its legacy highlight text entirely.
                other_highlights = [
                    h for h in content.get("highlights") or [] if h not in shown_highlights
                ]
                if other_highlights:
                    annotation_parts.append("\n--- Highlights ---")
                    annotation_parts.extend(other_highlights)
                page_templates = content.get("page_templates") or []
                if page_templates:
                    annotation_parts.append("\n--- Page templates ---")
                    for first, last, name in group_pages(
                        [(t["page"], t["template"]) for t in page_templates]
                    ):
                        pages = f"Page {first}" if first == last else f"Pages {first}-{last}"
                        annotation_parts.append(f"{pages}: {name}")
                if content.get("handwritten_text"):
                    annotation_parts.append("\n--- Handwritten (OCR) ---")
                    annotation_parts.extend(content["handwritten_text"])
                if mark_list:
                    mark_summaries = _mark_summaries(mark_list, await load_archive())
                    annotation_parts.append("\n--- Marks (added with remarkable_mark_pdf) ---")
                    annotation_parts.extend(_mark_line(m) for m in mark_summaries)

                if annotation_parts:
                    if text_parts and content_type == "text":
                        text_parts.append("\n\n=== Annotations ===\n")
                    text_parts.extend(annotation_parts)

        physical_pages = int(content.get("pages") or 0) if content else 0
        if physical_pages <= 0 and page_count_note is None:
            if content_type == "raw" and file_type == "pdf":
                try:
                    physical_pages = await run_blocking(_count_pdf_pages, raw_data)
                except Exception as e:
                    logger.warning("Could not count raw PDF pages for %s: %s", target_doc.ID, e)
                    page_count_note = (
                        "Physical page count unavailable; raw PDF content was returned."
                    )
            else:
                try:
                    physical_pages = await count_archive_pages()
                except Exception as e:
                    if content_type != "raw" or file_type != "epub":
                        raise
                    logger.warning("Could not count raw EPUB pages for %s: %s", target_doc.ID, e)
                    page_count_note = (
                        "Physical page count unavailable because the document archive "
                        "could not be read; raw EPUB content was returned."
                    )
        raw_page_count_unknown = (
            physical_pages <= 0 and content_type == "raw" and file_type in ("pdf", "epub")
        )
        if raw_page_count_unknown and page_count_note is None:
            page_count_note = (
                f"Physical page count unavailable; raw {file_type.upper()} content was returned."
            )
        page_count_unknown = physical_pages <= 0 and page_count_note is not None
        total_pages = None if page_count_unknown else max(1, physical_pages)
        total_pages_known = total_pages is not None
        physical_page_summary = (
            f"document has {total_pages} physical page(s)"
            if total_pages_known
            else "physical document page count is unavailable"
        )

        # OCR results remain content-pagination units. They can be fewer than
        # physical pages when blank or non-OCR pages are omitted. grep searches
        # them as text, like any other document.
        if notebook_pages and grep:
            text_parts = list(notebook_pages)
        elif notebook_pages:
            content_pages = len(notebook_pages)

            if page > content_pages:
                return make_error(
                    error_type="page_out_of_range",
                    message=(
                        f"Content page {page} does not exist. Extracted content has "
                        f"{content_pages} page(s); the document has {total_pages} "
                        "physical page(s)."
                    ),
                    suggestion=f"Use page=1 to {content_pages} to read extracted content.",
                )

            page_content = notebook_pages[page - 1]
            has_more = page < content_pages

            result = {
                "document": target_doc.VissibleName,
                "path": _apply_root_filter(doc_path),
                "file_type": "notebook",
                "content_type": content_type,
                "content": page_content,
                "part": page,
                "page": page,
                "total_pages": total_pages,
                "total_pages_known": total_pages_known,
                "parts": content_pages,
                "content_pages": content_pages,
                "page_type": "notebook",
                "total_chars": len(page_content),
                "more": has_more,
                "modified": (
                    target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
                ),
            }

            if ocr_backend_used:
                result["ocr_backend"] = ocr_backend_used
            handwriting = (content or {}).get("handwriting") or []
            if 1 <= page <= len(handwriting):
                result["handwriting"] = [handwriting[page - 1]]

            if has_more:
                result["next_part"] = result["next_page"] = page + 1

            hint_parts = [f"Notebook text part {page}/{content_pages}; {physical_page_summary}."]
            ocr_note = _ocr_reliability_note(
                result.get("handwriting"), ocr_backend_used, target_doc.VissibleName
            )
            if ocr_note:
                result["ocr_note"] = ocr_note
                hint_parts.append(ocr_note)
            if has_more:
                doc_name = target_doc.VissibleName
                hint_parts.append(f"Next: remarkable_read('{doc_name}', part={page + 1}).")
            else:
                hint_parts.append("(last part)")

            return result, " ".join(hint_parts)

        # Combine all content
        full_text = "\n\n".join(text_parts) if text_parts else ""

        # A notebook with no typed text: read it again with OCR.
        if not full_text and not include_ocr and file_type not in ("pdf", "epub"):
            out = await _read_document(document, content_type, page, grep, include_ocr=True)
            if isinstance(out, str):
                return out
            data, hint = out
            data["_ocr_auto_enabled"] = True
            return data, "OCR auto-enabled (notebook had no typed text). " + hint

        matches: list = []
        if pattern is not None:
            page_map = []
            if file_type == "pdf" and source_chars:
                try:
                    page_map = _archive_page_map(await load_archive())
                except Exception:
                    page_map = []
            starts = _page_starts(full_text, source_chars, page_map)
            matches = _grep_text(full_text, pattern, starts)
            match_parts = _group_by_size(matches, page_size, lambda m: len(m["text"]))
            units = ["\n\n---\n\n".join(_match_line(m) for m in group) for group in match_parts]
        else:
            units = _text_chunks(full_text, page_size)
        total_chars = len(full_text)

        if total_chars == 0 and pattern is None:
            if page > 1:
                return make_error(
                    error_type="page_out_of_range",
                    message=(
                        f"Content page {page} does not exist. Extracted content has "
                        f"1 page; {physical_page_summary}."
                    ),
                    suggestion="Use part=1 to start from the beginning.",
                )
            # Return empty result for page 1
            result = {
                "document": target_doc.VissibleName,
                "path": _apply_root_filter(doc_path),
                "file_type": file_type or "notebook",
                "content_type": content_type,
                "content": "",
                "part": 1,
                "page": 1,
                "total_pages": total_pages,
                "total_pages_known": total_pages_known,
                "parts": 1,
                "content_pages": 1,
                "total_chars": 0,
                "more": False,
                "modified": (
                    target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
                ),
            }
            hint = (
                f"Document '{target_doc.VissibleName}' has no extractable text content. "
                "This may be a handwritten notebook - try include_ocr=True for OCR extraction."
            )
            if page_count_note:
                result["page_count_note"] = page_count_note
                hint = f"{hint} {page_count_note}"
            return result, hint

        content_pages = len(units)
        if page > content_pages:
            return make_error(
                error_type="page_out_of_range",
                message=(
                    f"Part {page} does not exist. The text has {content_pages} "
                    f"part(s); {physical_page_summary}."
                ),
                suggestion="Use part=1 to start from the beginning.",
            )

        page_content = units[page - 1]
        has_more = page < content_pages

        result = {
            "document": target_doc.VissibleName,
            "path": _apply_root_filter(doc_path),
            "file_type": file_type or "notebook",
            "content_type": content_type,
            "content": page_content,
            "part": page,
            "page": page,
            "total_pages": total_pages,
            "total_pages_known": total_pages_known,
            "parts": content_pages,
            "content_pages": content_pages,
            "total_chars": total_chars,
            "more": has_more,
            "modified": (
                target_doc.ModifiedClient if hasattr(target_doc, "ModifiedClient") else None
            ),
        }

        # Add tags if present (from extraction, or from listing metadata)
        tags = (content.get("tags") if content else None) or _item_tags(target_doc)
        if tags:
            result["tags"] = tags
        if content and content.get("page_templates"):
            result["page_templates"] = content["page_templates"]

        # Highlights with their colors, in page order
        if content_type in ("text", "annotations") and content:
            highlight_details = content.get("highlight_details")
            if highlight_details:
                result["highlights"] = highlight_details
            highlighter_strokes = content.get("highlighter_strokes")
            if highlighter_strokes:
                result["highlighter_strokes"] = highlighter_strokes
            pasted = content.get("images")
            if pasted:
                result["images"] = pasted
        if content and content.get("page_tags"):
            result["page_tags"] = content["page_tags"]
        if mark_summaries:
            result["marks"] = mark_summaries
        if outline_entries:
            result["outline"] = _outline_summaries(outline_entries, await load_archive())
        if scanned_pages:
            page_map = _archive_page_map(await load_archive())
            result["pages_without_text"] = [
                p
                for p in (tablet_page(i, page_map) if page_map else i + 1 for i in scanned_pages)
                if p
            ]
        if content_type in ("text", "annotations") and content:
            page_layers = [
                {"page": ap["page"], "layers": ap["layers"]}
                for ap in content.get("annotated_pages") or []
                if _page_shows_layers(ap.get("layers") or [])
            ]
            if page_layers:
                result["layers"] = page_layers
        if content and content.get("handwriting"):
            result["handwriting"] = content["handwriting"]

        if has_more:
            result["next_part"] = result["next_page"] = page + 1

        hint_parts = []
        if pattern is not None:
            result["grep"] = grep
            result["grep_matches"] = len(matches)
            result["matches"] = match_parts[page - 1]
            hint_parts.append(_grep_hint(grep, matches))

        if has_more:
            hint_parts.append(
                f"Part {page}/{content_pages}; {physical_page_summary}. "
                f"Next: remarkable_read('{document}', part={page + 1})"
            )
        else:
            hint_parts.append(f"Part {page}/{content_pages} (complete); {physical_page_summary}.")
        if page_count_note:
            result["page_count_note"] = page_count_note
            hint_parts.append(page_count_note)
        if result.get("pages_without_text"):
            count = len(result["pages_without_text"])
            hint_parts.append(
                f"{count} scanned page{'s have' if count > 1 else ' has'} no text "
                f"(pages_without_text); remarkable_pdf_ocr('{document}') adds it."
            )

        ocr_note = _ocr_reliability_note(
            result.get("handwriting"),
            ocr_backend_used or (content or {}).get("ocr_backend"),
            document,
        )
        if ocr_note:
            result["ocr_note"] = ocr_note
            hint_parts.append(ocr_note)

        if content_type == "text" and not raw_available and file_type in ("pdf", "epub"):
            hint_parts.append(
                "No source PDF/EPUB blob was found for this document, so only "
                "annotations were extracted. Very large files may not be synced "
                "to the cloud; try SSH/USB mode with the device connected."
            )

        return result, " ".join(hint_parts)

    except Exception as e:
        return make_error(
            error_type="read_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@mcp.tool(annotations=BROWSE_ANNOTATIONS)
async def remarkable_browse(
    path: str = "/", query: Optional[str] = None, tags: Optional[List[str]] = None
) -> str:
    """
    <usecase>Browse your reMarkable library or search for documents.</usecase>
    <instructions>
    Three modes:
    1. Browse mode (default): List contents of a folder
       - Use path="/" for root folder
       - Use path="/FolderName" to navigate into folders
    2. Search mode: Find documents by name
       - Set query="search term" to search across all documents
    3. Filter by tags: Find documents with specific tags
       - Set tags=["tag1", "tag2"] to list documents carrying any of the tags,
         as document tags or page tags
       - In browse mode, searches the folder and all of its subfolders
       - Works in both browse and search modes

    Results include document names, types, modification dates, and tags.
    "modified" is the last edit time recorded by the tablet or app that made
    the change, not the time of the last sync.

    Note: If REMARKABLE_ROOT_PATH is configured, only documents within that
    folder are accessible. Paths are relative to the root path.
    </instructions>
    <parameters>
    - path: Folder path to browse (default: "/" for root)
    - query: Search term to find documents by name (optional, triggers search mode)
    - tags: List of tags to filter documents (optional, case-insensitive)
    </parameters>
    <examples>
    - remarkable_browse()  # List root folder
    - remarkable_browse("/Work")  # List Work folder
    - remarkable_browse(query="meeting")  # Search for "meeting"
    - remarkable_browse(tags=["important"])  # Show documents with "important" tag
    - remarkable_browse(query="project", tags=["work"])  # Search with tag filter
    </examples>
    """
    try:
        client = await run_blocking(get_rmapi)
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)
        items_by_parent = get_items_by_parent(collection)

        root = _get_root_path()
        # Resolve user path to actual device path
        actual_path = _resolve_root_path(path)

        # Search mode
        if query:
            query_lower = query.lower()
            matches = []

            for item in collection:
                # Skip cloud-archived items
                if _is_cloud_archived(item, items_by_id):
                    continue
                item_path = get_item_path(item, items_by_id)
                # Filter by root path
                if not _is_within_root(item_path, root):
                    continue
                # Filter by tags if provided
                if tags and not _matches_item_tags(item, tags):
                    continue
                if query_lower in item.VissibleName.lower():
                    match_info = {
                        "name": item.VissibleName,
                        "path": _apply_root_filter(item_path),
                        "type": "folder" if item.is_folder else "document",
                        "modified": (
                            item.ModifiedClient if hasattr(item, "ModifiedClient") else None
                        ),
                    }
                    # Add tags if present
                    item_tags = _item_tags(item)
                    if item_tags:
                        match_info["tags"] = item_tags
                    item_page_tags = _item_page_tags(item)
                    if item_page_tags:
                        match_info["page_tags"] = item_page_tags
                    matches.append(match_info)

            matches.sort(key=lambda x: x["name"])

            result = {"mode": "search", "query": query, "count": len(matches), "results": matches}
            if tags:
                result["filter_tags"] = tags

            if matches:
                first_doc = next((m for m in matches if m["type"] == "document"), None)
                if first_doc:
                    hint = (
                        f"Found {len(matches)} results. "
                        f"To read a document: remarkable_read('{first_doc['name']}')."
                    )
                else:
                    hint = (
                        f"Found {len(matches)} folders. "
                        f"To browse one: remarkable_browse('{matches[0]['path']}')."
                    )
            else:
                filter_desc = f" with tags {tags}" if tags else ""
                hint = (
                    f"No results for '{query}'{filter_desc}. "
                    "Try remarkable_browse('/') to see all files, "
                    "or use a different search term."
                )

            return make_response(result, hint)

        # Browse mode - use actual_path (with root applied)
        if actual_path == "/" or actual_path == "":
            target_parent = ""
        else:
            # Navigate to the folder (case-insensitive)
            path_parts = [p for p in actual_path.strip("/").split("/") if p]
            current_parent = ""

            for i, part in enumerate(path_parts):
                part_lower = part.lower()
                found = False
                found_document = None

                for item in items_by_parent.get(current_parent, []):
                    if item.VissibleName.lower() == part_lower:
                        if item.is_folder:
                            current_parent = item.ID
                            found = True
                            break
                        else:
                            # Found a document with this name
                            found_document = item

                if not found:
                    # Check if it's a document (only valid as the last path part)
                    if found_document and i == len(path_parts) - 1:
                        # Auto-redirect: return first page of the document
                        doc_path = get_item_path(found_document, items_by_id)
                        # Check if within root before redirecting
                        if not _is_within_root(doc_path, root):
                            return make_error(
                                error_type="access_denied",
                                message=(
                                    f"Document '{found_document.VissibleName}' "
                                    "is outside the configured root path."
                                ),
                                suggestion="Check REMARKABLE_ROOT_PATH configuration.",
                            )
                        out = await _read_document(
                            _apply_root_filter(doc_path), "text", 1, None, False
                        )
                        if isinstance(out, str):
                            return out
                        data, hint = out
                        data["_redirected_from"] = f"browse:{path}"
                        return make_response(data, f"Auto-redirected from browse to read. {hint}")

                    # Folder not found - suggest alternatives
                    available_folders = [
                        item.VissibleName
                        for item in items_by_parent.get(current_parent, [])
                        if item.is_folder
                    ]
                    available_docs = [
                        item.VissibleName
                        for item in items_by_parent.get(current_parent, [])
                        if not item.is_folder
                    ]
                    suggestion = "Use remarkable_browse('/') to see root folder contents."
                    if available_docs:
                        # Check if user might be looking for a document
                        for doc_name in available_docs:
                            if doc_name.lower() == part_lower:
                                suggestion = (
                                    f"'{doc_name}' is a document. "
                                    f"Use remarkable_read('{doc_name}') to read it."
                                )
                                break
                    return make_error(
                        error_type="folder_not_found",
                        message=f"Folder not found: '{part}'",
                        suggestion=suggestion,
                        did_you_mean=(available_folders[:5] if available_folders else None),
                    )

            target_parent = current_parent

        folders = []
        documents = []

        if tags:
            # Tag filter: list matching documents in this folder and all of
            # its subfolders, each with its path.
            for item in _descendant_items(items_by_parent, target_parent):
                if item.is_folder or _is_cloud_archived(item, items_by_id):
                    continue
                if not _matches_item_tags(item, tags):
                    continue
                doc_info = {
                    "name": item.VissibleName,
                    "id": item.ID,
                    "path": _apply_root_filter(get_item_path(item, items_by_id)),
                    "modified": (item.ModifiedClient if hasattr(item, "ModifiedClient") else None),
                    "tags": _item_tags(item),
                }
                item_page_tags = _item_page_tags(item)
                if item_page_tags:
                    doc_info["page_tags"] = item_page_tags
                documents.append(doc_info)
            documents.sort(key=lambda d: d["path"].lower())
        else:
            items = items_by_parent.get(target_parent, [])
            for item in sorted(items, key=lambda x: x.VissibleName.lower()):
                # Skip cloud-archived items
                if _is_cloud_archived(item, items_by_id):
                    continue
                if item.is_folder:
                    folders.append({"name": item.VissibleName, "id": item.ID})
                else:
                    doc_info = {
                        "name": item.VissibleName,
                        "id": item.ID,
                        "modified": (
                            item.ModifiedClient if hasattr(item, "ModifiedClient") else None
                        ),
                    }
                    # Add tags if present
                    item_tags = _item_tags(item)
                    if item_tags:
                        doc_info["tags"] = item_tags
                    item_page_tags = _item_page_tags(item)
                    if item_page_tags:
                        doc_info["page_tags"] = item_page_tags
                    documents.append(doc_info)

        result = {"mode": "browse", "path": path, "folders": folders, "documents": documents}
        if tags:
            result["filter_tags"] = tags
            result["recursive"] = True

        # Build helpful hint
        if tags:
            hint_parts = [
                f"Found {len(documents)} document(s) with tags {tags} "
                "in this folder and its subfolders."
            ]
        else:
            hint_parts = [f"Found {len(folders)} folder(s) and {len(documents)} document(s)."]

        if documents:
            hint_parts.append(f"To read a document: remarkable_read('{documents[0]['name']}').")
        if folders:
            folder_path = f"{path.rstrip('/')}/{folders[0]['name']}"
            hint_parts.append(f"To enter a folder: remarkable_browse('{folder_path}').")
        if not folders and not documents and not tags:
            hint_parts.append("This folder is empty.")

        return make_response(result, " ".join(hint_parts))

    except Exception as e:
        return make_error(
            error_type="browse_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@mcp.tool(annotations=RECENT_ANNOTATIONS)
async def remarkable_recent(limit: int = 10, include_preview: bool = False) -> str:
    """
    <usecase>Get your most recently modified documents.</usecase>
    <instructions>
    Returns documents sorted by modification date (newest first).
    Optionally includes a text preview of each document's content.

    Use this to quickly find what you were working on recently.

    Note: If REMARKABLE_ROOT_PATH is configured, only documents within that
    folder are included.
    </instructions>
    <parameters>
    - limit: Maximum documents to return (default: 10, max: 50 without preview, 10 with preview)
    - include_preview: Include first ~200 chars of text content (default: False)
    </parameters>
    <examples>
    - remarkable_recent()  # Last 10 documents
    - remarkable_recent(limit=5, include_preview=True)  # With content preview
    </examples>
    """
    try:
        client = await run_blocking(get_rmapi)
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)

        # Clamp limit - lower max when previews enabled (expensive operation)
        max_limit = 10 if include_preview else 50
        limit = min(max(1, limit), max_limit)

        root = _get_root_path()

        # Get documents sorted by modified date (excluding archived, filtered by root)
        documents = []
        for item in collection:
            if item.is_folder or _is_cloud_archived(item, items_by_id):
                continue
            item_path = get_item_path(item, items_by_id)
            if not _is_within_root(item_path, root):
                continue
            documents.append(item)

        documents.sort(key=_modified_sort_key, reverse=True)

        results = []
        for doc in documents[:limit]:
            doc_path = get_item_path(doc, items_by_id)
            doc_info = {
                "name": doc.VissibleName,
                "path": _apply_root_filter(doc_path),
                "modified": (doc.ModifiedClient if hasattr(doc, "ModifiedClient") else None),
            }
            # Add tags if present
            doc_tags = _item_tags(doc)
            if doc_tags:
                doc_info["tags"] = doc_tags

            if include_preview:
                # Download and extract preview (skip notebooks - they need slow OCR)
                file_type = await run_blocking(get_file_type, client, doc)
                if file_type == "notebook":
                    # Notebooks need OCR for preview, skip for performance
                    doc_info["preview_skipped"] = "notebook (use remarkable_read with include_ocr)"
                else:
                    # PDFs and EPUBs have extractable text - fast to preview
                    try:
                        raw_doc = await run_blocking(client.download, doc)
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                            tmp.write(raw_doc)
                            tmp_path = Path(tmp.name)

                        try:
                            content = await run_blocking(
                                extract_text_from_document_zip,
                                tmp_path,
                                include_ocr=False,
                                doc_id=doc.ID,
                                version=document_version(doc),
                            )
                            preview_text = "\n".join(content["typed_text"])[:200]
                            if preview_text:
                                if len(preview_text) == 200:
                                    doc_info["preview"] = preview_text + "..."
                                else:
                                    doc_info["preview"] = preview_text
                            # No preview key if empty - cleaner response
                        finally:
                            tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass  # No preview key on error - cleaner response

            results.append(doc_info)

        result = {"count": len(results), "documents": results}

        if results:
            next_limit = min(limit * 2, 50)
            hint = (
                f"Showing {len(results)} recent documents. "
                f"To read one: remarkable_read('{results[0]['name']}'). "
                f"To see more: remarkable_recent(limit={next_limit})."
            )
        else:
            hint = "No documents found. Use remarkable_browse('/') to check your library."

        return make_response(result, hint)

    except Exception as e:
        return make_error(
            error_type="recent_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
async def remarkable_search(
    query: Optional[str] = None,
    grep: Optional[str] = None,
    limit: int = 5,
    include_ocr: bool = False,
    tags: Optional[List[str]] = None,
    folder: Optional[str] = None,
) -> str:
    """
    <usecase>Read several documents in one call, picked by name, folder or tags,
    optionally keeping only the lines that match a pattern.</usecase>
    <instructions>
    Picks documents first, then reads each one:
    1. Candidates are documents whose names contain query (case-insensitive),
       inside folder and its subfolders when given, with any of tags when
       given. At least one of query, folder or tags is needed. Without query,
       every document in folder (or with tags) is a candidate, so grep
       searches content regardless of names.
    2. The first `limit` candidates by path (default 5, at most 20) are read
       like remarkable_read: the first part of the text (up to 2000
       characters per document), or with grep each match with its context
       and tablet page. "candidates" gives how many there were.

    To find a document by name only, remarkable_browse(query=...) is faster.
    To search one long document, use remarkable_read(document, grep=...).
    </instructions>
    <parameters>
    - query: Text the document names must contain (optional)
    - grep: Optional regex to find in each document's text
    - limit: Documents to read (default: 5, max: 20)
    - include_ocr: OCR handwriting before searching it (default: False; slow)
    - tags: Documents with any of these tags (optional, case-insensitive)
    - folder: Only documents in this folder and its subfolders (optional)
    </parameters>
    <examples>
    - remarkable_search("meeting")  # Read docs with "meeting" in the name
    - remarkable_search("journal", grep="project")  # "project" in journals
    - remarkable_search(folder="/Work", grep="budget")  # content search in a folder
    - remarkable_search("project", tags=["work"])  # Work-tagged projects
    </examples>
    """
    if not query and folder is None and not tags:
        return invalid_param(
            "query: give query, folder or tags to pick the documents to search.",
            "Example: remarkable_search(folder='/Work', grep='budget').",
        )
    limit = min(max(1, limit), MAX_SEARCH_DOCUMENTS)
    try:
        client = await run_blocking(get_rmapi)
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)
        candidates = scoped_items(collection, items_by_id, "document")
        if folder is not None:
            folder_id, error = lookup_folder(folder, collection, items_by_id)
            if error:
                return error
            if folder_id:
                inside = _descendant_items(get_items_by_parent(collection), folder_id)
                inside_ids = {item.ID for item in inside}
                candidates = [item for item in candidates if item.ID in inside_ids]
        if query:
            candidates = [c for c in candidates if query.lower() in c.VissibleName.lower()]
        if tags:
            candidates = [c for c in candidates if _matches_item_tags(c, tags)]
        paths = {c.ID: _apply_root_filter(get_item_path(c, items_by_id)) for c in candidates}
        candidates.sort(key=lambda c: paths[c.ID].lower())

        if not candidates:
            picked = " and ".join(
                part
                for part in (
                    f"names containing '{query}'" if query else "",
                    f"in '{folder}'" if folder is not None else "",
                    f"with tags {tags}" if tags else "",
                )
                if part
            )
            return make_error(
                error_type="no_documents_found",
                message=f"No documents found {picked}.",
                suggestion="Try a different search term or use remarkable_browse('/') to list all.",
            )

        search_results = []
        for doc in candidates[:limit]:
            doc_result = {
                "name": doc.VissibleName,
                "path": paths[doc.ID],
                "modified": getattr(doc, "ModifiedClient", None),
            }
            if _item_tags(doc):
                doc_result["tags"] = _item_tags(doc)
            if _item_page_tags(doc):
                doc_result["page_tags"] = _item_page_tags(doc)
            try:
                out = await _read_document(paths[doc.ID], "text", 1, grep, include_ocr)
                if isinstance(out, str):
                    doc_result["error"] = json.loads(out)["_error"]["message"]
                else:
                    data, _ = out
                    content = data.get("content", "")
                    doc_result["content"] = content[:SEARCH_CONTENT_CHARS]
                    doc_result["total_pages"] = data.get("total_pages", 1)
                    doc_result["total_pages_known"] = data.get("total_pages_known", True)
                    doc_result["content_pages"] = data.get("content_pages", 1)
                    if grep:
                        doc_result["grep_matches"] = data.get("grep_matches", 0)
                        doc_result["match_pages"] = list(
                            dict.fromkeys(m["page"] for m in data.get("matches", []) if m["page"])
                        )
                    if len(content) > SEARCH_CONTENT_CHARS:
                        doc_result["truncated"] = True
            except Exception as e:
                doc_result["error"] = str(e)
            search_results.append(doc_result)

        result = {
            "query": query,
            "grep": grep,
            "count": len(search_results),
            "candidates": len(candidates),
            "documents": search_results,
        }
        if folder is not None:
            result["folder"] = folder
        if tags:
            result["filter_tags"] = tags

        docs_with_content = [d for d in search_results if "content" in d]
        tag_desc = f" with tags {tags}" if tags else ""
        if grep:
            found = [d for d in docs_with_content if d.get("grep_matches")]
            matches = sum(d["grep_matches"] for d in found)
            hint = (
                f"Read {len(docs_with_content)} document(s){tag_desc}; {len(found)} "
                f"have {matches} match(es) for '{grep}'."
            )
        else:
            hint = f"Found {len(docs_with_content)} document(s){tag_desc}."
        if len(candidates) > limit:
            hint += (
                f" {len(candidates) - limit} more candidate(s) were not read; narrow the search."
            )
        if docs_with_content:
            hint += f" To read more: remarkable_read('{docs_with_content[0]['path']}')."

        return make_response(result, hint)

    except Exception as e:
        return make_error(
            error_type="search_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


def _archive_layout(archive: bytes, pdf: Optional[bytes], page: int) -> dict:
    """``page_layout.page_layout`` for a downloaded document archive."""
    from remarkable_mcp.page_layout import page_layout

    page_map = _archive_page_map(archive)
    with tempfile.TemporaryDirectory(prefix="remarkable-layout-") as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            zf.extractall(root)
        return page_layout(root, pdf, page, page_map)


@mcp.tool(annotations=LAYOUT_ANNOTATIONS)
async def remarkable_page_layout(document: str, page: int = 1) -> str:
    """
    <usecase>Find where things are on a page: the PDF's text lines, the user's
    handwriting, marks and free space.</usecase>
    <instructions>
    Returns regions on one tablet page as page fractions [left, top, right,
    bottom], the frame remarkable_image's region uses:
    - text_lines: the PDF's printed lines (before marks), each with its text
    - ink: boxes around the user's handwriting on visible layers (roughly a
      word or a line each)
    - free: the largest empty regions, clear of ink, text and marks
    - layers: the page's layers, bottom first
    Pass an ink box to remarkable_image(document, page, region=...) to read
    that writing, or place marks in free space with remarkable_mark_pdf.
    Notebooks have ink but no text lines. Never changes the tablet.
    </instructions>
    <parameters>
    - document: Document name or path
    - page: Tablet page number (default: 1)
    </parameters>
    <examples>
    - remarkable_page_layout("Week 3 worksheet", page=2)
    </examples>
    """
    from remarkable_mcp.pdf_marks import MarkError

    try:
        client = await run_blocking(get_rmapi)
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)
        target_doc, error = lookup(
            client, document, kind="document", collection=collection, items_by_id=items_by_id
        )
        if error:
            return error
        archive = await run_blocking(client.download, target_doc)
        if _is_pdf_payload(archive):
            return make_error(
                error_type="layout_not_available",
                message="This transport returned only the PDF, without the tablet's page files.",
                suggestion="Use remarkable_image to look at the page instead.",
            )
        pdf = None
        if await run_blocking(get_file_type, client, target_doc) == "pdf":
            pdf = await run_blocking(download_raw_file, client, target_doc, "pdf")
        layout = await run_blocking(_archive_layout, archive, pdf, page)
    except MarkError as e:
        return invalid_param(f"page: {e}", "Pages are numbered from 1.")
    except Exception as e:
        return make_error(
            error_type="layout_failed",
            message=f"Failed to read the page layout: {e}",
            suggestion="Check remarkable_status() and try again.",
        )
    data = {
        "document": target_doc.VissibleName,
        "path": _apply_root_filter(get_item_path(target_doc, items_by_id)),
        **layout,
    }
    hint = (
        f"Page {page}: {len(layout['text_lines'])} text lines, {len(layout['ink'])} ink "
        "regions. Crop to a region with remarkable_image(document, page, region=...)."
    )
    return make_response(data, hint)


@mcp.tool(annotations=STATUS_ANNOTATIONS)
async def remarkable_status() -> str:
    """
    <usecase>Check connection status, active transport, and write capabilities.</usecase>
    <instructions>
    Returns authentication status, the active transport (cloud, local-dir, ssh, or
    usb-web), the document count, and a capability matrix describing what each
    transport can do. Use this to verify your connection, choose a transport, or
    troubleshoot.

    Capability notes:
    - Export is available in every transport and creates only a bounded temporary
      local resource; it never modifies the tablet.
    - Cloud (default): full read/render/export/upload/mkdir/move/rename/delete — no device
      needed, works from anywhere your token is valid.
    - SSH: full capabilities over a local/USB connection to the tablet.
    - USB web: read, render, export, and upload (to root) only — the tablet's USB web
      interface firmware exposes no folder/move/rename/delete endpoints. For full
      write parity over a USB cable, use SSH mode pointed at the USB IP.
    - Local directory: fully offline read/render/export access to the desktop app's sync
      cache. Strictly read-only to avoid corrupting app-managed state.
    Write tools (upload/mkdir/move/rename/delete) are enabled by default; run
    with --read-only (or REMARKABLE_READ_ONLY=1) to expose a read-only server.
    </instructions>
    <examples>
    - remarkable_status()
    </examples>
    """

    from remarkable_mcp.api import (
        REMARKABLE_USE_LOCAL_DIR,
        REMARKABLE_USE_SSH,
        REMARKABLE_USE_USB_WEB,
        get_active_transport,
    )
    from remarkable_mcp.write_tools import write_enabled

    # Determine the *selected* transport from configuration (pre-fallback).
    if REMARKABLE_USE_LOCAL_DIR:
        from remarkable_mcp.local_dir import find_default_local_dir

        selected_transport = "local-dir"
        local_dir = os.environ.get("REMARKABLE_LOCAL_DIR") or str(
            find_default_local_dir() or "(no directory found)"
        )
        connection_info = f"local directory at {local_dir}"
    elif REMARKABLE_USE_USB_WEB:
        from remarkable_mcp.usb_web import DEFAULT_USB_HOST

        selected_transport = "usb-web"
        usb_host = os.environ.get("REMARKABLE_USB_HOST", DEFAULT_USB_HOST)
        connection_info = f"USB web interface at {usb_host}"
    elif REMARKABLE_USE_SSH:
        from remarkable_mcp.ssh import (
            DEFAULT_SSH_HOST,
            DEFAULT_SSH_PORT,
            DEFAULT_SSH_USER,
        )

        selected_transport = "ssh"
        ssh_host = os.environ.get("REMARKABLE_SSH_HOST", DEFAULT_SSH_HOST)
        ssh_user = os.environ.get("REMARKABLE_SSH_USER", DEFAULT_SSH_USER)
        ssh_port = int(os.environ.get("REMARKABLE_SSH_PORT", str(DEFAULT_SSH_PORT)))
        connection_info = f"SSH to {ssh_user}@{ssh_host}:{ssh_port}"
    else:
        selected_transport = "cloud"
        connection_info = "environment variable" if REMARKABLE_TOKEN else "file (~/.rmapi)"

    # What each transport is capable of (independent of read-only mode).
    # Read/render/export are always available; the remaining booleans are writes.
    capability_matrix = {
        "cloud": {
            "read": True,
            "render": True,
            "export": True,
            "upload": True,
            "mkdir": True,
            "move": True,
            "rename": True,
            "tag": True,
            "mark_pdf": True,
            "delete": True,
        },
        "ssh": {
            "read": True,
            "render": True,
            "export": True,
            "upload": True,
            "mkdir": True,
            "move": True,
            "rename": True,
            "tag": True,
            "mark_pdf": False,
            "delete": True,
        },
        "usb-web": {
            "read": True,
            "render": True,
            "export": True,
            "upload": True,
            "mkdir": False,
            "move": False,
            "rename": False,
            "tag": False,
            "mark_pdf": False,
            "delete": False,
        },
        # Local directory is strictly read-only: the folder is the desktop
        # app's private sync cache, so direct writes would bypass sync.
        "local-dir": {
            "read": True,
            "render": True,
            "export": True,
            "upload": False,
            "mkdir": False,
            "move": False,
            "rename": False,
            "tag": False,
            "mark_pdf": False,
            "delete": False,
        },
    }
    writes_on = write_enabled()
    transport = selected_transport

    try:
        client = await run_blocking(get_rmapi)
        # Reflect any startup fallback to cloud (device unreachable + token set).
        transport = get_active_transport()
        fell_back = transport != selected_transport
        if fell_back:
            connection_info = (
                f"cloud (fell back from {selected_transport}: transport unavailable, "
                "cloud token configured)"
            )

        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)

        root = _get_root_path()

        # Count documents (not folders or trashed, filtered by root)
        doc_count = 0
        for item in collection:
            if item.is_folder or _is_cloud_archived(item, items_by_id):
                continue
            item_path = get_item_path(item, items_by_id)
            if _is_within_root(item_path, root):
                doc_count += 1

        # Effective capabilities for the active transport: write ops only count
        # when not in read-only mode.
        transport_caps = capability_matrix[transport]
        effective_caps = {
            cap: (supported if cap in ("read", "render", "export") else supported and writes_on)
            for cap, supported in transport_caps.items()
        }

        result = {
            "authenticated": True,
            "transport": transport,
            "connection": connection_info,
            "status": "connected",
            "document_count": doc_count,
            "write_enabled": writes_on,
            "capabilities": effective_caps,
            "capabilities_by_transport": capability_matrix,
        }
        reliability_status = getattr(type(client), "reliability_status", None)
        if callable(reliability_status):
            result["reliability"] = reliability_status(client)
        if fell_back:
            result["fell_back_to_cloud"] = True

        # Add root path info if configured
        if root != "/":
            result["root_path"] = root

        hint_parts = [f"Connected successfully via {transport}. Found {doc_count} documents."]
        hint_parts.append(
            "Single-document PDF/Markdown export is available and does not modify the tablet."
        )
        if fell_back:
            hint_parts.append(
                f"Note: {selected_transport} was selected but unavailable, so it "
                "fell back to cloud (a cloud token is configured). Set "
                "REMARKABLE_DISABLE_CLOUD_FALLBACK=1 to disable this."
            )
        if root != "/":
            hint_parts.append(f"Filtered to root: {root}")
        if writes_on:
            if transport == "usb-web":
                hint_parts.append(
                    "Write is enabled, but the USB web interface only supports upload "
                    "(to root). For mkdir/move/rename/delete over USB, use SSH mode."
                )
            else:
                marks = " mark_pdf (cloud only)," if transport == "cloud" else ""
                hint_parts.append(
                    f"Write is enabled: upload, mkdir, move, rename, tag,{marks} and "
                    "delete are available."
                )
        elif selected_transport == "local-dir":
            hint_parts.append(
                "Write tools are disabled because the local-directory transport "
                "is strictly read-only."
            )
        else:
            hint_parts.append(
                "Read-only mode is active (--read-only / REMARKABLE_READ_ONLY=1). "
                "Restart without it to enable write tools."
            )
        hint_parts.append(
            "Use remarkable_browse() to see your files, "
            "or remarkable_recent() for recent documents."
        )

        return make_response(result, " ".join(hint_parts))

    except Exception as e:
        error_msg = str(e)
        # Reflect fallback in the reported transport when one occurred.
        transport = get_active_transport()

        result = {
            "authenticated": False,
            "transport": transport,
            "connection": connection_info,
            "error": error_msg,
            "write_enabled": writes_on,
            "capabilities_by_transport": capability_matrix,
        }

        if transport == "ssh":
            hint = (
                "SSH connection failed. Make sure:\n"
                "1) Developer mode / SSH is enabled on your tablet\n"
                "2) Your reMarkable is connected (USB or same network)\n"
                "3) You can run: ssh root@10.11.99.1\n\n"
                "See: https://remarkable.guide/guide/access/ssh.html\n\n"
                "Or use cloud mode instead (remove --ssh flag) — no device needed."
            )
        elif transport == "usb-web":
            hint = (
                "USB web interface not reachable. Make sure:\n"
                "1) Your reMarkable is connected via USB\n"
                "2) USB web interface is enabled (Settings → Storage)\n"
                "3) The device is on and unlocked"
            )
        elif transport == "local-dir":
            hint = (
                "Local data directory not usable. Make sure:\n"
                "1) The reMarkable desktop app is installed and signed in "
                "(it creates and syncs the folder), or\n"
                "2) REMARKABLE_LOCAL_DIR points to a directory containing "
                "xochitl-style data (*.metadata files)\n"
                "Keep the desktop app running so the folder stays in sync."
            )
        else:
            hint = (
                "Cloud authentication failed. To connect:\n"
                "1) Go to https://my.remarkable.com/device/browser/connect\n"
                "2) Get a one-time code\n"
                "3) Run: uvx remarkable-mcp --register YOUR_CODE\n"
                "Cloud mode works from anywhere — no device required. SSH/USB modes "
                "are also available for local access (add --ssh or --usb)."
            )

        return make_response(result, hint)


MIN_IMAGE_DPI = 36
MAX_IMAGE_DPI = 600


def _image_region_error(region: List[float]) -> Optional[str]:
    """Why ``region`` is not a valid page-fraction crop box, or None."""
    if len(region) != 4:
        return "region needs four numbers: [left, top, right, bottom]."
    left, top, right, bottom = region
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return (
            "region values are fractions of the page width and height and need "
            "0 <= left < right <= 1 and 0 <= top < bottom <= 1."
        )
    return None


def _crop_png(png_data: bytes, region: List[float]) -> bytes:
    """Crop a PNG to ``[left, top, right, bottom]`` fractions of its size."""
    import io

    from PIL import Image as PILImage

    image = PILImage.open(io.BytesIO(png_data))
    width, height = image.size
    left, top, right, bottom = region
    box = (
        int(round(left * width)),
        int(round(top * height)),
        max(int(round(right * width)), int(round(left * width)) + 1),
        max(int(round(bottom * height)), int(round(top * height)) + 1),
    )
    output = io.BytesIO()
    image.crop(box).save(output, format="PNG")
    return output.getvalue()


async def _render_png_page(
    client,
    target_doc,
    tmp_path: Path,
    page: int,
    background: str,
    render_merged: bool,
    allow_pdf_fallback: bool,
    layers: Optional[str] = None,
    dpi: Optional[float] = None,
    full_page: bool = False,
) -> tuple[Optional[bytes], Optional[str], bool, bool]:
    """Render a PNG with the same fallbacks used by remarkable_image.

    ``full_page`` renders annotation-only pages over the whole page instead of
    cropping to the ink, so page coordinates (and ``dpi``) apply.
    """
    merged_note = None
    is_merged = False
    if render_merged:
        png_data, merged_note = await run_blocking(
            render_merged_page_from_document_zip,
            tmp_path,
            page,
            background_color=background,
            layers=layers,
            dpi=dpi,
        )
        is_merged = png_data is not None and merged_note is None
    elif full_page:
        full = await run_blocking(
            render_page_full_page_from_document_zip,
            tmp_path,
            page,
            background_color=background,
            layers=layers,
            dpi=dpi,
        )
        png_data = full[0] if full is not None else None
    else:
        png_data = await run_blocking(
            render_page_from_document_zip,
            tmp_path,
            page,
            background_color=background,
            layers=layers,
        )

    rendered_via_pdf = False
    if png_data is None and allow_pdf_fallback:
        png_data, has_source_pdf = await run_blocking(
            render_mapped_pdf_page_from_document_zip, tmp_path, page
        )
        rendered_via_pdf = png_data is not None
        if png_data is None and not has_source_pdf:
            pdf_bytes = await run_blocking(download_raw_file, client, target_doc, "pdf")
            if pdf_bytes:
                png_data = await run_blocking(
                    render_tablet_pdf_page_to_png, pdf_bytes, page, dpi=dpi
                )
                rendered_via_pdf = png_data is not None

    if png_data is None:
        full = await run_blocking(
            render_page_full_page_from_document_zip,
            tmp_path,
            page,
            background_color=background,
            layers=layers,
            dpi=dpi,
        )
        if full is not None:
            png_data = full[0]

    return png_data, merged_note, is_merged, rendered_via_pdf


def _publish_png(png_data: bytes, title: str) -> PublishedExport[None]:
    """Publish a rendered page image as a temporary resource."""

    def write(path: Path) -> None:
        path.write_bytes(png_data)

    return export_store.publish(filename=title, output_format="png", writer=write)


@mcp.tool(annotations=IMAGE_ANNOTATIONS)
async def remarkable_image(
    document: str,
    page: int = 1,
    background: Optional[str] = None,
    output_format: str = "png",
    compatibility: bool = False,
    include_ocr: bool = False,
    render_merged: bool = True,
    layers: str = "visible",
    dpi: Optional[int] = None,
    region: Optional[List[float]] = None,
    marks: bool = True,
):
    """
    <usecase>Get an image of a specific page from a reMarkable document.</usecase>
    <instructions>
    Renders a notebook or document page as an image (PNG or SVG). This is useful for:
    - Viewing hand-drawn diagrams, sketches, or UI mockups
    - Getting visual context that text extraction might miss
    - Implementing designs based on hand-drawn wireframes
    - SVG format for scalable vector graphics that can be edited

    ## Merged PDF + Annotation Rendering

    PNG pages backed by an imported PDF automatically composite the PDF page with
    its reMarkable annotations. Set render_merged=False for an annotation-only
    render, or True to explicitly request compositing. SVG remains annotation-only.

    ## What the Render Shows

    - Pasted images (captures) are drawn where they were placed, including
      moved, resized and rotated ones, in the tablet's stacking order: a later
      capture covers an earlier one, and ink drawn after pasting is on top.
      Captures are opaque, so they hide what is beneath them. Captures on
      hidden layers follow the layers selection like ink.
    - Notebook page templates (dots, lines, grids) are not drawn: notebook
      pages render on a plain background. A dot grid inside a capture is part
      of the capture. remarkable_read lists each page's template.
    - Positions match remarkable_read: its regions line up with the render on
      PDF-backed pages and notebooks.
    - Ink uses the colors the desktop app draws, and pen strokes their stored
      widths.

    ## Response Formats

    By default, images are returned as embedded resources (EmbeddedResource) which
    include the full image data inline:
    - PNG: Returned as BlobResourceContents with base64-encoded data
    - SVG: Returned as TextResourceContents with SVG markup

    If your client doesn't support embedded resources in tool responses, set
    compatibility=True to receive a JSON response with just a resource URI. For
    PNG it points to a temporary copy of the rendered image that expires after
    15 minutes; the client can then fetch the resource separately.

    Optionally, enable include_ocr=True to extract text from the image using OCR.
    Google Vision is used when configured; otherwise OCR runs locally with Tesseract.

    Note: Native notebooks retain their existing stroke rendering behavior.
    If older USB firmware returns only a native PDF export, PNG remains
    available; SVG and explicit annotation-only rendering require an rmdoc
    archive and return a clear error when it is unavailable.
    </instructions>
    <parameters>
    - document: Document name or path (use remarkable_browse to find documents)
    - page: Page number (default: 1, 1-indexed)
    - background: Background color as hex code. Supports RGB (#RRGGBB) or RGBA (#RRGGBBAA).
      Default is "#FBFBFB" (reMarkable paper color), or set REMARKABLE_BACKGROUND_COLOR
      env var to override. Use "#00000000" for transparent.
    - output_format: Output format - "png" (default) or "svg" for vector graphics
    - compatibility: If True, return resource URI in JSON instead of embedded resource.
      Use this if your client doesn't support embedded resources in tool responses.
    - include_ocr: Enable OCR text extraction from the image (default: False).
    - render_merged: For PNG, True (default) draws PDF-backed pages over their
      PDF page; False returns the annotation-only layer. SVG output is always
      annotation-only.
    - layers: Which page layers to draw: "visible" (default) draws what the tablet
      shows, "all" includes hidden layers, or a layer name (e.g. "Layer 3") draws
      only that layer. remarkable_read(content_type="annotations") lists layers.
    - dpi: PNG resolution in dots per inch of the physical page (36-600). Renders
      the whole page rather than cropping to the ink. Use 300 to read small
      handwriting. Default: about 1872 px on the long edge (notebooks) or 144 dpi
      (PDF-backed pages).
    - region: PNG crop box [left, top, right, bottom] as fractions of the page
      (0-1), e.g. [0, 0.5, 1, 0.75] for the third quarter of the page. Combine
      with dpi to inspect one area closely without a huge image.
    - marks: Draw marks added with remarkable_mark_pdf (default True). False
      shows the page as it was before marking, with the user's ink.
    </parameters>
    <examples>
    - remarkable_image("UI Mockup")  # Get first page as embedded PNG resource
    - remarkable_image("Meeting Notes", page=2)  # Get second page
    - remarkable_image("/Work/Designs/Wireframe", background="#FFFFFF")  # White background
    - remarkable_image("Sketch", background="#00000000")  # Transparent background
    - remarkable_image("Diagram", output_format="svg")  # Get as embedded SVG resource
    - remarkable_image("Notes", compatibility=True)  # Return resource URI for retry
    - remarkable_image("Notes", include_ocr=True)  # Get image with OCR text extraction
    - remarkable_image("Annotated PDF")  # PDF + annotations composited automatically
    - remarkable_image("Annotated PDF", render_merged=False)  # Annotation layer only
    - remarkable_image("Notes", page=3, layers="all")  # Include hidden layers
    - remarkable_image("Notes", page=3, layers="Layer 3")  # One named layer
    - remarkable_image("Worksheet", page=2, dpi=300)  # Full page at 300 dpi
    - remarkable_image("Worksheet", page=2, dpi=300, region=[0, 0.4, 1, 0.6])  # One band
    </examples>
    """
    try:
        # Resolve background color: use provided value or get from env/default
        if background is None:
            background = await run_blocking(get_background_color)

        client = await run_blocking(get_rmapi)
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)

        target_doc, error = lookup(
            client, document, kind="document", collection=collection, items_by_id=items_by_id
        )
        if error:
            return error

        format_lower = output_format.lower()
        if format_lower not in ("png", "svg"):
            return invalid_param(
                f"output_format: invalid format '{output_format}'. Supported formats: png, svg",
                "Use output_format='png' for raster or 'svg' for vectors.",
            )

        if (dpi is not None or region is not None) and format_lower != "png":
            return invalid_param(
                "dpi and region apply to PNG output only.",
                "Use output_format='png' with dpi or region.",
            )
        if dpi is not None and not (MIN_IMAGE_DPI <= dpi <= MAX_IMAGE_DPI):
            return invalid_param(
                f"dpi must be between {MIN_IMAGE_DPI} and {MAX_IMAGE_DPI}.",
                "Use dpi=300 to read handwriting; add region to keep images small.",
            )
        if region is not None:
            region_error = _image_region_error(region)
            if region_error:
                return invalid_param(
                    region_error,
                    "Example: region=[0, 0.5, 1, 0.75] for the third quarter.",
                )

        raw_doc = await run_blocking(client.download, target_doc)
        native_pdf = raw_doc if _is_pdf_payload(raw_doc) else None
        tmp_path: Optional[Path] = None
        if native_pdf is None and not marks:
            raw_doc = await run_blocking(_archive_without_marks, raw_doc)
        if native_pdf is None:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(raw_doc)
                tmp_path = Path(tmp.name)

        try:
            if native_pdf is not None:
                total_pages = await run_blocking(_count_pdf_pages, native_pdf)
                file_type = "pdf"
            else:
                total_pages = await run_blocking(get_document_page_count, tmp_path)
                file_type = await run_blocking(get_document_file_type, tmp_path)
                if not file_type:
                    file_type = await run_blocking(get_file_type, client, target_doc)
            # Notebooks and EPUBs have no PDF page to draw under the ink.
            use_merged = render_merged and file_type not in ("notebook", "epub")

            if total_pages == 0:
                return make_error(
                    error_type="no_pages",
                    message=f"Document '{target_doc.VissibleName}' has no renderable pages.",
                    suggestion=(
                        "This may be a PDF/EPUB without annotations. "
                        "Use remarkable_read() to extract text content instead."
                    ),
                )

            if page < 1 or page > total_pages:
                return make_error(
                    error_type="page_out_of_range",
                    message=f"Page {page} does not exist. Document has {total_pages} page(s).",
                    suggestion=f"Use page=1 to {total_pages} to view different pages.",
                )

            layer_selection = (layers or "visible").strip()
            if layer_selection.lower() not in ("visible", "all") and tmp_path is not None:
                page_layers = await run_blocking(get_page_layers_from_document_zip, tmp_path, page)
                names = [la["name"] for la in page_layers]
                if layer_selection.lower() not in (n.lower() for n in names):
                    return make_error(
                        error_type="layer_not_found",
                        message=f"Page {page} has no layer named '{layer_selection}'.",
                        suggestion=(
                            f"Layers on this page: {', '.join(names)}."
                            if names
                            else "This page has no named layers; use layers='visible'."
                        ),
                        did_you_mean=names or None,
                    )

            # Build resource URI for this page
            doc_path = _apply_root_filter(get_item_path(target_doc, items_by_id))
            uri_path = doc_path.lstrip("/")

            # Render the page based on format
            merged_note = None
            is_merged = False

            if format_lower == "svg":
                if native_pdf is not None:
                    return make_error(
                        error_type="svg_not_available",
                        message=(
                            "SVG output is unavailable because this transport returned "
                            "a native PDF instead of a document archive."
                        ),
                        suggestion="Retry with output_format='png'.",
                    )

                svg_content = await run_blocking(
                    render_page_from_document_zip_svg,
                    tmp_path,
                    page,
                    background_color=background,
                    layers=layer_selection,
                )

                if svg_content is None:
                    return make_error(
                        error_type="render_failed",
                        message="Failed to render page to SVG.",
                        suggestion=(
                            "SVG output requires local stroke parsing, so the "
                            "page may be empty or in a newer format. Try "
                            "output_format='png', which falls back to the "
                            "tablet's native PDF export in USB/SSH mode, or "
                            "remarkable_read() to extract text instead."
                        ),
                    )

                resource_uri = f"remarkablesvg:///{uri_path}.page-{page}.svg"

                if compatibility:
                    # Return SVG content in JSON for clients without embedded resource support
                    hint = (
                        f"Page {page}/{total_pages} as SVG. "
                        f"Use compatibility=False for embedded resource format."
                    )
                    if merged_note:
                        hint = f"{merged_note} {hint}"
                    response_data = {
                        "svg": svg_content,
                        "mime_type": "image/svg+xml",
                        "page": page,
                        "total_pages": total_pages,
                        "resource_uri": resource_uri,
                        "merged": False,
                    }
                    return make_response(response_data, hint)
                else:
                    # Return SVG as embedded TextResourceContents with info hint
                    text_resource = TextResourceContents(
                        uri=resource_uri,
                        mime_type="image/svg+xml",
                        text=svg_content,
                    )
                    embedded = EmbeddedResource(type="resource", resource=text_resource)
                    info_text = (
                        f"Page {page}/{total_pages} of '{target_doc.VissibleName}' as SVG. "
                        f"Resource URI: {resource_uri}"
                    )
                    if merged_note:
                        info_text = f"{merged_note}\n{info_text}"
                    info = TextContent(type="text", text=info_text)
                    return [info, embedded]
            else:
                if native_pdf is not None and not render_merged:
                    return make_error(
                        error_type="annotation_only_not_available",
                        message=(
                            "Annotation-only rendering is unavailable because this "
                            "transport returned a native PDF instead of a document archive."
                        ),
                        suggestion=(
                            "Use the default PNG render, or connect through cloud/SSH or "
                            "newer USB firmware that supports rmdoc."
                        ),
                    )
                if native_pdf is not None:
                    png_data = await run_blocking(
                        render_tablet_pdf_page_to_png,
                        native_pdf,
                        page,
                        dpi=dpi,
                    )
                    rendered_via_pdf = png_data is not None
                else:
                    (
                        png_data,
                        merged_note,
                        is_merged,
                        rendered_via_pdf,
                    ) = await _render_png_page(
                        client,
                        target_doc,
                        tmp_path,
                        page,
                        background,
                        use_merged,
                        render_merged,
                        layer_selection,
                        dpi,
                        dpi is not None or region is not None,
                    )

                if png_data is None:
                    return make_error(
                        error_type="render_failed",
                        message="Failed to render page to image.",
                        suggestion=(
                            "Local stroke parsing and PyMuPDF SVG rasterization both "
                            "failed. Reinstall remarkable-mcp to restore its Python "
                            "dependencies. For PDF-backed documents the source PDF is "
                            "used automatically as a fallback; otherwise try "
                            "remarkable_read() to extract text instead."
                        ),
                    )

                if region is not None:
                    png_data = await run_blocking(_crop_png, png_data, region)
                render_info = {}
                if dpi is not None:
                    render_info["dpi"] = dpi
                if region is not None:
                    render_info["region"] = region

                # Handle OCR if requested - extract text from the image
                ocr_text = None
                ocr_backend_used = None
                if include_ocr:
                    ocr_text, ocr_backend_used = await run_blocking(ocr.image_text, png_data)

                uri_suffix = ".merged.png" if is_merged else ".png"
                resource_uri = f"remarkableimg:///{uri_path}.page-{page}{uri_suffix}"

                # Build OCR info for response if OCR was requested
                ocr_info = {}
                if include_ocr:
                    ocr_info["ocr_text"] = ocr_text
                    ocr_info["ocr_backend"] = ocr_backend_used
                    if ocr_text is None:
                        ocr_info["ocr_message"] = "No text detected in image"

                if compatibility:
                    # Clients without embedded resource support get a link to a
                    # temporary resource holding exactly this rendered image.
                    published = await run_blocking(
                        _publish_png,
                        png_data,
                        f"{target_doc.VissibleName} page {page}",
                    )
                    image_resource = published.resource
                    hint = (
                        f"Page {page}/{total_pages} as a PNG resource; read resource_uri "
                        f"before it expires at {image_resource.expires_at.isoformat()}. "
                        f"Use compatibility=False to embed the image instead."
                    )
                    if include_ocr and ocr_text:
                        hint = (
                            f"Page {page}/{total_pages} with OCR text "
                            f"(backend: {ocr_backend_used})."
                        )
                    elif include_ocr:
                        hint = f"Page {page}/{total_pages}. No text detected via OCR."
                    if merged_note:
                        hint = f"{merged_note} {hint}"
                    elif is_merged:
                        hint = f"Rendered with PDF + annotation compositing. {hint}"
                    if rendered_via_pdf:
                        hint = (
                            "Rendered via the tablet's native PDF export "
                            "(local stroke render unavailable). " + hint
                        )

                    response_data = {
                        "mime_type": "image/png",
                        "page": page,
                        "total_pages": total_pages,
                        "resource_uri": image_resource.uri,
                        "size": image_resource.size,
                        "expires_at": image_resource.expires_at.isoformat(),
                        "merged": is_merged,
                        "render_source": "tablet_pdf" if rendered_via_pdf else "strokes",
                        **render_info,
                        **ocr_info,
                    }
                    return make_response(response_data, hint)
                else:
                    # Return PNG as embedded BlobResourceContents with info hint
                    png_base64 = base64.b64encode(png_data).decode("utf-8")
                    blob_resource = BlobResourceContents(
                        uri=resource_uri,
                        mime_type="image/png",
                        blob=png_base64,
                    )
                    embedded = EmbeddedResource(type="resource", resource=blob_resource)

                    info_text = f"Page {page}/{total_pages} of '{target_doc.VissibleName}' as PNG. "
                    info_text += f"Resource URI: {resource_uri}"
                    if dpi is not None:
                        info_text += f"\nRendered at {dpi} dpi."
                    if region is not None:
                        info_text += f"\nCropped to region {region} (fractions of the page)."
                    if is_merged:
                        info_text += "\nRendered with PDF + annotation compositing."
                    if rendered_via_pdf:
                        info_text += (
                            "\nRendered via the tablet's native PDF export "
                            "(local stroke render unavailable)."
                        )
                    if merged_note:
                        info_text += f"\nNote: {merged_note}"
                    if include_ocr and ocr_text:
                        info_text += f"\n\nOCR Text (via {ocr_backend_used}):\n{ocr_text}"
                    elif include_ocr:
                        info_text += "\n\nOCR: No text detected in image."

                    info = TextContent(type="text", text=info_text)
                    return [info, embedded]

        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    except Exception as e:
        return make_error(
            error_type="image_failed",
            message=str(e),
            suggestion="Check remarkable_status() to verify your connection.",
        )


@dataclass(frozen=True)
class _DocumentExportResult:
    build: ExportBuildResult
    metadata: ExportMetadata
    representation: str
    pdf_mode: PdfMode | None = None


def _parse_page_spec(pages) -> List[tuple]:
    """``(first, last)`` ranges from ``5``, ``"5"`` or ``"5-9,12"``; ValueError if malformed."""
    if isinstance(pages, bool):
        raise ValueError("pages must be a page number or ranges like '5-9,12'.")
    if isinstance(pages, int):
        parts = [str(pages)]
    else:
        parts = [part.strip() for part in str(pages).split(",")]
    ranges = []
    for part in parts:
        first, sep, last = part.partition("-")
        try:
            start = int(first)
            end = int(last) if sep else start
        except ValueError:
            raise ValueError(
                f"Invalid page selection '{part}'. Use a page number or ranges like '5-9,12'."
            )
        if start < 1 or end < start:
            raise ValueError(
                f"Invalid page range '{part}'. Pages start at 1 and ranges run low to high."
            )
        ranges.append((start, end))
    return ranges


def _select_pages(ranges: List[tuple], page_count: int) -> List[int]:
    """Pages in ``ranges``, in document order without repeats, checked against the count."""
    out_of_range = [r for r in ranges if r[1] > page_count]
    if out_of_range:
        first, last = out_of_range[0]
        label = f"{first}" if first == last else f"{first}-{last}"
        raise _ExportRequestError(
            "invalid_parameter",
            f"pages: page selection {label} is outside the document, which has "
            f"{page_count} page(s).",
            f"Choose pages between 1 and {page_count}.",
        )
    return sorted({page for first, last in ranges for page in range(first, last + 1)})


def _archive_with_pdf(archive: bytes, convert) -> bytes:
    """A document archive with ``convert(pdf_bytes)`` applied to its PDF."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(archive)) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as dst,
    ):
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith(".pdf"):
                data = convert(data)
            dst.writestr(info, data)
    return out.getvalue()


def _archive_without_marks(archive: bytes) -> bytes:
    """A document archive with its PDF restored to the version before marks."""
    return _archive_with_pdf(archive, original_pdf)


def _archive_for_export(archive: bytes) -> bytes:
    """A document archive with marks in the desktop apps' colors (see
    ``pdf_marks.for_export``)."""
    return _archive_with_pdf(archive, for_export)


def _archive_page_map(archive: bytes) -> list:
    """The tablet page -> PDF page map of a document archive, or []."""
    from remarkable_mcp.pages import pdf_page_map

    if _is_pdf_payload(archive):
        return []
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            name = next((n for n in zf.namelist() if n.endswith(".content")), None)
            return pdf_page_map(json.loads(zf.read(name))) if name else []
    except Exception:
        return []


def _outline_summaries(entries: list, archive: bytes) -> list:
    """A PDF's outline entries with the tablet page each points to."""

    page_map = _archive_page_map(archive)
    out = []
    for entry in entries:
        index = entry["pdf_page"]
        if index is None:
            page = None
        else:
            page = tablet_page(index, page_map) if page_map else index + 1
        out.append({"level": entry["level"], "title": entry["title"], "page": page})
    return out


def _mark_summaries(marks: list, archive: bytes) -> list:
    """Short descriptions of a PDF's marks, with the tablet page of each."""
    from remarkable_mcp.pages import pdf_page_map, tablet_page
    from remarkable_mcp.pdf_marks import summarize

    content = None
    if not _is_pdf_payload(archive):
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                name = next((n for n in zf.namelist() if n.endswith(".content")), None)
                if name:
                    content = json.loads(zf.read(name))
        except Exception:
            content = None
    page_map = pdf_page_map(content) if content else []
    states = _mark_ink_states(marks, archive, content, page_map)
    summaries = []
    for mark in marks:
        page = tablet_page(mark["pdf_page"], page_map) if page_map else mark["pdf_page"] + 1
        summaries.append(summarize(mark, page, states.get(mark["id"])))
    return summaries


def _mark_ink_states(marks: list, archive: bytes, content, page_map: list) -> dict:
    """The state on the tablet of each mark's ink, read from the archive's ink."""
    from remarkable_mcp.mark_ink import has_ink, mark_states
    from remarkable_mcp.pages import content_page_ids

    if not content or not has_ink(marks):
        return {}
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            ink = {
                name.rsplit("/", 1)[-1][: -len(".rm")]: zf.read(name)
                for name in zf.namelist()
                if name.endswith(".rm")
            }
        states, _ = mark_states(
            marks,
            page_ids=content_page_ids(content),
            page_map=page_map,
            read_ink=ink.get,
        )
        return states
    except Exception:
        return {}


def _mark_line(summary: dict) -> str:
    where = f"page {summary['page']}" if summary["page"] else "page no longer shown"
    kind = summary.get("shape") or summary["kind"]
    details = ", ".join(v for v in (where, summary.get("color"), kind) if v)
    line = f"{summary['id']} ({details})"
    content = summary.get("text") or summary.get("latex")
    if content:
        line += f": {content}"
    style = [
        key if value is True else f"{key} {value}"
        for key, value in (summary.get("style") or {}).items()
        if key != "at"
    ]
    if style:
        line += f" [{', '.join(style)}]"
    if summary.get("state"):
        line += f" [{summary['state']}]"
    cover = summary.get("cover")
    if cover:
        state = f": {cover['state']}" if cover.get("state") else ""
        line += f" [cover '{cover['layer']}'{state}]"
    if summary.get("description"):
        line += f" [description: {summary['description']}]"
    return line


def _ocr_reliability_note(handwriting, backend, document: str) -> Optional[str]:
    """For Tesseract OCR of handwriting: a note that the text is approximate,
    naming the pages with low-confidence lines to view first."""
    if backend != "tesseract" or not handwriting:
        return None
    doubtful = [
        entry["page"]
        for entry in handwriting
        if any(line.get("low_confidence") for line in entry.get("lines") or [])
    ]
    note = (
        "Handwriting OCR is from Tesseract and only approximate; view a page with "
        f"remarkable_image('{document}', page=N) before relying on its text"
    )
    if doubtful:
        pages = ", ".join(str(p) for p in doubtful)
        note += f" (low-confidence lines on page{'s' if len(doubtful) > 1 else ''} {pages})"
    return note + "."


class _ExportRequestError(Exception):
    def __init__(self, error_type: str, message: str, suggestion: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.suggestion = suggestion


def _extract_source_text_from_bytes(data: bytes, source_type: str) -> str:
    if source_type == "pdf":
        data = original_pdf(data)
    suffix = f".{source_type}"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    try:
        if source_type == "pdf":
            return extract_text_from_pdf(path)
        if source_type == "epub":
            return extract_text_from_epub(path)
        raise ValueError(f"Unsupported source type: {source_type}")
    finally:
        path.unlink(missing_ok=True)


def _publish_document_export(
    client,
    target_doc,
    metadata: ExportMetadata,
    *,
    output_format: Literal["pdf", "markdown"],
    pdf_mode: PdfMode,
    include_ocr: bool,
    background_color: str,
    page_ranges: Optional[List[tuple]] = None,
) -> PublishedExport[_DocumentExportResult]:
    """Download, build, and publish one export without touching tablet state.

    ``page_ranges`` (PDF only) limits the export to those physical pages.
    """

    def selection(page_count: Optional[int]) -> Optional[List[int]]:
        if page_ranges is None or page_count is None:
            return None
        return _select_pages(page_ranges, page_count)

    def writer(destination: Path) -> _DocumentExportResult:
        raw_doc = client.download(target_doc)
        if not raw_doc:
            raise _ExportRequestError(
                "document_download_failed",
                "The transport returned an empty document payload.",
                "Retry the export or use remarkable_status() to check the connection.",
            )

        if _is_pdf_payload(raw_doc):
            page_count = _count_pdf_pages(raw_doc)
            resolved = replace(metadata, page_count=page_count)
            if output_format == "pdf":
                if pdf_mode == "annotations":
                    raise _ExportRequestError(
                        "annotation_only_not_available",
                        (
                            "Annotation-only PDF export is unavailable because this "
                            "transport returned a flattened native PDF."
                        ),
                        (
                            "Use pdf_mode='merged', or connect through cloud/SSH, local-dir, "
                            "or newer USB firmware that exposes an rmdoc archive."
                        ),
                    )
                build = write_native_pdf_export(
                    raw_doc, destination, resolved, pages=selection(page_count)
                )
                return _DocumentExportResult(
                    build=build,
                    metadata=resolved,
                    representation="tablet_pdf",
                    pdf_mode=pdf_mode,
                )

            source_text = _extract_source_text_from_bytes(raw_doc, "pdf")
            warnings = [
                (
                    "This transport returned a flattened native PDF. Source/visible text "
                    "was preserved, but separate typed, annotation, highlight, and OCR "
                    "layers were unavailable."
                )
            ]
            build = write_markdown_export(
                destination,
                resolved,
                source_text=source_text,
                extraction=None,
                include_ocr=include_ocr,
                warnings=warnings,
            )
            return _DocumentExportResult(
                build=build,
                metadata=resolved,
                representation="tablet_pdf",
            )

        if output_format == "pdf":
            # Marks match the tablet's screen in the document; exports match
            # the desktop and browser apps instead.
            raw_doc = _archive_for_export(raw_doc)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_doc)
            archive_path = Path(tmp.name)

        try:
            try:
                page_count = get_document_page_count(archive_path)
                archive_type = get_document_file_type(archive_path) or metadata.source_type
            except Exception as exc:
                raise _ExportRequestError(
                    "invalid_document_archive",
                    f"The downloaded document archive could not be read: {exc}",
                    "Retry the export. If it persists, try another transport.",
                ) from exc

            resolved = replace(
                metadata,
                source_type=archive_type or "notebook",
                page_count=page_count if page_count > 0 else None,
            )

            if output_format == "pdf":
                # USB web can expose a firmware-rendered PDF for notebooks and
                # EPUBs. Prefer it in merged mode because it includes source
                # underlays that the local archive cannot reconstruct.
                if pdf_mode == "merged" and resolved.source_type != "pdf":
                    native_pdf = download_raw_file(client, target_doc, "pdf")
                    if _is_pdf_payload(native_pdf):
                        native_pages = _count_pdf_pages(native_pdf)
                        native_metadata = replace(resolved, page_count=native_pages)
                        build = write_native_pdf_export(
                            native_pdf,
                            destination,
                            native_metadata,
                            pages=selection(native_pages),
                        )
                        return _DocumentExportResult(
                            build=build,
                            metadata=native_metadata,
                            representation="tablet_pdf",
                            pdf_mode=pdf_mode,
                        )

                if resolved.page_count is None and resolved.source_type == "pdf":
                    source_pdf = download_raw_file(client, target_doc, "pdf")
                    if _is_pdf_payload(source_pdf):
                        resolved = replace(resolved, page_count=_count_pdf_pages(source_pdf))

                if resolved.page_count is None:
                    raise _ExportRequestError(
                        "page_count_unavailable",
                        "The physical page count could not be determined for this document.",
                        (
                            "Retry with another transport. PDF export cannot preserve page "
                            "order safely without a physical page count."
                        ),
                    )

                build = write_archive_pdf_export(
                    archive_path,
                    destination,
                    resolved,
                    pdf_mode=pdf_mode,
                    background_color=background_color,
                    pages=selection(resolved.page_count),
                )
                return _DocumentExportResult(
                    build=build,
                    metadata=resolved,
                    representation="document_archive",
                    pdf_mode=pdf_mode,
                )

            warnings: list[str] = []
            extraction = None
            try:
                extraction = extract_text_from_document_zip(
                    archive_path,
                    include_ocr=include_ocr,
                    doc_id=resolved.document_id,
                    version=document_version(target_doc),
                )
                if resolved.page_count is None:
                    extracted_pages = int(extraction.get("pages") or 0)
                    if extracted_pages > 0:
                        resolved = replace(resolved, page_count=extracted_pages)
            except Exception as exc:
                warnings.append(f"Annotation extraction failed: {exc}")

            source_text = None
            if resolved.source_type in ("pdf", "epub"):
                source_data = download_raw_file(client, target_doc, resolved.source_type)
                if source_data:
                    try:
                        source_text = _extract_source_text_from_bytes(
                            source_data,
                            resolved.source_type,
                        )
                    except Exception as exc:
                        warnings.append(
                            f"{resolved.source_type.upper()} source text extraction failed: {exc}"
                        )
                else:
                    warnings.append(
                        f"Original {resolved.source_type.upper()} source data was unavailable."
                    )

            if extraction is None and source_text is None:
                raise _ExportRequestError(
                    "no_exportable_content",
                    "Neither source text nor annotation content could be extracted.",
                    "Retry with another transport or verify the document opens on the tablet.",
                )

            build = write_markdown_export(
                destination,
                resolved,
                source_text=source_text,
                extraction=extraction,
                include_ocr=include_ocr,
                warnings=warnings,
            )
            return _DocumentExportResult(
                build=build,
                metadata=resolved,
                representation="document_archive",
            )
        finally:
            archive_path.unlink(missing_ok=True)

    extension = "pdf" if output_format == "pdf" else "md"
    source_name = metadata.title
    for source_suffix in (".pdf", ".epub"):
        if source_name.lower().endswith(source_suffix):
            source_name = source_name[: -len(source_suffix)]
            break
    return export_store.publish(
        filename=f"{source_name}.{extension}",
        output_format=output_format,
        writer=writer,
    )


MAX_BATCH_EXPORTS = 200


def _export_targets(collection, items_by_id: dict, folder: Optional[str], tags):
    """``(base_path, documents)`` for a batch export: the documents in
    ``folder`` (the library when None) and its subfolders, keeping those with
    any of ``tags``; raises _ExportRequestError if the folder is not found."""
    root = _get_root_path()
    base = _resolve_root_path(folder) if folder and folder.startswith("/") else folder
    base = (base or root).rstrip("/") or "/"
    if base != "/":
        found = any(
            item.is_folder
            and not _is_cloud_archived(item, items_by_id)
            and get_item_path(item, items_by_id).lower() == base.lower()
            for item in collection
        )
        if not found or not _is_within_root(base, root):
            raise _ExportRequestError(
                "folder_not_found",
                f"Folder not found: '{folder}'",
                "Use remarkable_browse() to find the folder path.",
            )
    wanted = {str(tag).lower() for tag in tags or []}
    prefix = "" if base == "/" else base.lower() + "/"
    documents = []
    for item in collection:
        if item.is_folder or _is_cloud_archived(item, items_by_id):
            continue
        path = get_item_path(item, items_by_id)
        if not _is_within_root(path, root) or not path.lower().startswith(prefix or "/"):
            continue
        if wanted and not wanted & {t.lower() for t in getattr(item, "tags", None) or []}:
            continue
        documents.append((path, item))
    documents.sort(key=lambda entry: entry[0].lower())
    return base, documents


def _export_many(
    client,
    collection,
    items_by_id: dict,
    documents: list,
    base: str,
    save_dir: Path,
    *,
    output_format: str,
    pdf_mode: str,
    include_ocr: bool,
    overwrite: bool,
) -> dict:
    """Export each document into ``save_dir``, mirroring its folders under
    ``base``; one failure does not stop the others."""
    suffix = export_save.SUFFIXES[output_format]
    background = get_background_color()
    planned: dict = {}
    saved, failed = [], []
    for path, item in documents:
        relative = path[len(base) :].strip("/") if base != "/" else path.strip("/")
        parts = [export_save.safe_file_name(part) for part in relative.split("/")]
        target = save_dir.joinpath(*parts[:-1], parts[-1] + suffix)
        if str(target).lower() in planned:
            target = target.with_name(f"{target.stem} ({item.ID[:8]}){suffix}")
        planned[str(target).lower()] = item.ID
        display_path = _apply_root_filter(path)
        try:
            checked = export_save.check_path(str(target), output_format, overwrite)
            if overwrite:
                export_save.check_replace(checked, item.ID)
            metadata = ExportMetadata(
                document_id=item.ID,
                title=item.VissibleName,
                path=display_path,
                source_type=get_file_type(client, item),
                page_count=None,
                modified=getattr(item, "ModifiedClient", None),
                tags=tuple(getattr(item, "tags", None) or ()),
            )
            published = _publish_document_export(
                client,
                item,
                metadata,
                output_format=output_format,
                pdf_mode=pdf_mode,
                include_ocr=include_ocr,
                background_color=background,
            )
            data = export_store.read_bytes(published.resource.export_id, output_format)
            result = export_save.save(data, checked, overwrite, item.ID)
            build = published.result.build
            saved.append(
                {
                    "document": item.VissibleName,
                    "path": display_path,
                    **result,
                    "pages": build.pages,
                    "status": build.status,
                    "warnings": list(build.warnings),
                }
            )
        except (export_save.SaveError, _ExportRequestError) as exc:
            failed.append(
                {
                    "document": item.VissibleName,
                    "path": display_path,
                    "error": exc.error_type,
                    "message": exc.message,
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "document": item.VissibleName,
                    "path": display_path,
                    "error": "export_failed",
                    "message": str(exc),
                }
            )
    return {"saved": saved, "failed": failed}


async def _saved_export_response(published, output_format, save_path, overwrite, page_ranges):
    """Save a published export at ``save_path`` and describe the saved copy."""
    resource, result = published.resource, published.result
    build = result.build
    try:
        data = export_store.read_bytes(resource.export_id, output_format)
        saved = await run_blocking(
            export_save.save, data, save_path, overwrite, result.metadata.document_id
        )
    except export_save.SaveError as exc:
        return make_error(error_type=exc.error_type, message=exc.message, suggestion=exc.suggestion)
    except OSError as exc:
        return make_error(
            error_type="save_failed",
            message=f"Could not save the export: {exc}",
            suggestion="Check the folder exists and is writable, then try again.",
        )
    response = {
        "document": result.metadata.title,
        "document_id": result.metadata.document_id,
        "path": result.metadata.path,
        "source_type": result.metadata.source_type,
        "format": output_format,
        **saved,
        "pages": build.pages,
        "status": build.status,
        "failed_pages": list(build.failed_pages),
        "warnings": list(build.warnings),
        "representation": result.representation,
    }
    if result.pdf_mode is not None:
        response["pdf_mode"] = result.pdf_mode
    if page_ranges is not None:
        response["selected_pages"] = _select_pages(page_ranges, result.metadata.page_count)
        response["document_pages"] = result.metadata.page_count
    if build.ocr_backend:
        response["ocr_backend"] = build.ocr_backend
    hint = (
        f"Saved the {output_format.upper()} export to {saved['saved_path']} "
        f"({saved['size']} bytes, SHA-256 {saved['sha256']}). The tablet was not modified."
    )
    if build.status == "partial":
        hint = f"Partial export: {'; '.join(build.warnings) or 'some pages failed'}. {hint}"
    return make_response(response, hint)


async def _remarkable_export_many(
    document, output_format, pdf_mode, include_ocr, pages, save_to, overwrite, folder, tags
):
    """remarkable_export for a folder and/or tags."""

    if document is not None:
        return invalid_param(
            "document: give document, or folder and/or tags, not both.",
            "Omit document to export a folder or tagged documents.",
        )
    if pages is not None:
        return invalid_param("pages applies to one document only.", "Omit pages.")
    if output_format == "markdown" and pdf_mode != "merged":
        return invalid_param("pdf_mode applies only to PDF exports.", "Use pdf_mode='merged'.")
    if output_format == "pdf" and include_ocr:
        return invalid_param(
            "include_ocr applies only to Markdown exports.", "Set include_ocr=False."
        )
    if tags is not None and (
        not isinstance(tags, list) or not tags or not all(isinstance(t, str) for t in tags)
    ):
        return invalid_param(
            "tags must be a non-empty list of tag names.", 'Example: tags=["Graded"]'
        )
    if save_to is None:
        return invalid_param(
            "save_to: exporting several documents needs a folder to save them in.",
            "Pass save_to as a folder inside REMARKABLE_EXPORT_DIRS.",
        )
    try:
        save_dir = export_save.check_dir(save_to)
        client = await run_blocking(get_rmapi)
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)
        base, documents = _export_targets(collection, items_by_id, folder, tags)
    except (_ExportRequestError, export_save.SaveError) as exc:
        return make_error(error_type=exc.error_type, message=exc.message, suggestion=exc.suggestion)
    if not documents:
        return make_response(
            {"saved": [], "failed": [], "count": 0, "save_to": str(save_dir)},
            "No documents matched; nothing was exported.",
        )
    if len(documents) > MAX_BATCH_EXPORTS:
        return invalid_param(
            f"folder/tags: {len(documents)} documents match; at most {MAX_BATCH_EXPORTS}"
            " are exported at once.",
            "Narrow the export with a subfolder or tags.",
        )
    result = await run_blocking(
        _export_many,
        client,
        collection,
        items_by_id,
        documents,
        base,
        save_dir,
        output_format=output_format,
        pdf_mode=pdf_mode,
        include_ocr=include_ocr,
        overwrite=overwrite,
    )
    data = {
        "saved": result["saved"],
        "failed": result["failed"],
        "count": len(documents),
        "save_to": str(save_dir),
        "format": output_format,
    }
    hint = f"Saved {len(result['saved'])} of {len(documents)} documents to {save_dir}."
    if result["failed"]:
        hint += f" {len(result['failed'])} failed; see failed."
    return make_response(data, hint + " The tablet was not modified.")


@mcp.tool(annotations=EXPORT_ANNOTATIONS)
async def remarkable_export(
    document: Optional[str] = None,
    output_format: Literal["pdf", "markdown"] = "pdf",
    pdf_mode: Literal["merged", "annotations"] = "merged",
    include_ocr: bool = False,
    pages: Optional[int | str] = None,
    save_to: Optional[str] = None,
    overwrite: bool = False,
    folder: Optional[str] = None,
    tags: Optional[List[str]] = None,
):
    """
    <usecase>Export reMarkable documents as PDF or Markdown files: one document (or
    some of its pages), or every document in a folder or with given tags.</usecase>
    <instructions>
    Exports without modifying the tablet or its library.

    The generated file is written to a server-managed temporary directory and
    returned as an MCP ResourceLink. The tool response stays small: it never embeds
    the PDF/Markdown as base64 and never writes to an arbitrary host path. The
    resource expires after 15 minutes and may be evicted earlier when more than
    eight exports are retained. Fetch and save the linked resource for durable use.

    With `save_to`, the file is instead saved at that path on this machine and
    the response gives its path, size, page count and SHA-256 hash to check
    the copy. Only paths inside the folders listed in REMARKABLE_EXPORT_DIRS
    are accepted. Missing folders are created; the file is written under a
    temporary name and renamed into place. An existing file is replaced only
    with `overwrite=True`, and only when it is an earlier export of the same
    document. Nothing is ever deleted.

    To export many documents, give `folder` (its documents and those in its
    subfolders) and/or `tags` (documents with any of them; the whole library
    when no folder is given) instead of `document`, with `save_to` a folder
    inside REMARKABLE_EXPORT_DIRS. Subfolders are recreated under it and
    files are named after the documents. Each document is exported on its
    own: the response lists every saved file (path, size, page count,
    SHA-256) and every failure, such as a file that already exists.

    PDF behavior:
    - `pdf_mode="merged"` (default) preserves complete physical pages, combining
      mapped PDF underlays and reMarkable annotations.
    - `pdf_mode="annotations"` exports full-page annotation layers only when the
      transport provides a document archive.
    - Pages remain in physical device order. A render failure produces a labeled
      placeholder at that ordinal and a partial-export warning; pages are not skipped.
    - `pages` exports only the selected physical pages (same numbering as
      remarkable_image), in document order.

    Markdown behavior:
    - Preserves basic source metadata and fixed source text, typed text, annotation,
      highlight, and OCR sections.
    - OCR is opt-in and uses the same configured/cached OCR path as remarkable_read.
    - Extracted text is kept verbatim; the exporter does not invent headings, lists,
      links, or page attribution it cannot prove.
    </instructions>
    <parameters>
    - document: Document name or path (use remarkable_browse to find documents).
    - output_format: "pdf" (default) or "markdown".
    - pdf_mode: PDF-only mode, "merged" (default) or "annotations".
    - include_ocr: Enable existing handwriting OCR for Markdown (default: False).
    - pages: PDF only. One page (5) or ranges ("5-9,12"); default: every page.
    - save_to: Absolute path of a .pdf or .md file to save the export at,
      inside a folder listed in REMARKABLE_EXPORT_DIRS.
    - overwrite: Replace an existing file at save_to that is an earlier export
      of the same document (default: False).
    - folder: Export every document in this folder and its subfolders
      (needs save_to as a folder).
    - tags: Export documents with any of these tags (needs save_to as a
      folder); combined with folder, only those inside it.
    </parameters>
    <examples>
    - remarkable_export("Meeting Notes")
    - remarkable_export("Research Paper", output_format="pdf", pdf_mode="annotations")
    - remarkable_export("Journal", output_format="markdown", include_ocr=True)
    - remarkable_export("Lecture Notes", pages="3-5,9")
    - remarkable_export("Week 3 worksheet",
        save_to="/Users/me/Projects/Course/Worksheets/Week 3 - done.pdf")
    - remarkable_export(folder="/Classes/Physics/Worksheets", tags=["Graded"],
        save_to="/Users/me/Projects/Course/Graded worksheets")
    </examples>
    """
    if folder is not None or tags is not None:
        return await _remarkable_export_many(
            document, output_format, pdf_mode, include_ocr, pages, save_to, overwrite, folder, tags
        )
    if not isinstance(document, str) or not document.strip():
        return invalid_param(
            "document: give a document, or folder and/or tags to export several.",
            "Example: remarkable_export('Meeting Notes').",
        )
    if output_format == "markdown" and pdf_mode != "merged":
        return invalid_param(
            "pdf_mode applies only to PDF exports.",
            "Use pdf_mode='merged' for Markdown, or choose output_format='pdf'.",
        )
    if output_format == "pdf" and include_ocr:
        return invalid_param(
            "include_ocr applies only to Markdown exports.",
            "Set include_ocr=False, or choose output_format='markdown'.",
        )
    page_ranges = None
    if pages is not None:
        if output_format != "pdf":
            return invalid_param(
                "pages applies only to PDF exports.",
                "Omit pages for Markdown, or choose output_format='pdf'.",
            )
        try:
            page_ranges = _parse_page_spec(pages)
        except ValueError as exc:
            return invalid_param(
                f"pages: {exc}",
                "Examples: pages=5, pages='5-9', pages='1,3,10-12'.",
            )

    save_path = None
    if overwrite and save_to is None:
        return invalid_param(
            "overwrite applies only with save_to.",
            "Pass save_to with the path to save at, or omit overwrite.",
        )
    if save_to is not None:
        try:
            save_path = export_save.check_path(save_to, output_format, overwrite)
        except export_save.SaveError as exc:
            return make_error(
                error_type=exc.error_type, message=exc.message, suggestion=exc.suggestion
            )

    try:
        client = await run_blocking(get_rmapi)
        collection = await run_blocking(client.get_meta_items)
        items_by_id = get_items_by_id(collection)
        target_doc, error = lookup(
            client, document, kind="document", collection=collection, items_by_id=items_by_id
        )
        if error:
            return error

        if save_path is not None and overwrite:
            await run_blocking(export_save.check_replace, save_path, target_doc.ID)
        full_path = get_item_path(target_doc, items_by_id)
        display_path = _apply_root_filter(full_path)
        source_type = await run_blocking(get_file_type, client, target_doc)
        metadata = ExportMetadata(
            document_id=target_doc.ID,
            title=target_doc.VissibleName,
            path=display_path,
            source_type=source_type,
            page_count=None,
            modified=getattr(target_doc, "ModifiedClient", None),
            tags=tuple(_item_tags(target_doc)),
        )
        background = await run_blocking(get_background_color)
        published = await run_blocking(
            _publish_document_export,
            client,
            target_doc,
            metadata,
            output_format=output_format,
            pdf_mode=pdf_mode,
            include_ocr=include_ocr,
            background_color=background,
            page_ranges=page_ranges,
        )
    except (_ExportRequestError, export_save.SaveError) as exc:
        return make_error(
            error_type=exc.error_type,
            message=exc.message,
            suggestion=exc.suggestion,
        )
    except Exception as exc:
        return make_error(
            error_type="export_failed",
            message=str(exc),
            suggestion=(
                "Retry the export, or use remarkable_status() to verify the active transport."
            ),
        )

    resource = published.resource
    result = published.result
    build = result.build
    if save_path is not None:
        return await _saved_export_response(
            published, output_format, save_path, overwrite, page_ranges
        )
    response = {
        "document": result.metadata.title,
        "document_id": result.metadata.document_id,
        "path": result.metadata.path,
        "source_type": result.metadata.source_type,
        "format": output_format,
        "mime_type": resource.mime_type,
        "filename": resource.filename,
        "size": resource.size,
        "pages": build.pages,
        "status": build.status,
        "failed_pages": list(build.failed_pages),
        "warnings": list(build.warnings),
        "representation": result.representation,
        "resource_uri": resource.uri,
        "expires_at": resource.expires_at,
        "temporary_local_file": True,
    }
    if result.pdf_mode is not None:
        response["pdf_mode"] = result.pdf_mode
    if page_ranges is not None:
        response["selected_pages"] = _select_pages(page_ranges, result.metadata.page_count)
        response["document_pages"] = result.metadata.page_count
    if build.ocr_backend:
        response["ocr_backend"] = build.ocr_backend

    hint = (
        f"Temporary {output_format.upper()} export ready as an MCP resource "
        f"({resource.size} bytes). It expires at {resource.expires_at.isoformat()}; "
        "fetch and save the resource for durable storage. The tablet was not modified."
    )
    if build.status == "partial":
        hint = f"Partial export: {'; '.join(build.warnings) or 'some pages failed'}. {hint}"

    info = TextContent(type="text", text=make_response(response, hint))
    link = ResourceLink(
        type="resource_link",
        name=resource.filename,
        title=f"{result.metadata.title} ({output_format.upper()} export)",
        uri=resource.uri,
        description=(
            f"Temporary export of reMarkable document {result.metadata.document_id}; "
            f"expires {resource.expires_at.isoformat()}."
        ),
        mime_type=resource.mime_type,
        size=resource.size,
        meta={
            "documentId": result.metadata.document_id,
            "documentPath": result.metadata.path,
            "expiresAt": resource.expires_at.isoformat(),
            "temporaryLocalFile": True,
        },
    )
    return [info, link]

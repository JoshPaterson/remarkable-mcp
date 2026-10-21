"""Where things are on a tablet page: text lines, ink, marks and free space.

Used to place marks (remarkable_mark_pdf) and to find the user's writing on a
page (remarkable_page_layout). Regions are page fractions
``[left, top, right, bottom]``, the frame remarkable_image's ``region`` uses.
"""

import json
from pathlib import Path
from typing import List, Optional

from remarkable_mcp.pages import content_page_ids


def page_layers(root: Path, page: int) -> list:
    """A tablet page's layers, bottom first: ``{"name", "visible",
    "selected"}``; empty when the page has no ink yet (Layer 1 is made with
    the first ink). ``root`` holds the document's files."""
    from remarkable_mcp import rm_ink

    content_file = next(iter(sorted(root.rglob("*.content"))), None)
    if content_file is None:
        return []
    page_ids = content_page_ids(json.loads(content_file.read_text()))
    if not 1 <= page <= len(page_ids):
        return []
    rm = next(iter(sorted(root.rglob(f"{page_ids[page - 1]}.rm"))), None)
    if rm is None:
        return []
    data = rm.read_bytes()
    layers = rm_ink.page_layers(data)
    selected = rm_ink.selected_layer(data)
    if selected is None and layers:
        selected = layers[0]["id"]
    return [
        {"name": layer["name"], "visible": layer["visible"], "selected": layer["id"] == selected}
        for layer in layers
    ]


def page_layout(root: Path, pdf: Optional[bytes], page: int, page_map: List) -> dict:
    """A tablet page's text lines (from the PDF before marks), the regions of
    the user's ink and of the marks, the largest free regions and the layers.

    ``root`` holds the document's files, ``pdf`` is its PDF with marks (None
    for a notebook) and ``page_map`` maps tablet pages to PDF pages. Raises
    ``MarkError`` for a page the document does not have.
    """
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
    ink = page_ink_regions(root, page)
    lines, marks = [], []
    if pdf is not None:
        if pdf_page is not None:
            lines = text_lines(original_pdf(pdf), pdf_page)
        marks = [m["box"] for m in stored_marks(pdf) if m.get("pdf_page") == pdf_page]
    return {
        "page": page,
        "text_lines": lines,
        "ink": ink,
        "free": free_regions(ink + [line["region"] for line in lines] + marks),
        "layers": page_layers(root, page),
    }

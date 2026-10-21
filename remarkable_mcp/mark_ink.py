"""The tablet ink of marks: covers, text highlights and erasable marks.

Each kind of mark ink is stored in the mark under its own key, with the ids
of its strokes or items once written (``"pending"`` until then). This module
writes the ink of an edit's new marks and deletes that of removed ones, one
page at a time, and reads back each mark's state on the tablet.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from remarkable_mcp import cover_layers, ink_marks, rm_ink, text_highlights
from remarkable_mcp.pages import tablet_page


@dataclasses.dataclass(frozen=True)
class InkKind:
    """One kind of mark ink: the mark's key for it, the key of its ids, how
    a pending one is written and how its state is read."""

    key: str
    ids: str
    add: Callable[[rm_ink.PageWriter, Dict[str, Any], Tuple[float, float]], None]
    status: Callable[[Optional[bytes], Sequence[Sequence[int]], Dict[str, Any]], str]


KINDS = (
    InkKind(
        "cover",
        "strokes",
        cover_layers.add_mark_cover,
        lambda data, ids, _mark: cover_layers.cover_status(data, ids),
    ),
    InkKind(
        "highlight", "items", text_highlights.add_mark_highlight, text_highlights.highlight_status
    ),
    InkKind(
        "ink",
        "items",
        ink_marks.add_mark_ink,
        lambda data, ids, _mark: ink_marks.ink_status(data, ids),
    ),
)


def has_ink(marks: Iterable[Dict[str, Any]]) -> bool:
    """Whether any of ``marks`` has tablet ink, written or pending."""
    return any(isinstance(m.get(kind.key), dict) for m in marks for kind in KINDS)


def _page_ids(marks: Iterable[Dict[str, Any]], pdf_page: int) -> Tuple[list, list]:
    """``(cover ids, ink ids)`` stored in the marks on ``pdf_page``."""
    covers: list = []
    inks: list = []
    for mark in marks:
        if mark["pdf_page"] == pdf_page:
            for kind, ids in _stored_ids(mark):
                (covers if kind.key == "cover" else inks).extend(ids)
    return covers, inks


def _stored_ids(mark: Dict[str, Any]) -> List[Tuple[InkKind, list]]:
    out = []
    for kind in KINDS:
        entry = mark.get(kind.key)
        if isinstance(entry, dict) and entry.get(kind.ids):
            out.append((kind, entry[kind.ids]))
    return out


def write_marks(
    before: Sequence[Dict[str, Any]],
    after: Sequence[Dict[str, Any]],
    removed: Iterable[str],
    *,
    page_ids: Sequence[str],
    page_map: Sequence[Optional[int]],
    sizes: Sequence[Tuple[float, float]],
    read_ink,
    author: uuid.UUID,
) -> Dict[str, bytes]:
    """Write the ink of new marks and delete that of removed ones.

    ``before`` and ``after`` are the mark lists around an edit; marks in
    ``after`` with pending ink get their ids. ``read_ink(page_id)`` returns a
    page's ``.rm`` file or None. Each page is read once: the removed marks'
    ink is deleted, then the new marks' ink added. Returns the changed files
    by page id.
    """
    removed = set(removed)
    jobs: Dict[int, Dict[str, list]] = {}
    for mark in before:
        if mark["id"] in removed:
            for _, ids in _stored_ids(mark):
                jobs.setdefault(mark["pdf_page"], {"remove": [], "add": []})["remove"] += ids
    for kind in KINDS:
        for mark in after:
            entry = mark.get(kind.key)
            if isinstance(entry, dict) and entry.get("pending"):
                job = jobs.setdefault(mark["pdf_page"], {"remove": [], "add": []})
                job["add"].append((kind, mark))
    changed: Dict[str, bytes] = {}
    for pdf_page, job in jobs.items():
        page = tablet_page(pdf_page, page_map)
        if page is None or page > len(page_ids):
            continue
        page_id = page_ids[page - 1]
        data = read_ink(page_id)
        if data is None and not job["add"]:
            continue
        if data is not None and not rm_ink.is_v6(data):
            raise rm_ink.InkError(f"Page {page}'s ink is in an older format.")
        writer = rm_ink.PageWriter(data, author, *_page_ids([*before, *after], pdf_page))
        writer.tombstone(job["remove"])
        for kind, mark in job["add"]:
            kind.add(writer, mark, sizes[pdf_page])
        if writer.data != data:
            changed[page_id] = writer.data
    return changed


def mark_states(
    marks: Sequence[Dict[str, Any]],
    *,
    page_ids: Sequence[str],
    page_map: Sequence[Optional[int]],
    read_ink,
) -> Tuple[Dict[str, str], List[str]]:
    """``(states, notes)``: each mark's ink state on the tablet (see each
    kind's status), and notes about the user's strokes on cover layers."""
    states: Dict[str, str] = {}
    pages: Dict[int, Optional[bytes]] = {}
    covered: Dict[int, int] = {}
    for mark in marks:
        stored = _stored_ids(mark)
        if not stored:
            continue
        page = tablet_page(mark["pdf_page"], page_map)
        if page is None or page > len(page_ids):
            continue
        if page not in pages:
            pages[page] = read_ink(page_ids[page - 1])
        for kind, ids in stored:
            states[mark["id"]] = kind.status(pages[page], ids, mark)
            if kind.key == "cover":
                covered[page] = mark["pdf_page"]
    notes = []
    author = rm_ink.known_author_uuid()
    for page, pdf_page in sorted(covered.items()):
        count = cover_layers.foreign_strokes_on_covers(
            pages[page], author, *_page_ids(marks, pdf_page)
        )
        if count:
            notes.append(
                f"Page {page}: {count} of the user's strokes are on a cover layer and "
                "hide with the marks while it is shown."
            )
    return states, notes

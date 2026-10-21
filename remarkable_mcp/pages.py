"""Page order from a document's ``.content`` file.

formatVersion 2 lists pages as ``cPages.pages[]`` entries. A page deleted on
the tablet stays in that list with a ``deleted`` field (e.g.
``{"timestamp": "1:2", "value": 1}``) and is not shown. formatVersion 1 lists
page ids in a flat ``pages`` list.
"""

from typing import Any, Dict, List, Optional, Sequence, Set


def _is_deleted(entry: Dict[str, Any]) -> bool:
    deleted = entry.get("deleted")
    if isinstance(deleted, dict):
        return bool(deleted.get("value", 1))
    return bool(deleted)


def live_cpages_entries(content: Any) -> List[Dict[str, Any]]:
    """``cPages.pages`` entries the tablet shows, in order (deleted ones skipped)."""
    if not isinstance(content, dict):
        return []
    cpages = content.get("cPages")
    if not isinstance(cpages, dict) or not isinstance(cpages.get("pages"), list):
        return []
    return [p for p in cpages["pages"] if isinstance(p, dict) and not _is_deleted(p)]


def has_cpages(content: Any) -> bool:
    """True if ``content`` uses the formatVersion 2 ``cPages`` page list."""
    return (
        isinstance(content, dict)
        and isinstance(content.get("cPages"), dict)
        and isinstance(content["cPages"].get("pages"), list)
    )


def content_page_ids(content: Any) -> List[str]:
    """Ids of the pages the tablet shows, in order."""
    if has_cpages(content):
        return [p["id"] for p in live_cpages_entries(content) if p.get("id")]
    if isinstance(content, dict) and isinstance(content.get("pages"), list):
        return [p for p in content["pages"] if isinstance(p, str)]
    return []


def deleted_page_ids(content: Any) -> Set[str]:
    """Ids of pages deleted on the tablet (their ``.rm`` files may remain)."""
    if not has_cpages(content):
        return set()
    return {
        p["id"]
        for p in content["cPages"]["pages"]
        if isinstance(p, dict) and p.get("id") and _is_deleted(p)
    }


def pdf_page_map(content: Any) -> List[Optional[int]]:
    """0-based PDF page index of each page the tablet shows, in order.

    ``None`` marks a page added on the tablet, which has no PDF page behind
    it. formatVersion 2 uses ``cPages.pages[].redir``; formatVersion 1 uses
    ``redirectionPageMap`` (-1 for added pages), or PDF order when that is
    missing.
    """
    if has_cpages(content):
        result: List[Optional[int]] = []
        for entry in live_cpages_entries(content):
            redir = entry.get("redir")
            value = redir.get("value") if isinstance(redir, dict) else None
            result.append(value if isinstance(value, int) and value >= 0 else None)
        return result
    if not isinstance(content, dict):
        return []
    rpm = content.get("redirectionPageMap")
    if isinstance(rpm, list) and rpm:
        return [v if isinstance(v, int) and v >= 0 else None for v in rpm]
    return list(range(len(content_page_ids(content))))


def tablet_page(pdf_page: int, page_map: Sequence[Optional[int]]) -> Optional[int]:
    """The 1-based tablet page showing ``pdf_page``, if it is still shown."""
    for position, index in enumerate(page_map, start=1):
        if index == pdf_page:
            return position
    return None


# --- Inserting PDF pages ---------------------------------------------------


def _idx_between(low: str, high: Optional[str]) -> str:
    """A ``cPages`` sort key between ``low`` and ``high`` (None: after ``low``)."""
    out, i = "", 0
    while True:
        a = low[i] if i < len(low) else "a"
        b = high[i] if high is not None and i < len(high) else "{"
        if ord(b) - ord(a) > 1:
            return out + chr((ord(a) + ord(b)) // 2)
        out += a
        i += 1


def _max_counter(value: Any) -> int:
    """The highest counter in any ``"author:counter"`` timestamp in ``value``."""
    best = 0
    if isinstance(value, dict):
        stamp = value.get("timestamp")
        if isinstance(stamp, str) and ":" in stamp:
            try:
                best = int(stamp.split(":", 1)[1])
            except ValueError:
                pass
        for item in value.values():
            best = max(best, _max_counter(item))
    elif isinstance(value, list):
        for item in value:
            best = max(best, _max_counter(item))
    return best


def insert_pdf_pages(
    content: Dict[str, Any],
    after: int,
    page_ids: List[str],
    author: str,
    templates: Optional[List[str]] = None,
) -> int:
    """Add ``page_ids`` to ``content`` after tablet page ``after`` (0: first),
    backed by new PDF pages placed after the PDF page of the nearest earlier
    page; later PDF pages move down. Returns the first new PDF page index.

    formatVersion 2 entries get sort keys between their neighbours and
    timestamps under ``author`` (added to ``cPages.uuids``); formatVersion 1
    gets ``pages``, ``redirectionPageMap`` and the page counts updated.
    ``templates`` lines (``.pagedata``) are extended in place when given.
    """
    count = len(page_ids)
    page_map = pdf_page_map(content)
    if not 0 <= after <= len(page_map):
        raise ValueError(f"Page {after} does not exist; the document has {len(page_map)}.")
    earlier = [p for p in page_map[:after] if p is not None]
    target = max(earlier) + 1 if earlier else 0

    if has_cpages(content):
        cpages = content["cPages"]
        uuids = cpages.setdefault("uuids", [])
        mine = next((u.get("second") for u in uuids if u.get("first") == author), None)
        if mine is None:
            mine = max((int(u.get("second", 0)) for u in uuids), default=1) + 1
            uuids.append({"first": author, "second": mine})
        stamp = f"{mine}:{_max_counter(content) + 1}"
        for entry in cpages["pages"]:
            redir = entry.get("redir")
            if isinstance(redir, dict) and isinstance(redir.get("value"), int):
                if redir["value"] >= target:
                    entry["redir"] = {"timestamp": stamp, "value": redir["value"] + count}
        live = live_cpages_entries(content)
        before = live[after - 1] if after else None
        follow = live[after] if after < len(live) else None
        low = (before or {}).get("idx", {}).get("value", "") if before else ""
        high = follow.get("idx", {}).get("value") if follow else None
        position = cpages["pages"].index(before) + 1 if before else 0
        for offset, page_id in enumerate(page_ids):
            key = _idx_between(low, high)
            cpages["pages"].insert(
                position + offset,
                {
                    "id": page_id,
                    "idx": {"timestamp": stamp, "value": key},
                    "redir": {"timestamp": stamp, "value": target + offset},
                    "template": {"timestamp": stamp, "value": "Blank"},
                },
            )
            low = key
        original = cpages.get("original")
        if isinstance(original, dict) and isinstance(original.get("value"), int):
            cpages["original"] = {"timestamp": stamp, "value": original["value"] + count}
    else:
        ids = content.setdefault("pages", [])
        rpm = content.get("redirectionPageMap")
        if not isinstance(rpm, list) or len(rpm) != len(ids):
            rpm = list(range(len(ids)))
        rpm = [v + count if isinstance(v, int) and v >= target else v for v in rpm]
        rpm[after:after] = [target + i for i in range(count)]
        ids[after:after] = page_ids
        content["redirectionPageMap"] = rpm
        if isinstance(content.get("originalPageCount"), int):
            content["originalPageCount"] += count
        if templates is not None:
            templates[after:after] = ["Blank"] * count
    if isinstance(content.get("pageCount"), int):
        content["pageCount"] += count
    return target

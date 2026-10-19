"""Page order from a document's ``.content`` file.

formatVersion 2 lists pages as ``cPages.pages[]`` entries. A page deleted on
the tablet stays in that list with a ``deleted`` field (e.g.
``{"timestamp": "1:2", "value": 1}``) and is not shown. formatVersion 1 lists
page ids in a flat ``pages`` list.
"""

from typing import Any, Dict, List, Optional, Set


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

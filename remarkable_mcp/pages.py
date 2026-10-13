"""Page order from a document's ``.content`` file.

formatVersion 2 lists pages as ``cPages.pages[]`` entries. A page deleted on
the tablet stays in that list with a ``deleted`` field (e.g.
``{"timestamp": "1:2", "value": 1}``) and is not shown. formatVersion 1 lists
page ids in a flat ``pages`` list.
"""

from typing import Any, Dict, List, Set


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

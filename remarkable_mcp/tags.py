"""Document tag helpers.

The tablet stores document tags in the ``.content`` file as a list of
``{"name": ..., "timestamp": ...}`` objects. Some older exports and test data
carry plain strings in ``.metadata`` instead. These helpers normalize both to a
list of tag names.
"""

from typing import Any, Iterable, List, Optional


def tag_names(tags: Optional[Iterable[Any]]) -> List[str]:
    """Return tag names from a list of tag strings or ``{"name": ...}`` dicts."""
    names: List[str] = []
    for tag in tags or ():
        if isinstance(tag, dict):
            name = tag.get("name")
        else:
            name = tag
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def document_tag_names(metadata: Optional[dict], content: Optional[dict]) -> List[str]:
    """Tag names for a document, preferring ``.content`` over ``.metadata``."""
    if isinstance(content, dict) and content.get("tags"):
        return tag_names(content["tags"])
    if isinstance(metadata, dict):
        return tag_names(metadata.get("tags"))
    return []


def page_tag_entries(content: Optional[dict], page_order: List[str]) -> List[dict]:
    """Map ``.content`` ``pageTags`` to ``[{"page": n, "name": ...}]``.

    ``page_order`` is the document's page id order; ``page`` is the 1-based
    position of the tag's ``pageId`` in it. Tags on unknown pages are skipped.
    """
    if not isinstance(content, dict):
        return []
    positions = {page_id: i + 1 for i, page_id in enumerate(page_order)}
    entries = []
    for tag in content.get("pageTags") or ():
        if not isinstance(tag, dict):
            continue
        name = tag.get("name")
        page = positions.get(tag.get("pageId"))
        if isinstance(name, str) and name and page is not None:
            entry = {"page": page, "name": name}
            if entry not in entries:
                entries.append(entry)
    entries.sort(key=lambda e: (e["page"], e["name"].lower()))
    return entries


def page_tag_names(content: Optional[dict]) -> List[str]:
    """Distinct page tag names in a ``.content`` dict."""
    if not isinstance(content, dict):
        return []
    return tag_names(t for t in content.get("pageTags") or () if isinstance(t, dict))


def matches_tags(item_tags: Optional[Iterable[Any]], wanted: Iterable[str]) -> bool:
    """True if any wanted tag matches one of ``item_tags`` (case-insensitive)."""
    have = {name.lower() for name in tag_names(item_tags)}
    return any(tag.lower() in have for tag in wanted if isinstance(tag, str))

"""Document tag helpers.

The tablet stores document tags in the ``.content`` file as a list of
``{"name": ..., "timestamp": ...}`` objects, and page tags as ``pageTags``
entries that also carry a ``pageId``.
"""

from typing import List, Optional


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

"""Add, rename and remove tags in a document's ``.content``.

Document tags are ``content["tags"]`` entries ``{"name", "timestamp"}``; page
tags are ``content["pageTags"]`` entries ``{"name", "pageId", "timestamp"}``.
Timestamps are milliseconds since the epoch. Only these two lists change;
every other field of ``.content`` is left as it is.

Names match case-insensitively. A new tag takes the spelling of an existing
tag with the same name, so "graded" is not added next to "Graded". New tags
go first in the list, as the desktop app adds them.
"""

from typing import Any, Dict, Iterable, List, Optional

TAG_FIELDS = ("tags", "pageTags")


class TagEditError(ValueError):
    """A tag change that cannot be applied (bad name, page or combination)."""


def clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise TagEditError("Tag names must be non-empty text.")
    return name.strip()


def _spelling(name: str, known: Iterable[str]) -> str:
    for existing in known:
        if existing.lower() == name.lower():
            return existing
    return name


def edit_tags(
    content: Dict[str, Any],
    *,
    now_ms: int,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    rename: Optional[Dict[str, str]] = None,
    page_id: Optional[str] = None,
    known_tags: Iterable[str] = (),
) -> Dict[str, Any]:
    """Apply tag changes to ``content`` in place and describe the result.

    With ``page_id`` the changes apply to that page's tags, otherwise to the
    document's. Renames run first, then removals, then additions.
    ``known_tags`` are tag names used elsewhere in the library, whose
    spelling new tags reuse.

    Returns ``{"tags", "added", "removed", "renamed", "notes", "changed"}``;
    ``tags`` lists the tag names after the change.
    """
    add = [clean_name(n) for n in add]
    remove = [clean_name(n) for n in remove]
    renames = [(clean_name(old), clean_name(new)) for old, new in (rename or {}).items()]
    removing = {n.lower() for n in remove}
    clash = [n for n in add if n.lower() in removing]
    if clash:
        raise TagEditError(f"'{clash[0]}' is both added and removed.")

    field = "pageTags" if page_id else "tags"
    entries = content.get(field)
    if not isinstance(entries, list):
        entries = []

    def in_scope(entry: Any) -> bool:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            return False
        return entry.get("pageId") == page_id if page_id else True

    def find(name: str) -> Optional[dict]:
        for entry in entries:
            if in_scope(entry) and entry["name"].lower() == name.lower():
                return entry
        return None

    added: List[str] = []
    removed: List[str] = []
    renamed: List[Dict[str, str]] = []
    notes: List[str] = []
    known = list(known_tags)

    for old, new in renames:
        entry = find(old)
        if entry is None:
            notes.append(f"'{old}' was not tagged, so it was not renamed.")
            continue
        old_name = entry["name"]
        existing = find(new)
        if existing is entry:
            # Same tag; only a change of capitalization.
            if old_name == new:
                notes.append(f"'{new}' already has that name.")
                continue
        elif existing is not None:
            entries.remove(entry)
            new = existing["name"]
            notes.append(f"'{old_name}' merged into the existing tag '{new}'.")
            renamed.append({"from": old_name, "to": new})
            continue
        else:
            new = _spelling(new, known)
        entry["name"] = new
        entry["timestamp"] = now_ms
        renamed.append({"from": old_name, "to": new})

    for name in remove:
        entry = find(name)
        if entry is None:
            notes.append(f"'{name}' was not tagged, so it was not removed.")
            continue
        entries.remove(entry)
        removed.append(entry["name"])

    for name in add:
        existing = find(name)
        if existing is not None:
            notes.append(f"'{existing['name']}' is already tagged.")
            continue
        spelled = _spelling(name, known)
        if spelled != name:
            notes.append(f"Used the existing spelling '{spelled}' for '{name}'.")
        entry = {"name": spelled, "timestamp": now_ms}
        if page_id:
            entry = {"name": spelled, "pageId": page_id, "timestamp": now_ms}
        entries.insert(0, entry)
        added.append(spelled)

    changed = bool(added or removed or renamed)
    if changed:
        content[field] = entries
    return {
        "tags": [e["name"] for e in entries if in_scope(e)],
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "notes": notes,
        "changed": changed,
    }


def other_fields(content: Dict[str, Any]) -> Dict[str, Any]:
    """``content`` without the tag lists, for checking nothing else changed."""
    return {k: v for k, v in content.items() if k not in TAG_FIELDS}

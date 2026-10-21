"""Find documents and folders the way clients name them.

Clients see and send root-relative paths (see ``roots``). Every tool resolves
names through this module so the configured ``REMARKABLE_ROOT_PATH`` applies
the same way to reads and writes:

- a path starting with ``/`` is root-relative; a bare name matches an item's
  name or its root-relative path without the leading ``/``
- items outside the root, and the root folder itself, are never found
- trashed items (directly, or below a trashed folder) are skipped; a write
  tool may reach one by its explicit ``/trash/...`` path, only when no root
  is set
"""

from typing import List, Optional

from remarkable_mcp.api import TRASH_PARENT_ID, get_item_path
from remarkable_mcp.roots import get_root, is_under_root, to_library_path, to_root_relative


def is_directly_archived(item) -> bool:
    """Check the item's own metadata, ignoring its ancestors."""
    # SSH/local-dir/USB expose an explicit property. Require a real bool so
    # permissive proxy objects do not accidentally hide otherwise valid items.
    archived = getattr(item, "is_cloud_archived", None)
    if isinstance(archived, bool):
        return archived
    # Cloud mode: check parent == "trash"
    parent = item.Parent if hasattr(item, "Parent") else getattr(item, "parent", "")
    return parent == TRASH_PARENT_ID


def is_cloud_archived(item, items_by_id: Optional[dict] = None) -> bool:
    """Check if an item is cloud-archived (not available on device).

    Trashing a folder only re-parents the folder itself, so with
    ``items_by_id`` the ancestors are checked too.
    """
    if is_directly_archived(item):
        return True
    if not items_by_id:
        return False
    seen = {item.ID}
    parent_id = getattr(item, "Parent", "")
    while parent_id in items_by_id and parent_id not in seen:
        seen.add(parent_id)
        parent = items_by_id[parent_id]
        if is_directly_archived(parent):
            return True
        parent_id = getattr(parent, "Parent", "")
    return False


def display_path(item, items_by_id: dict) -> str:
    """The item's path as clients see it (root-relative)."""
    return to_root_relative(get_item_path(item, items_by_id))


def _in_root(path: str, root: str) -> bool:
    """Whether library ``path`` is strictly inside ``root`` (not the root)."""
    if root == "/":
        return True
    return is_under_root(path, root) and path.lower() != root.lower()


def _kind_matches(item, kind: str) -> bool:
    if kind == "folder":
        return bool(item.is_folder)
    if kind == "document":
        return not item.is_folder
    return True


def scoped_items(collection, items_by_id: dict, kind: str = "any") -> List:
    """Live items inside the root, of ``kind`` ("any", "document", "folder")."""
    root = get_root()
    return [
        item
        for item in collection
        if _kind_matches(item, kind)
        and not is_cloud_archived(item, items_by_id)
        and _in_root(get_item_path(item, items_by_id), root)
    ]


def find_item(
    name_or_path: str,
    collection,
    items_by_id: dict,
    kind: str = "any",
    include_trash: bool = False,
):
    """The live item inside the root named ``name_or_path``, or None.

    ``kind`` limits matches to "document" or "folder". With
    ``include_trash`` and no root set, an explicit ``/trash/...`` path also
    reaches a trashed item; a bare name never does.
    """
    root = get_root()
    wanted = to_library_path("/" + name_or_path.strip("/"), root).strip("/").lower()
    name = name_or_path.strip("/").lower()
    bare = not name_or_path.startswith("/")

    trashed = []
    for item in collection:
        if not _kind_matches(item, kind):
            continue
        if is_cloud_archived(item, items_by_id):
            trashed.append(item)
            continue
        item_path = get_item_path(item, items_by_id)
        if not _in_root(item_path, root):
            continue
        if bare and item.VissibleName.lower() == name:
            return item
        if item_path.strip("/").lower() == wanted:
            return item

    if not include_trash or root != "/":
        return None
    if not name_or_path.lower().startswith("/" + TRASH_PARENT_ID + "/"):
        return None
    for item in trashed:
        if get_item_path(item, items_by_id).strip("/").lower() == name:
            return item
    return None


def find_folder_id(path: Optional[str], collection, items_by_id: dict) -> Optional[str]:
    """The ID of the live folder at root-relative ``path``, or None.

    ``/`` (or empty) is the root: ``""`` for the library root, otherwise the
    root folder's ID (None when that folder does not exist).
    """
    root = get_root()
    if not path or path.strip("/") == "":
        if root == "/":
            return ""
        library = root.strip("/").lower()
    else:
        library = to_library_path("/" + path.strip("/"), root).strip("/").lower()
    for item in collection:
        if not item.is_folder or is_cloud_archived(item, items_by_id):
            continue
        if get_item_path(item, items_by_id).strip("/").lower() == library:
            return item.ID
    return None


def folder_paths(collection, items_by_id: dict) -> List[str]:
    """Root-relative paths of the live folders inside the root."""
    return [
        display_path(item, items_by_id) for item in scoped_items(collection, items_by_id, "folder")
    ]

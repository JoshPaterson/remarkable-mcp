"""The configured library root (``REMARKABLE_ROOT_PATH``).

A root such as ``/Work`` limits the server to that folder and presents it as
``/``. Two path spaces follow from it:

- library paths: full paths on the tablet, e.g. ``/Work/Project/Notes``
- root-relative paths: what clients see and send, e.g. ``/Project/Notes``

Matching against the root is case-insensitive; converted paths keep the case
of their input.
"""

import os
from typing import Optional


def get_root() -> str:
    """The configured root as ``/Name`` (no trailing slash), or ``/`` for none.

    Empty, ``/``, ``/Work``, ``/Work/`` and ``Work`` are all accepted.
    """
    root = os.environ.get("REMARKABLE_ROOT_PATH", "").strip()
    if not root or root == "/":
        return "/"
    if not root.startswith("/"):
        root = "/" + root
    return root.rstrip("/") or "/"


def is_under_root(path: str, root: Optional[str] = None) -> bool:
    """Whether library ``path`` is the root or inside it.

    ``root`` defaults to ``get_root()``.
    """
    root = get_root() if root is None else root
    if root == "/":
        return True
    path_lower = path.lower()
    root_lower = root.lower()
    return path_lower == root_lower or path_lower.startswith(root_lower + "/")


def to_root_relative(path: str, root: Optional[str] = None) -> str:
    """Library ``path`` as clients see it: ``/Work/Project`` -> ``/Project``.

    A path outside the root is returned unchanged. ``root`` defaults to
    ``get_root()``.
    """
    root = get_root() if root is None else root
    if root == "/":
        return path
    path_lower = path.lower()
    root_lower = root.lower()
    if path_lower == root_lower:
        return "/"
    if path_lower.startswith(root_lower + "/"):
        return path[len(root) :]
    return path


def to_library_path(path: str, root: Optional[str] = None) -> str:
    """Root-relative ``path`` (starting with ``/``) as a library path:
    ``/Project`` -> ``/Work/Project``. ``root`` defaults to ``get_root()``.
    """
    root = get_root() if root is None else root
    if root == "/":
        return path
    if path == "/":
        return root
    return root + path

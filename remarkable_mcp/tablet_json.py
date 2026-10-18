"""JSON in the layout the tablet writes.

The tablet and the desktop app write ``.content`` and ``.metadata`` with Qt's
indented JSON: four-space indentation, ``": "`` separators, empty objects and
arrays split over two lines, and a trailing newline. Rewriting an existing
document's ``.content`` in another layout (compact, sorted keys) left the
tablet unable to sync it; files in this layout sync normally.

Key order is preserved, so a file read with :func:`json.loads` and written
back unchanged is byte-for-byte identical to the tablet's.
"""

import json
from typing import Any

_INDENT = "    "


def _dumps(value: Any, level: int) -> str:
    inner = _INDENT * (level + 1)
    outer = _INDENT * level
    if isinstance(value, dict):
        if not value:
            return "{\n" + outer + "}"
        items = [
            inner + json.dumps(key, ensure_ascii=False) + ": " + _dumps(item, level + 1)
            for key, item in value.items()
        ]
        return "{\n" + ",\n".join(items) + "\n" + outer + "}"
    if isinstance(value, list):
        if not value:
            return "[\n" + outer + "]"
        items = [inner + _dumps(item, level + 1) for item in value]
        return "[\n" + ",\n".join(items) + "\n" + outer + "]"
    return json.dumps(value, ensure_ascii=False)


def dumps(value: Any) -> str:
    """Serialize ``value`` the way the tablet writes its JSON files."""
    return _dumps(value, 0) + "\n"

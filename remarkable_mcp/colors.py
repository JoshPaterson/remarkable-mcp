"""reMarkable v6 pen and highlight colors.

Scene items (``Line`` strokes and ``GlyphRange`` text highlights) store a
color id (rmscene ``PenColor``). Current firmware also stores an ARGB value
(rmscene ``color_rgba``); for color id 9 ("custom") that value is the actual
color, which is how the blue and orange highlighters are distinguished.
"""

from typing import Any, Dict, Optional

# Color id 9: the actual color is the item's ARGB value.
CUSTOM_COLOR_ID = 9

# Names for v6 color ids (rmscene PenColor).
COLOR_NAMES: Dict[int, str] = {
    0: "black",
    1: "gray",
    2: "white",
    3: "yellow",
    4: "green",
    5: "pink",
    6: "blue",
    7: "red",
    8: "gray",
    9: "highlight",
    10: "green",
    11: "cyan",
    12: "magenta",
    13: "yellow",
}

# Render colors for v6 color ids when no ARGB value applies.
RENDER_COLORS: Dict[int, str] = {
    0: "black",
    1: "#808080",
    2: "white",
    3: "#FFD700",
    4: "#00A000",
    5: "#FF69B4",
    6: "#4169E1",
    7: "#E00000",
    8: "#A0A0A0",
    9: "#FFD700",
    10: "#00C000",
    11: "#00CED1",
    12: "#FF00FF",
    13: "#FFD700",
}

# Highlighter colors written as color id 9 plus an ARGB value.
ARGB_NAMES: Dict[int, str] = {
    0xFFBEEAFE: "blue",
    0xFFFFC38C: "orange",
}

# ARGB value firmware writes when the color id alone defines the color.
_PLACEHOLDER_ARGB = 0xFF000000


def _color_id(color: Any) -> Optional[int]:
    value = getattr(color, "value", color)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _argb(rgba: Any) -> Optional[int]:
    """Pack an rmscene ``(r, g, b, a)`` tuple as an ARGB uint32."""
    if not isinstance(rgba, (tuple, list)) or len(rgba) != 4:
        return None
    try:
        r, g, b, a = (int(c) & 0xFF for c in rgba)
    except (TypeError, ValueError):
        return None
    return (a << 24) | (r << 16) | (g << 8) | b


def _stored_argb(color_id: Optional[int], rgba: Any) -> Optional[int]:
    """The ARGB value when it carries color information, else None."""
    argb = _argb(rgba)
    if argb is None:
        return None
    if argb == _PLACEHOLDER_ARGB and color_id != CUSTOM_COLOR_ID:
        return None
    return argb


def _hex(argb: int) -> str:
    return f"#{argb & 0xFFFFFF:06x}"


def color_info(color: Any, rgba: Any = None) -> Dict[str, Any]:
    """Describe a scene item's color.

    Returns ``{"color": name, "color_id": int | None, "color_hex": str | None}``.
    ``color_hex`` is set only when the tablet stored an ARGB value. Color id 9
    is named from its ARGB value, falling back to ``#rrggbb``.
    """
    color_id = _color_id(color)
    argb = _stored_argb(color_id, rgba)
    color_hex = _hex(argb) if argb is not None else None
    if color_id == CUSTOM_COLOR_ID and argb is not None:
        name = ARGB_NAMES.get(argb) or ARGB_NAMES.get(argb | 0xFF000000) or color_hex
    elif color_id in COLOR_NAMES:
        name = COLOR_NAMES[color_id]
    elif color_hex:
        name = color_hex
    else:
        name = "unknown"
    return {"color": name, "color_id": color_id, "color_hex": color_hex}


def render_color(color: Any, rgba: Any = None, default: str = "black") -> str:
    """SVG color for a scene item, using the ARGB value for color id 9."""
    color_id = _color_id(color)
    argb = _stored_argb(color_id, rgba)
    if argb is not None and (color_id == CUSTOM_COLOR_ID or color_id not in RENDER_COLORS):
        return _hex(argb)
    return RENDER_COLORS.get(color_id, default)

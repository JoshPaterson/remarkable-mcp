"""reMarkable v6 pen and highlight colors.

Scene items (``Line`` strokes and ``GlyphRange`` text highlights) store a
color id (rmscene ``PenColor``). Current firmware also stores an ARGB value
(rmscene ``color_rgba``); for color id 9 ("custom") that value is the actual
color, which is how the blue and orange highlighters are distinguished.
"""

import dataclasses
from typing import Any, Dict, Optional, Tuple

# Color id 9: the actual color is the item's ARGB value.
CUSTOM_COLOR_ID = 9

# ``color_rgba`` the tablet stores with a highlighter color that its color id
# alone defines.
NO_RGBA = (0, 0, 0, 255)


@dataclasses.dataclass(frozen=True)
class NamedColor:
    """A named pen or highlighter color.

    ``color_id`` is the v6 color id (rmscene ``PenColor``) and ``rgba`` the
    ``color_rgba`` the tablet stores with it (the actual color for id 9).
    ``screen`` is the color as the Paper Pro's screen shows it, which marks
    drawn into a PDF use so they match the tablet's own ink there. ``desktop``
    is the color the desktop and browser apps and the tablet's PDF export
    draw. ``opacity`` is set for highlighters the export draws translucent.
    """

    name: str
    color_id: int
    screen: str
    desktop: str
    rgba: Tuple[int, int, int, int] = NO_RGBA
    opacity: Optional[float] = None


# Pen colors. The Paper Pro's screen draws the pure colors without
# dithering, matching its own pen strokes; the softer desktop values are
# dithered on the screen and look different.
PENS: Tuple[NamedColor, ...] = (
    NamedColor("black", 0, "#000000", "#000000"),
    NamedColor("gray", 1, "#909090", "#909090"),
    NamedColor("white", 2, "#FFFFFF", "#FFFFFF"),
    NamedColor("blue", 6, "#0000FF", "#304AE0"),
    NamedColor("red", 7, "#FF0000", "#C23132"),
    NamedColor("green", 10, "#00FF00", "#91DA71"),
    NamedColor("yellow", 13, "#FFFF00", "#FAE719"),
    NamedColor("cyan", 11, "#00FFFF", "#74D2E8"),
    NamedColor("magenta", 12, "#FF00FF", "#C07FD2"),
)
# Highlighter colors. Blue, orange and gray on the screen are the export's
# values, which the tablet also shows as its own highlighters. Yellow, pink
# and green were measured on a Paper Pro, whose own highlighter colors differ
# from its export: drawing its highlighter across swatches that vary one
# channel (the others 00) shows, per channel, the last swatch the stroke goes
# under, which is the highlighter's value. The export draws gray as black at
# 25% opacity with the Darken blend: #BFBFBF on white paper.
HIGHLIGHTERS: Tuple[NamedColor, ...] = (
    NamedColor("yellow", 3, "#FFED75", "#FEFD60"),
    NamedColor("blue", CUSTOM_COLOR_ID, "#BEEAFE", "#BEEAFE", (190, 234, 254, 255)),
    NamedColor("pink", 5, "#F29EFF", "#FF55CF"),
    NamedColor("orange", CUSTOM_COLOR_ID, "#FFC38C", "#FFC38C", (255, 195, 140, 255)),
    NamedColor("green", 4, "#ACFF85", "#A9FA5C"),
    NamedColor("gray", 8, "#000000", "#000000", opacity=0.2509804),
)
PEN_BY_NAME: Dict[str, NamedColor] = {c.name: c for c in PENS}
HIGHLIGHTER_BY_NAME: Dict[str, NamedColor] = {c.name: c for c in HIGHLIGHTERS}

_BY_ID = {c.color_id: c for c in (*PENS, *HIGHLIGHTERS) if c.color_id != CUSTOM_COLOR_ID}

# Names for v6 color ids.
COLOR_NAMES: Dict[int, str] = {
    **{i: c.name for i, c in _BY_ID.items()},
    CUSTOM_COLOR_ID: "highlight",
}

# Render colors for v6 color ids when no ARGB value applies: the colors the
# tablet's PDF export draws (see HIGHLIGHT_OPACITY for translucent ones).
# Color id 9 without an ARGB value draws as the yellow highlighter.
RENDER_COLORS: Dict[int, str] = {
    **{i: c.desktop for i, c in _BY_ID.items()},
    CUSTOM_COLOR_ID: HIGHLIGHTER_BY_NAME["yellow"].desktop,
}

# Opacity of highlighter colors the export draws translucent; others are opaque.
HIGHLIGHT_OPACITY: Dict[int, float] = {
    c.color_id: c.opacity for c in HIGHLIGHTERS if c.opacity is not None
}


def _pack(rgba: Tuple[int, int, int, int]) -> int:
    r, g, b, a = rgba
    return (a << 24) | (r << 16) | (g << 8) | b


# Highlighter colors written as color id 9 plus an ARGB value.
ARGB_NAMES: Dict[int, str] = {
    _pack(c.rgba): c.name for c in HIGHLIGHTERS if c.color_id == CUSTOM_COLOR_ID
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


def highlight_opacity(color: Any) -> str:
    """SVG opacity attribute (with a leading space) for highlighter ink of
    ``color``, or "" when it is opaque."""
    opacity = HIGHLIGHT_OPACITY.get(_color_id(color))
    return f' opacity="{opacity}"' if opacity is not None else ""

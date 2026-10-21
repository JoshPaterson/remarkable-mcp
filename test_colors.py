"""The named color tables: calibrated values pinned exactly."""

from remarkable_mcp import colors, ink_marks, pdf_marks, strokes, text_highlights


def _rgb(hex_color):
    return tuple(int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))


def _rgbs(table):
    return {name: _rgb(value) for name, value in table.items()}


PEN_SCREEN = {
    "black": "#000000",
    "gray": "#909090",
    "white": "#FFFFFF",
    "blue": "#0000FF",
    "red": "#FF0000",
    "green": "#00FF00",
    "yellow": "#FFFF00",
    "cyan": "#00FFFF",
    "magenta": "#FF00FF",
}
HIGHLIGHT_SCREEN = {
    "yellow": "#FFED75",
    "blue": "#BEEAFE",
    "pink": "#F29EFF",
    "orange": "#FFC38C",
    "green": "#ACFF85",
    "gray": "#000000",
}
PEN_DESKTOP = {
    "black": "#000000",
    "gray": "#909090",
    "white": "#FFFFFF",
    "blue": "#304AE0",
    "red": "#C23132",
    "green": "#91DA71",
    "yellow": "#FAE719",
    "cyan": "#74D2E8",
    "magenta": "#C07FD2",
}
HIGHLIGHT_DESKTOP = {
    "yellow": "#FEFD60",
    "blue": "#BEEAFE",
    "pink": "#FF55CF",
    "orange": "#FFC38C",
    "green": "#A9FA5C",
    "gray": "#000000",
}


def test_pdf_mark_palettes():
    assert pdf_marks.COLORS == _rgbs(PEN_SCREEN)
    assert pdf_marks.HIGHLIGHT_COLORS == _rgbs(HIGHLIGHT_SCREEN)
    assert pdf_marks.DESKTOP_COLORS == _rgbs(PEN_DESKTOP)
    assert pdf_marks.DESKTOP_HIGHLIGHT_COLORS == _rgbs(HIGHLIGHT_DESKTOP)
    assert pdf_marks.HIGHLIGHT_OPACITY == {"gray": 0.2509804}


def test_ink_color_tables():
    assert colors.COLOR_NAMES == {
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
    assert colors.RENDER_COLORS == {
        0: "#000000",
        1: "#909090",
        2: "#FFFFFF",
        3: "#FEFD60",
        4: "#A9FA5C",
        5: "#FF55CF",
        6: "#304AE0",
        7: "#C23132",
        8: "#000000",
        9: "#FEFD60",
        10: "#91DA71",
        11: "#74D2E8",
        12: "#C07FD2",
        13: "#FAE719",
    }
    assert colors.HIGHLIGHT_OPACITY == {8: 0.2509804}
    assert colors.ARGB_NAMES == {0xFFBEEAFE: "blue", 0xFFFFC38C: "orange"}
    assert text_highlights.COLORS == {
        "yellow": (3, (0, 0, 0, 255)),
        "green": (4, (0, 0, 0, 255)),
        "pink": (5, (0, 0, 0, 255)),
        "gray": (8, (0, 0, 0, 255)),
        "blue": (9, (190, 234, 254, 255)),
        "orange": (9, (255, 195, 140, 255)),
    }
    assert ink_marks.PEN_COLORS == {
        "black": 0,
        "gray": 1,
        "white": 2,
        "blue": 6,
        "red": 7,
        "green": 10,
        "cyan": 11,
        "magenta": 12,
        "yellow": 13,
    }
    assert strokes.COLOR_IDS == {
        "black": 0,
        "gray": 1,
        "white": 2,
        "yellow": 3,
        "green": 4,
        "pink": 5,
        "blue": 6,
        "red": 7,
        "gray_overlap": 8,
        "highlight": 9,
        "green_2": 10,
        "cyan": 11,
        "magenta": 12,
        "yellow_2": 13,
        "grey": 1,
    }


def test_canvas_colors_match_erasable_marks_for_the_same_tool():
    fineliner = strokes.resolve_tool("fineliner")
    highlighter = strokes.resolve_tool("highlighter")
    for name, color_id in ink_marks.PEN_COLORS.items():
        assert strokes.resolve_ink_color(name, fineliner) == (color_id, None)
    for name, (color_id, rgba) in text_highlights.COLORS.items():
        expected = rgba if color_id == colors.CUSTOM_COLOR_ID else None
        assert strokes.resolve_ink_color(name, highlighter) == (color_id, expected)
    assert strokes.resolve_color("yellow") == 13 and strokes.resolve_color("Grey") == 1
    assert strokes.resolve_color("yellow_2", highlighter) == 13
    assert strokes.resolve_color(3, fineliner) == 3


def test_canvas_blue_highlighter_stores_its_rgba():
    from rmscene.tagged_block_common import CrdtId

    stroke = {"points": [[0.1, 0.1], [0.2, 0.1]], "tool": "highlighter", "color": "blue"}
    block = strokes.build_line_block(CrdtId(0, 11), CrdtId(1, 20), stroke, 1404, 1872)
    assert int(block.item.value.color) == 9
    assert block.item.value.color_rgba == (190, 234, 254, 255)


def test_text_highlight_color_is_normalized():
    part = {"start": 0, "length": 5, "text": "hello", "rect": (100, 200, 140, 212)}
    result = pdf_marks.edit_marks(
        [],
        add=[{"page": 1, "text_highlight": "hello", "color": " Blue "}],
        remove=[],
        clear=False,
        page_map=[0],
        sizes=[(595.0, 842.0)],
        color=None,
        locate=lambda *_: [part],
    )
    assert result["marks"][0]["color_name"] == "blue"

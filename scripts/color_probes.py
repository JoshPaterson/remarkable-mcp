# /// script
# dependencies = ["pymupdf"]
# ///
"""Generate the probe PDFs used to match mark colors to a tablet's screen.

See docs/color-calibration.md for the procedure.

    uv run scripts/color_probes.py export-colors "Color samples.pdf"
    uv run scripts/color_probes.py pens pens.pdf
    uv run scripts/color_probes.py channels probe.pdf \\
        --page "Yellow highlighter" R:00:16 G:00:16 B:00:16 \\
        --page "Blue highlighter (control)" R:00:16 G:00:16 B:00:16
    uv run scripts/color_probes.py blend blend.pdf
"""

import argparse
import json
from collections import defaultdict

import pymupdf

W, H = 595, 842
CHANNELS = {"R": 0, "G": 1, "B": 2}
NAMES = {"R": "Red", "G": "Green", "B": "Blue"}
PURE = {
    "blue": "#0000FF",
    "red": "#FF0000",
    "green": "#00FF00",
    "yellow": "#FFFF00",
    "cyan": "#00FFFF",
    "magenta": "#FF00FF",
}
HIGHLIGHTERS = ["yellow", "blue", "pink", "orange", "green", "gray"]
BLOCKS = [
    ("gray", "#808080"),
    ("light gray", "#C0C0C0"),
    ("red", "#FF0000"),
    ("blue", "#0000FF"),
    ("cyan", "#00FFFF"),
    ("magenta", "#FF00FF"),
    ("yellow", "#FFFF00"),
]


def rgb(hex_color: str):
    return tuple(int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))


def hex_of(color) -> str:
    return "#" + "".join(f"{round(v * 255):02X}" for v in color)


def export_colors(path: str) -> None:
    """Print the colors and blend states the tablet's PDF export draws with."""
    doc = pymupdf.open(path)
    for xref in range(1, doc.xref_length()):
        obj = doc.xref_object(xref)
        if "/BM" in obj or "/CA" in obj:
            print(f"graphics state {xref}: {' '.join(obj.split())}")
    groups = defaultdict(list)
    for d in doc[0].get_drawings():
        groups[(d.get("color"), d.get("fill"), d.get("stroke_opacity"))].append(d["rect"])
    for (stroke, fill, opacity), rects in sorted(
        groups.items(), key=lambda kv: min(r.y0 for r in kv[1])
    ):
        top = min(r.y0 for r in rects)
        color = hex_of(stroke) if stroke else hex_of(fill)
        kind = "stroke" if stroke else "fill"
        print(f"y={top:5.0f}  {kind:6} {color}  opacity={opacity}  paths={len(rects)}")


def pens(out: str) -> None:
    """Pure pen colors to compare with the tablet's own pen strokes."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_text((36, 44), "Pen colors", fontsize=14, fontname="hebo")
    page.insert_text(
        (36, 60),
        "Draw each pen color beside its swatch. A match renders smooth, not speckled.",
        fontsize=9,
        fontname="helv",
    )
    for i, (name, value) in enumerate(PURE.items()):
        y0 = 90 + i * 70
        page.draw_rect((36, y0, 200, y0 + 40), color=None, fill=rgb(value))
        page.insert_text((210, y0 + 24), f"{name} {value}", fontsize=10, fontname="helv")
    doc.save(out)


def channels(out: str, pages) -> None:
    """One row per channel: the tested channel steps, the others are 00."""
    doc = pymupdf.open()
    for title, *rows in pages:
        page = doc.new_page(width=W, height=H)
        page.insert_text((30, 44), title, fontsize=14, fontname="hebo")
        page.insert_text(
            (30, 62),
            "Draw one stroke across each row. Before the flash, note the last swatch "
            "from the left where the stroke goes under (disappears).",
            fontsize=7.5,
            fontname="helv",
        )
        for r, spec in enumerate(rows):
            parts = spec.split(":")
            channel, start, step = parts[0].upper(), int(parts[1], 16), int(parts[2])
            count = int(parts[3]) if len(parts) > 3 else 17
            values = [min(start + i * step, 0xFF) for i in range(count)]
            if values[-1] < 0xFF and step >= 16:
                values.append(0xFF)
            width = min(52, (W - 60) / len(values) - 6)
            y0 = 100 + r * 200
            page.insert_text(
                (30, y0 - 10),
                f"{NAMES[channel]} channel {values[0]:02X}-{values[-1]:02X} (other channels 00)",
                fontsize=10,
                fontname="hebo",
            )
            for i, v in enumerate(values):
                color = [0.0, 0.0, 0.0]
                color[CHANNELS[channel]] = v / 255
                x0 = 30 + i * (width + 6)
                page.draw_rect((x0, y0, x0 + width, y0 + 70), color=None, fill=color)
                page.insert_text((x0 + 2, y0 + 82), f"{v:02X}", fontsize=8, fontname="helv")
    doc.save(out)


def blend(out: str) -> None:
    """Colored blocks per highlighter; prints the highlight marks to add."""
    left, block, gap, row_h, step, top = 70, 58, 8, 64, 116, 110
    right = left + len(BLOCKS) * (block + gap) - gap
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_text((30, 44), "Highlighter blend test", fontsize=14, fontname="hebo")
    page.insert_text(
        (30, 60),
        "Top half: highlight mark. Draw the real highlighter across the bottom half.",
        fontsize=8,
        fontname="helv",
    )
    marks = []
    for r, name in enumerate(HIGHLIGHTERS):
        y0 = top + r * step
        page.insert_text((30, y0 - 8), f"{name.capitalize()} highlighter", fontsize=10)
        for i, (label, value) in enumerate(BLOCKS):
            x0 = left + i * (block + gap)
            page.draw_rect((x0, y0, x0 + block, y0 + row_h), color=None, fill=rgb(value))
            if r == 0:
                page.insert_text((x0, top - 22), label, fontsize=7)
        region = [(left - 8) / W, (y0 + 4) / H, (right + 8) / W, (y0 + row_h * 0.45) / H]
        marks.append(
            {
                "page": 1,
                "id": f"hl-{name}",
                "shape": "highlight",
                "color": name,
                "region": [round(v, 4) for v in region],
            }
        )
    doc.save(out)
    print("Upload the PDF, then call remarkable_mark_pdf(document, add=...) with:")
    print(json.dumps(marks))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("export-colors").add_argument("pdf")
    sub.add_parser("pens").add_argument("out")
    p = sub.add_parser("channels")
    p.add_argument("out")
    p.add_argument(
        "--page",
        nargs="+",
        action="append",
        required=True,
        metavar="TITLE_THEN_ROWS",
        help="a page title, then rows as CHANNEL:START:STEP[:COUNT], e.g. G:D0:2",
    )
    sub.add_parser("blend").add_argument("out")
    args = parser.parse_args()
    if args.command == "export-colors":
        export_colors(args.pdf)
    elif args.command == "pens":
        pens(args.out)
    elif args.command == "channels":
        channels(args.out, args.page)
    else:
        blend(args.out)


if __name__ == "__main__":
    main()

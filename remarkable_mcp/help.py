"""Help pages served as MCP resources, for details too long for a tool docstring."""

MARKS_HELP_URI = "remarkable://help/marks"

MARKS_HELP = """# remarkable_mark_pdf: placement, covers and erasable marks

Positions are page fractions (0-1 from the top-left): a point is `[x, y]`, a
region `[left, top, right, bottom]`. Sizes and widths are in points.

## Placing a mark

- `at`: the mark's `anchor` point goes at `[x, y]`. Anchors: top-left, top,
  top-right, left, center, right, bottom-left, bottom, bottom-right. Default
  center for check and cross, top-left otherwise. Anchor "left" with `at` at
  the right end of a text line's middle puts a note beside the line.
- `region`: text wraps within it; circle, box and highlight stretch to fill
  it; path, latex and graphics are scaled to fit it, centered and keeping
  their aspect (a path with `stretch: true` fills it); graphics sit at its
  top-left.
- `near`: text, latex, check, cross and path marks go in the closest free
  space to `[x, y]`, clear of the user's ink, the printed text and other
  marks (including marks placed earlier in the same call). Give the point of
  what the mark is about, at its edge (such as the end of an answer) rather
  than its middle. If no place fits, the tool says so: shorten the note or
  make it smaller.

## Leaders

With `near`, `"leader": true` draws a thin line from the mark to the point,
ending in a dot. It is drawn only when the mark cannot sit beside the point;
positions giving a diagonal leader are preferred. It starts at the middle of
the note's top or bottom for a point above or below the note, otherwise just
past the end that faces the point, at the text's top, middle or baseline, so
it never crosses the note (a bordered note's leader starts at its border). A
leader never crosses the user's handwriting or other marks, and crosses
printed text only when nothing else reaches the point.

## Mark details

- text: `"\\n"` starts a new line. `border` draws a rounded box with a white
  background.
- shape: `width` is the line width; `dot` puts a dot at the `to` end of a
  line or arrow. Highlights darken like the tablet's highlighter: the color
  shows on the paper and text under it stays dark.
- path: SVG path data in any units, e.g. `M 0 10 L 5 0 L 10 10` is a caret.
  With `at` it is scaled so its larger side is `size` points (default 12).
  `width` stays in points; `fill` is a color. Several subpaths (each starting
  with M) share one style; for drawings with several colors or text, or
  output from other tools, use a graphic.
- graphic: SVG may only reference its own elements or `data:` URIs.
- description: up to 200 characters saying what the mark is and why, for
  later sessions; shown in listings and by remarkable_read. Describe path
  marks, which are otherwise only path data, e.g. "gold star: neat, complete
  working".
- Listings give each mark's content, color, position and style (size, bold,
  anchor, width and so on), so it can be re-created exactly.

## Colors

Named colors are the tablet's pen colors (black, gray, white, blue, red,
green, yellow, cyan, magenta) and, for highlights, its highlighter colors
(yellow, blue, pink, orange, green, gray). They are tuned to look right on the
tablet's screen; PDF exports (remarkable_export) re-draw them in the desktop
and browser apps' colors. The call's `color` applies to marks that set none;
each mark takes the matching pen or highlighter shade.

## Covers: letting the user hide a mark

Marks drawn into the PDF cannot be erased on the tablet. Any mark can take
`"cover": true`: white ink goes over it on a "Mark cover" layer at the bottom
of the page, made hidden, and showing that layer hides the mark. A layer name
instead of `true` (e.g. "Answer key") makes a group the user hides separately;
`"covered": true` makes a new layer shown, so its marks start hidden (e.g.
answers to reveal later). Listings give each covered mark's layer and state:
hidden on tablet, shown on tablet, or cover erased on tablet. Treat marks the
user has hidden as dismissed. Removing a mark removes its cover.

## Erasable marks

Text, shape (including highlight) and path marks can take `"erasable": true`:
they are written as tablet ink instead of into the PDF, so the user can
erase, move or recolor them on the tablet, and their colors match in every
app. Text is drawn in a single-stroke font (bold is a thicker line); shapes,
paths and leaders with a pen, highlights with the highlighter. Named colors
only (pen colors; highlighter colors for highlights), no fill or opacity.
Option `layer` as for text_highlight. Listings give their state: on tablet,
partly erased or erased on tablet.

## Text highlights

`text_highlight` highlights printed text on the page as the tablet's own
highlighter does; the user can erase it on the tablet. It may run over several
lines. Options: `color` (a highlighter color, default yellow), `occurrence`
(which match, default 1), `layer` (a layer name; a new one is added on top if
the page has none; default the page's selected layer, as the tablet does).
`layout_page` lists the page's layers. Not with `at`, `region` or `near`.
"""


def register_marks_help(server) -> None:
    """Serve MARKS_HELP at MARKS_HELP_URI on ``server``."""

    @server.resource(
        MARKS_HELP_URI,
        name="remarkable_mark_pdf help",
        description="Placement, leader, cover, erasable and text highlight rules for marks",
        mime_type="text/markdown",
    )
    def marks_help() -> str:
        return MARKS_HELP

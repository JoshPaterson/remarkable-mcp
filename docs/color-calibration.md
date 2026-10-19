# Mark Color Calibration

How the named colors of `remarkable_mark_pdf` were matched exactly to a
reMarkable Paper Pro's own pen and highlighter colors, and how to repeat it for
another tablet. Probe PDFs come from `scripts/color_probes.py`.

## Why it is needed

- Tablet ink stores a **color id**; each app maps ids to its own palette.
  Marks live in the PDF, which only stores **RGB**, so their values must be
  chosen to look right on one screen.
- The Paper Pro screen shows a few colors natively and **dithers** (speckles)
  all other PDF colors.
- The tablet's **PDF export does not show its screen colors**: the export's pen
  and some highlighter values look different on the tablet.
- Highlighters are drawn with the **Darken** blend (per channel,
  `min(paper, highlighter)`) at opacity 1, except gray: black at 25% opacity.

## Result (Paper Pro)

| Pen | Value | Highlighter | Value |
|---|---|---|---|
| black | `#000000` | yellow | `#FFED75` (measured) |
| gray | `#909090` | blue | `#BEEAFE` (export) |
| white | `#FFFFFF` | pink | `#F29EFF` (measured) |
| blue | `#0000FF` | orange | `#FFC38C` (export) |
| red | `#FF0000` | green | `#ACFF85` (measured) |
| green | `#00FF00` | gray | black, opacity 0.251 (export) |
| yellow | `#FFFF00` | | |
| cyan | `#00FFFF` | | |
| magenta | `#FF00FF` | | |

Mark colors live in `remarkable_mcp/pdf_marks.py` (`COLORS`,
`HIGHLIGHT_COLORS`, `HIGHLIGHT_OPACITY`). The renderer
(`remarkable_mcp/colors.py`, `RENDER_COLORS`) deliberately keeps the export
palette, so `remarkable_image` shows ink as the desktop app does.

## Procedure

Judge everything on the tablet. Renders and the desktop app do not show the
tablet's dithering or its screen palette.

1. **Reference strokes.** In a notebook, draw one stroke with every pen color
   and every highlighter color, labeled ("Color samples"). Export it as PDF
   from the desktop app and read its colors and blend states:

   ```bash
   uv run scripts/color_probes.py export-colors "Color samples.pdf"
   ```

   Any color the tablet stores as RGB (here the blue and orange highlighters)
   already matches; gray, white and black matched too.

2. **Pens.** Upload `uv run scripts/color_probes.py pens pens.pdf` and draw
   each pen beside its swatch. On the Paper Pro the pure colors rendered smooth
   and matched; if they do not, probe nearby values the same way as step 3.

3. **Highlighters: channel method.** For each highlighter, each row varies one
   channel while the other two are `00`. Draw the real highlighter across the
   row and, **before the screen flashes**, note the last swatch where the stroke
   goes under (disappears). With Darken, the stroke goes under exactly while
   the swatch is at or below the highlighter in every channel, so that swatch
   is the channel's value. A row where it never goes over means `FF`.

   Narrow in three rounds, centering each round on the last transition:

   ```bash
   uv run scripts/color_probes.py channels coarse.pdf \
       --page "Yellow highlighter" R:00:16 G:00:16 B:00:16
   uv run scripts/color_probes.py channels fine.pdf \
       --page "Yellow highlighter" G:D0:2 B:60:2
   uv run scripts/color_probes.py channels exact.pdf \
       --page "Yellow highlighter" G:E8:1:9 B:70:1:9
   ```

   Rows are `CHANNEL:START:STEP[:COUNT]` (hex start, 17 swatches by default).
   Add a page for a highlighter of known value (blue, `#BEEAFE`) as a control.

4. **Verify.** Put the colors in `pdf_marks.py` and compare on the tablet:
   - Swatches beside the reference strokes: a copy of "Color samples" with
     `line` marks per pen color and `highlight` marks per highlighter color.
     Expect an exact match, smooth or speckled alike.
   - Blend: `uv run scripts/color_probes.py blend blend.pdf`, upload it, add
     the highlight marks it prints with `remarkable_mark_pdf`, and draw each
     real highlighter across the bottom half of its row. Both halves of every
     block should match; this checks Darken and opacity over color.

## Pitfalls

- The other channels of a channel probe must be `00`, not `FF`: with `FF`
  the stroke always shows on top and there is no visible transition.
- Read the over/under cue before the flash; after it, small differences blur
  into the dithering.
- A grid of whole colors judged by eye gets close (within a few units) but is
  slow; the channel method gave exact values in three rounds.
- PDF colors cannot match both the tablet and the desktop app. Marks drawn as
  native ink would; see `future-plans.md`.

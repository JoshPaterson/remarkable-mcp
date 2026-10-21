# Native-Ink Marks (Design Notes)

Not implemented. These notes record how `remarkable_mark_pdf` could draw
**erasable** marks as native ink (strokes in the page's `.rm` file) instead of
in the PDF, and what was verified on a Paper Pro through the cloud
(2026-10-19). The feature was set aside because PDF marks already match the
tablet's colors exactly; build it if erasable marks, or marks that match in
the desktop apps too, become worth the extra write path.

## Why

- Ink stores a **color id**; each app maps it to its own palette, so ink looks
  right on the tablet and in the desktop and browser apps. A PDF color is one
  RGB value for every viewer (see `color-calibration.md`).
- Ink can be erased, selected, moved and recolored on the tablet.
- Cost: a second write path that edits the tablet's own `.rm` files (an
  unpublished format; rmscene cannot read every block on current firmware).

## Proposed interface

`"erasable": true` on a mark in `remarkable_mark_pdf`:

- `shape` (check, cross, circle, box, line, arrow) becomes pen strokes;
  `highlight` becomes a highlighter stroke; `path` becomes strokes (curves
  flattened). Filled paths, `text`, `latex` and `graphic` are refused.
- Named colors only (ink stores color ids). Pens: ids 0 black, 1 gray,
  2 white, 6 blue, 7 red, 10 green, 11 cyan, 12 magenta, 13 yellow.
  Highlighters: 3 yellow, 4 green, 5 pink, 8 gray, and 9 with an ARGB value
  for blue (`#BEEAFE`) and orange (`#FFC38C`).
- The mark list in the PDF records, per erasable mark, the page and the stroke
  ids written, so it can be listed ("erased on tablet" when tombstoned) and
  removed by mark id. One sync commit updates the PDF and the `.rm`.

## File format facts

- A page's ink is `{doc_id}/{page_id}.rm`, a doc-index entry; uploaded PDFs
  need no `.content` change to gain one.
- Item ids are `(author, counter)`. `author` is a file-local number (one byte
  in ids, 1-255; 0 is structure such as layers) mapped to a UUID in the
  `AuthorIdsBlock` (block type `0x09`). `counter` is a varuint shared by all
  authors: each writer uses one more than the highest counter in the file.
  Uniqueness comes from the author number.
- Coordinates on PDF pages map through `extract._page_frame` (page fractions
  to stroke units, x centered); placement was exact on the tablet.

## Writing ink (all verified on the tablet)

`rm_ink.py` implements these steps (`PageWriter`).

1. **Author.** One random UUID per installation, stored in the config. In each
   file, reuse its author number if listed, else take the lowest free number
   and replace only the `AuthorIdsBlock` with one that adds it (rmscene
   re-encodes this block byte for byte; the desktop app does the same when it
   edits typed text). Copy every other block unchanged.
2. **Strokes.** `counter = max over all ids in the file + 1`, author = the
   MCP's number; append `SceneLineItemBlock`s to the end (`strokes.py`,
   `build_line_block`, parent = the page's layer).
3. **Removal.** Replace the stroke's own block in place with a tombstone: same
   item, `value=None`, `deleted_length=1` (25 bytes; rmscene reproduces the
   tablet's tombstones exactly). Never drop the bytes: the tablet's copy would
   bring the stroke back.
4. **First ink on a page.** Create the `.rm` without a `RootTextBlock`:
   `notebooks.blank_page_rm_bytes` minus block type `0x07`. With the empty
   text block the tablet treated the page as a text page and re-created the
   MCP's stroke under its own author in an anchored group. Without it the
   tablet accepted the file, added its `SceneInfo` and joined as author 2.

## Test results (cloud, Paper Pro)

| Test | Result |
|---|---|
| Append under the tablet's author, online | Synced and displayed |
| Same, while the tablet drew offline | **Collision**: both used `(1, 18)`; each side silently kept its own stroke, the tablet and the cloud disagreed |
| Append under the MCP's own author, tablet offline | Both strokes kept everywhere; the tablet kept the author entry |
| Tombstone an MCP stroke, tablet offline | Stayed deleted; the offline stroke was kept |
| First `.rm` with an empty text block | Accepted, but the stroke was re-created under the tablet's author |
| First `.rm` without a text block | Accepted unchanged |

## Cover layers (verified)

A cover layer hides PDF marks on the tablet: white strokes over a mark, on a
layer below the user's, so showing the layer hides the mark and hiding the
layer shows it again. The user's ink on the layers above stays visible,
including writing over a covered mark. Tested on a Paper Pro through the
cloud (2026-10-19):

- **New layer.** Under the MCP's author number `n` and next counter `c`:
  `SceneTreeBlock(tree_id=(n, c), node_id=(0, 0), is_update=True,
  parent_id=(0, 1))`, a `TreeNodeBlock` with `Group(node_id=(n, c),
  label=LwwValue((n, c+1), name), visible=LwwValue((n, c+2), shown))`, and a
  `SceneGroupItemBlock(parent_id=(0, 1))` with item `(n, c+3)` whose value is
  the layer. Insert each after the last existing block of its type; strokes go
  at the end with the layer as parent.
- **Bottom of the stack.** Give the group item `left_id=(0, 0)` and
  `right_id` = the first item of the root's layer sequence. Layers the user
  adds later go on top; the cover stays at the bottom.
- **Initial state.** `visible` False creates the layer hidden (marks show).
- **Covering.** White (color 2) fineliner strokes about 5pt wide, 3.5pt apart,
  over the mark's box plus 3pt, cover text fully.
- **Page with no ink.** Create Layer 1 as well (as in step 4 above) and add a
  `SceneInfo` whose `current_layer` is Layer 1 `(0, 11)`. Without a
  `SceneInfo` the tablet selected the cover layer, so the user's writing went
  onto it. On pages with ink, leave `SceneInfo` alone (`current_layer` may be
  `(0, 0)`; the tablet then selects Layer 1).
- **Concurrent edit.** A cover layer added while the tablet drew offline
  merged: both kept.

## Text highlights (verified)

The tablet's text highlights (made by selecting PDF text with the
highlighter) are `SceneGlyphItemBlock`s (block type `0x03`) on a layer, each
holding a `GlyphRange(start, length, text, color, rectangles, color_rgba)`.
rmscene re-encodes them byte for byte. Verified on a Paper Pro (2026-10-21):

- **One item per line.** The tablet cannot highlight across lines in one
  stroke; a two-line selection is two items.
- **`start`** is the offset of the text in the page's text as PyMuPDF's
  `get_text()` gives it, plus one for every line break before it (the tablet
  counts a line break as two characters). `length` is the text's length.
  Matched on every highlight checked (OCR'd and typeset pages).
- **`rectangles`** are in stroke units (x centered on the page, y from the
  top). Matching the tablet within about 0.5pt: top halfway between
  PyMuPDF's loose (default) and tight (`TEXT_ACCURATE_BBOXES`) character
  boxes' tops, bottom at the tight box's bottom, left edge 0.16 x font size
  (pt) before the loose box and right edge 0.10 x font size after it.
  Rectangles from the loose box alone stand visibly too tall.
- **Colors.** Yellow is stored as id 3 with `color_rgba` (0, 0, 0, 255); the
  blue highlighter as id 9 with `color_rgba` (190, 234, 254, 255).
- **Written by the MCP** under its own author id on Layer 1 (new ink file
  as in "Page with no ink"), they show, can be erased, and match the tablet's
  own. A wrong `start` still displayed normally.
- **Undo after erase** re-creates the highlight as a new item under the
  tablet's author (the MCP's item stays a tombstone).

## Edge cases

- **Partial erase on the tablet** tombstones the stroke and adds the remaining
  pieces as new strokes under the tablet's author; each piece's left neighbor
  chains back to the original. A partly erased mark is no longer the MCP's;
  removal can follow the chain or leave the pieces.
- **Notebook pages** (rather than PDF pages) are untested: layers and
  text-anchored groups may matter.
- `remarkable_author` (SSH) writes under the tablet's author and has the same
  collision risk with edits from other devices not yet on the tablet; it
  should move to the MCP's own author too.

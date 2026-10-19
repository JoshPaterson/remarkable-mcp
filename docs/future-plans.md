# Future Work

This file summarizes accepted problem areas. GitHub issues contain the current
scope and acceptance criteria.

## Near-term work

### SSH reliability

Track intermittent large-library SSH failures in
[#157](https://github.com/SamMorrowDrums/remarkable-mcp/issues/157).

### Cloud authoring

Add native notebook creation to cloud mode before considering page mutation.
See [#118](https://github.com/SamMorrowDrums/remarkable-mcp/issues/118).

### Native-ink marks

`remarkable_mark_pdf` draws marks into the PDF, which only carries RGB. On the
Paper Pro, PDF colors are dithered (speckled), while the tablet's own pen and
highlighter colors, stored as color ids, render in the screen's native colors.
Marks drawn as native ink would match the built-in colors exactly, on the
tablet and in the desktop app alike: ink stores a color id, which each app maps
to its own palette, while a PDF color is one RGB value for every viewer (marks
use the pure colors the tablet shows natively, which look more saturated in the
desktop app). What is known so far:

- Adding ink to a PDF page through the cloud works: attaching a tablet-written
  `.rm` file to an uploaded PDF as `{doc_id}/{page_id}.rm` (a new doc-index
  entry, `.content` unchanged) synced cleanly to the tablet and desktop app.
- `strokes.py` already appends strokes with a color id (`PenColor`) and pen
  (`Pen`) to an existing page without rewriting the tablet's bytes; a page with
  no ink would need a new `.rm` with a layer (see `notebooks.page_rm_bytes`).
- Ink coordinates on PDF pages follow the page frame `extract._page_frame`
  computes; marks' page fractions would map through it.
- Pen colors are ids 0, 1, 2, 6, 7 and 10-13; highlighter colors 3, 4, 5, 8,
  and 9 with an ARGB value for blue (`#BEEAFE`) and orange (`#FFC38C`).

Stroke ids are `(author, counter)`: `author` is a file-local number (one
byte in ids, 1-255; 0 is structure) mapped to a UUID by the `AuthorIdsBlock`;
`counter` is a varuint. Findings from cloud tests on a Paper Pro:

- A stroke appended through the cloud under the tablet's own author, with the
  next counter, synced and appeared, placed exactly as `_page_frame` predicts.
- **Collision:** while the tablet was offline, it and the cloud each added a
  stroke with the same id `(1, 18)`. After syncing, each side kept its own
  stroke and dropped the other's, silently: the tablet and the cloud (and the
  desktop app) disagreed, with no error. Never write under another device's
  author.
- **How a second writer joins:** the desktop app, editing typed text, added
  itself as author `2` (its own UUID) to the existing `AuthorIdsBlock`, used
  counter `max over all ids + 1`, and changed only the author, text and
  page-info blocks; every other block stayed byte-identical. rmscene re-encodes
  the `AuthorIdsBlock` exactly.

- **Own author, tested:** strokes appended through the cloud under a separate
  author (its UUID added to the `AuthorIdsBlock` as `2`, every other block
  copied byte for byte) synced and appeared. With the tablet offline, it drew
  `(1, 18)` while the cloud added `(2, 18)`; after syncing, both strokes were
  on the tablet and in the cloud, and the tablet kept the new author entry.

Plan (validated above): one random UUID per installation, stored in the config; reuse its
author number in a file or take the lowest free one; counter = max over all ids
+ 1; replace only the `AuthorIdsBlock` (add the entry) and append strokes,
copying every other block's bytes. The MCP's strokes are then those with its
author, so removing them needs no separate record. `remarkable_author` (SSH)
still writes under the tablet's author and should move to this too. Text and
LaTeX would stay in the PDF or be drawn as strokes.

### Export

Reusable PDF and Markdown export for one document is available through
`remarkable_export`. Reuse that primitive for folder and filter batching next. See
[#27](https://github.com/SamMorrowDrums/remarkable-mcp/issues/27).

## Search and OCR

### Persistent indexing

Build a local full-text index with incremental updates. Semantic search should
remain optional and use the same indexed chunks. See
[#26](https://github.com/SamMorrowDrums/remarkable-mcp/issues/26).

### Persistent cache

Cache OCR and extracted content with bounded storage, explicit invalidation, and
corruption recovery. See
[#28](https://github.com/SamMorrowDrums/remarkable-mcp/issues/28).

### OCR providers

Unify the existing Google Vision and Tesseract backends behind one provider
interface before adding more integrations. See
[#25](https://github.com/SamMorrowDrums/remarkable-mcp/issues/25).

## Workflow integrations

### Processed-page marker

Define an idempotent on-device marker for pages that have already been exported
or reviewed. See
[#24](https://github.com/SamMorrowDrums/remarkable-mcp/issues/24).

### Obsidian synchronization

Define stable identity, path mapping, conflict handling, and dry-run behavior
before implementing synchronization. See
[#158](https://github.com/SamMorrowDrums/remarkable-mcp/issues/158).

## Non-goals

- Firmware modification
- DRM bypass
- Subscription circumvention
- Silent overwrite or deletion during synchronization

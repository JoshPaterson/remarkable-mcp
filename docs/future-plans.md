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
Marks drawn as native ink would match the built-in colors exactly. What is
known so far:

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

Open questions: ink is erasable on the tablet and edited by both sides, so CRDT
ids must not collide with the tablet's (the cloud copy test reused tablet
bytes; generated strokes have not been synced through the cloud yet); the tool
needs a record of which strokes it added so it can remove them; text and LaTeX
would stay in the PDF or be drawn as strokes.

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

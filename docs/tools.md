# MCP Tools Reference

This document describes the read and render tools. Write tools are listed in the
[README](../README.md#write-tools-by-transport).

## Overview

| Tool | Purpose |
|------|---------|
| [`remarkable_read`](#remarkable_read) | Read and search document content |
| [`remarkable_browse`](#remarkable_browse) | Navigate folders and find documents |
| [`remarkable_search`](#remarkable_search) | Search across multiple documents |
| [`remarkable_recent`](#remarkable_recent) | Get recently modified documents |
| [`remarkable_status`](#remarkable_status) | Check connection status |
| [`remarkable_image`](#remarkable_image) | Get page images (PNG or SVG) |
| [`remarkable_page_layout`](#remarkable_page_layout) | Find text lines, ink and free space on a page |
| [`remarkable_export`](#remarkable_export) | Export documents as PDF or Markdown |

These tools never modify tablet content and return structured next-step hints.
`remarkable_export` additionally creates a bounded temporary file on the MCP server
host, so its MCP annotation is `readOnlyHint=false` even under tablet read-only mode.

## Root Path Filtering

All tools respect the `REMARKABLE_ROOT_PATH` environment variable. When configured, operations are scoped to that folder:

```json
{
  "env": {
    "REMARKABLE_ROOT_PATH": "/Work"
  }
}
```

With this configuration:
- Paths in responses are relative to the root (e.g., `/Work/Project` appears as `/Project`)
- Documents outside the root are not accessible, to read and write tools alike;
  a bare name only matches documents inside the root
- Paths sent to tools are root-relative too, including destination and parent
  folders of write tools (`/` is the root folder itself)
- Trashed items cannot be addressed by a `/trash/...` path while a root is set
- `remarkable_status()` shows the configured root and document count within that folder

---

## remarkable_read

**Read and extract text from a document.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `content_type` | string | `"text"` | What content to extract |
| `part` | int | `1` | Text part to read (not a tablet page); the old name `page` still works |
| `grep` | string | `None` | Regex to find in the whole document; returns each match with context and tablet page |
| `include_ocr` | bool | `False` | Enable OCR for handwritten content |

### Content Types

- **`"text"`**: source text plus annotations, highlights, and typed text (default)
- **`"raw"`**: original PDF/EPUB text without annotations
- **`"annotations"`**: highlights, typed notebook text, and OCR content

### Examples

```python
# Read first page of a document
remarkable_read("Meeting Notes")

# Read the third text part
remarkable_read("Research Paper.pdf", part=3)

# Find a keyword: every match with its context and tablet page
remarkable_read("Project Plan", grep="deadline")

# Get only annotations and highlights
remarkable_read("Book.pdf", content_type="annotations")

# Enable OCR for handwritten notes
remarkable_read("Journal", include_ocr=True)

# Read by full path
remarkable_read("/Work/Projects/Q4 Planning")
```

### Response Format

```json
{
  "document": "Meeting Notes",
  "path": "/Work/Meeting Notes",
  "file_type": "notebook",
  "content_type": "text",
  "content": "Extracted text content...",
  "part": 1,
  "page": 1,
  "total_pages": 5,
  "total_pages_known": true,
  "parts": 2,
  "content_pages": 2,
  "total_chars": 12500,
  "more": true,
  "next_part": 2,
  "next_page": 2,
  "modified": "2025-11-28T10:30:00Z",
  "_hint": "Part 1/2; document has 5 physical page(s). Next: remarkable_read('Meeting Notes', part=2)"
}
```

`page`, `content_pages` and `next_page` repeat `part`, `parts` and `next_part`
for older clients.

With `grep`, `content` holds the matches of this part, each prefixed with its
tablet page, and the response adds:

```json
{
  "grep": "deadline",
  "grep_matches": 3,
  "matches": [
    {"page": 4, "text": "...the deadline for the draft is Friday..."},
    {"page": 9, "text": "...moved the deadline to..."}
  ]
}
```

`grep` works the same for notebooks (their handwriting with `include_ocr=True`
or typed text) and for PDF and EPUB text. `page` is the tablet page (PDF page
numbers are mapped to tablet pages); it is `null` for text without pages, such
as EPUB text. Matches are split into parts like the text.

### Highlight colors

Text highlights are listed per page with their highlighter color, e.g.
`• [pink] The line of apsides advances...`. For `text` and `annotations`
reads, the response also carries a `highlights` list:

```json
"highlights": [
  {"page": 1, "text": "...", "color": "blue", "color_id": 9, "color_hex": "#beeafe"}
]
```

`color` is a name (yellow, green, pink, gray, blue, orange, ...). Color id 9
is a custom color named from the stored ARGB value, or `#rrggbb` when the value
is not a known highlighter color. `color_hex` is set only when the tablet stores
an ARGB value.

Each highlight also gives its position, as `[left, top, right, bottom]` from
the page's top-left corner: `rects_mm`, one box per line or piece; `bbox_mm`,
the box around all of them; and `region`, that box as fractions of the page,
which can be passed to `remarkable_image(region=...)`. The text output shows
the region after the text:

```
  • [pink] Estimate the remainder for a Taylor series   region [0.177, 0.206, 0.479, 0.219]
```

Highlight text can be wrong: it often misses mathematical symbols and
equations, or comes back short, garbled or with gaps. The region is reliable.
When the exact highlighted content matters, render the highlight's `region`
with `remarkable_image` (e.g. at `dpi=300`).

Highlighter strokes that did not snap to text (freehand highlighter ink, often
over a display equation) are summarized per page, e.g. `Page 2: 2 highlighter
strokes (pink, yellow)`, listed as `~ [pink] highlighter stroke, region [...]`,
and
returned as `highlighter_strokes` with `page`, the same color fields, and the
stroke's extent including the pen width, as `[left, top, right, bottom]` from
the page's top-left corner: `bbox_mm` in millimetres and `region` as fractions
of the page. `region` can be passed to `remarkable_image(region=...)` to view
the marked area. PDF-backed pages are measured on their PDF page. Other pen
strokes are summarized as "handwritten notes".

Page tags from the document's `.content` file are shown on their page, e.g.
`Page 3: handwritten notes; page tags: test-page`, and returned as
`page_tags: [{"page": 3, "name": "test-page"}]`.

Pages with more than one layer, or a hidden layer, list each layer with its
visibility and contents, and highlights and highlighter strokes name their
layer:

```
Page 3: handwritten notes; page tags: test-page
  Layer 1: 2 pen strokes (black, blue)
  Layer 2: 1 pen stroke (red)
  Layer 3 [hidden]: 1 pen stroke (green)
```

The same data is returned as `layers: [{"page": 3, "layers": [...]}]`.

Images pasted onto a page with the tablet's capture feature are listed per
page, e.g. `Page 4: handwritten notes, 1 pasted image` followed by
`+ pasted image, region [left, top, right, bottom]`, and returned as `images`
with `page`, `file`, `layer`, `bbox_mm` and `region`. Ink and highlights
copied inside a capture are part of the image, so they are not in the stroke
or highlight lists; read them from the render. `remarkable_image` draws
captures where they were placed (moved, resized and rotated ones included) and
in the tablet's stacking order: within a layer, items are drawn in the order
they were placed, so ink written over an image covers it, a later capture
covers an earlier one, and an image that is moved comes to the top. Captures
are opaque and hide what is beneath them; captures on hidden layers follow the
`layers` selection like ink.

The annotation list is a summary: handwriting is reported per page but not
transcribed without OCR. When reviewing the user's work, render each page with
`remarkable_image` as well.

### Page templates

Notebook pages name their template (page background) in the document's
`.content` file. Non-blank templates are listed under `--- Page templates ---`
and returned as `page_templates: [{"page": 1, "template": "P Dots S"}]`.
Templates are not part of the document, so `remarkable_image` does not draw
them: notebook pages render on a plain background.

### Typed text

Typed text on notebook pages (keyboard or type tool) comes first in
`content`, before the annotated pages. A highlight on typed text is listed like
a PDF highlight, with its text and region.

Typed text is drawn in Noto Sans. The tablet uses its own
font, reMarkable Sans, which cannot be included; Noto Sans has nearly the same
letter widths, so words land within about 2% of where the tablet puts them.
Where typed text is highlighted, the line is moved sideways (and squeezed if
needed) so the highlighted words sit centered across the tablet's highlight
box, as they do on the tablet.

### Marks

PDFs marked with `remarkable_mark_pdf` (grades, notes, check marks and other
marks drawn into the PDF) keep the PDF before marking inside them. Document
text comes from that original, so `content_type="raw"` never includes marks.
With `"text"` and `"annotations"` the marks are listed under
`--- Marks (added with remarkable_mark_pdf) ---`, e.g.
`q3-note (page 1, red, text): Orange is a secondary color.`, and returned as
`marks: [{"id", "page", "kind", "shape" | "text" | "latex" | "path" | "format",
"color", "region"}]`, with lines and arrows giving `from` and `to` instead of a
region. Regions are page fractions, as for `remarkable_image`'s `region`. A
mark with a cover layer adds `[cover 'Mark cover': hidden on tablet]` to its
line and `"cover": {"layer", "state"}` to its entry; the state is `hidden on
tablet`, `shown on tablet` or `cover erased on tablet`. The state follows the
cover layer's visibility, which the user changes on the tablet and the write
tool `remarkable_layers` changes from here.

Marks are the agent's, not the user's ink. Erasable marks and covers are
written as tablet ink, so the per-page lines also count them as strokes or
highlights.

A PDF with an outline (table of contents) also returns `outline: [{"level",
"title", "page"}]` with tablet page numbers (`page` is null for an entry that
points nowhere). `remarkable_pdf_outline` lists, sets and removes it.

### Handwriting OCR

With `include_ocr`, handwriting comes back by page, headed `--- Page N ---`
with the document's page number, one line per line of writing in reading
order. On pages with several visible layers each line ends with the layers
that drew it, e.g. `x = 4  [Layer 2]`; hidden layers are not read. The same
lines are returned as `handwriting: [{"page", "lines": [{"text", "layers",
"confidence", "low_confidence"}]}]`.

Tesseract reads handwriting poorly. Its lines carry a `confidence` (0-100),
lines below 80 are marked `(low confidence)`, and the response adds an
`ocr_note` saying the text is approximate and which pages to view with
`remarkable_image` before relying on it. Google Vision results carry no such
note.

### Scanned pages

A PDF page that is a scan (mostly image, no text) yields no text. Such pages
are listed as `pages_without_text` (tablet page numbers), and the hint points
to `remarkable_pdf_ocr`, which writes OCR text into the PDF.

### Smart Features

- **Auto-OCR**: If a notebook has no typed text and `include_ocr=False`, OCR is automatically enabled and you're notified via `_ocr_auto_enabled: true`
- **Fuzzy matching**: If the exact document isn't found, similar names are suggested
- **Path resolution**: Works with document names or full paths

### Pagination

- `total_pages` reports physical document pages, matching `remarkable_image`,
  and `total_pages_known` says whether that count was available.
- `parts` (also `content_pages`) reports text parts. PDF/EPUB source text
  uses ~8000-character parts; notebook OCR gives one part per OCR'd page, which
  may be fewer than the tablet pages when blank pages have no text.
- `part`, `more`, and `next_part` refer to those parts, not to tablet pages.
- Raw PDF reads count pages from the same source bytes used for text extraction
  and do not fetch the full document archive. A raw EPUB may need that archive
  for its physical page count; if the archive is unavailable, the extracted
  content still succeeds with `total_pages: null`, `total_pages_known: false`,
  and a `page_count_note`.
- On older USB firmware that returns a native PDF instead of an rmdoc archive,
  `content_type="text"` returns source text without annotations, while
  `content_type="annotations"` returns `annotations_not_available`.

When `more: true`, use the `part` parameter to continue reading.

---

## remarkable_browse

**Navigate your document library.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | `"/"` | Folder path to browse |
| `query` | string | `None` | Search documents by name |
| `tags` | list[string] | `None` | Match documents carrying any listed tag as a document or page tag (case-insensitive). Searches the folder and all its subfolders. |

### Examples

```python
# List root folder
remarkable_browse("/")

# Browse a specific folder
remarkable_browse("/Work/Projects")

# Search for documents by name
remarkable_browse(query="meeting")

# Combine path and search
remarkable_browse("/Work", query="report")

# Documents tagged "project" or "active" anywhere under /Work
remarkable_browse("/Work", tags=["project", "active"])
```

### Response Format

```json
{
  "path": "/Work",
  "folders": [
    {"name": "Projects", "path": "/Work/Projects"},
    {"name": "Archive", "path": "/Work/Archive"}
  ],
  "documents": [
    {
      "name": "Weekly Report",
      "path": "/Work/Weekly Report",
      "type": "pdf",
      "modified": "2025-11-28T10:30:00Z"
    }
  ],
  "_hint": "Found 2 folders, 1 document. To read: remarkable_read('Weekly Report')."
}
```

### Smart Features

- **Auto-redirect**: If `path` points to a document instead of a folder, automatically returns the document content (like calling `remarkable_read`)
- **Case-insensitive**: Paths and searches are case-insensitive

`modified` is the last edit time recorded by the tablet or app that made the
change, not the time of the last sync. In cloud mode the tablet can upload page
content later without changing it.

---

## remarkable_search

**Read several documents in one call, optionally keeping only matching lines.**

It picks documents first, then reads each like `remarkable_read`: candidates
are documents whose names contain `query`, inside `folder` (and its
subfolders) and with any of `tags` when given. At least one of `query`,
`folder` or `tags` is needed; without `query`, every document in the folder or
with the tags is a candidate, so `grep` searches content regardless of names.
The first `limit` candidates by path are read.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | `None` | Text the document names must contain |
| `grep` | string | `None` | Regex to find in each document's text |
| `limit` | int | `5` | Documents to read (max: 20) |
| `include_ocr` | bool | `False` | Enable OCR for handwritten content |
| `tags` | list[string] | `None` | Match documents carrying any listed tag (case-insensitive) |
| `folder` | string | `None` | Only documents in this folder and its subfolders |

### Examples

```python
# Find documents with "meeting" in the name
remarkable_search("meeting")

# Find "action items" inside meeting documents
remarkable_search("meeting", grep="action items")

# Search journals for a specific topic
remarkable_search("journal", grep="project idea", include_ocr=True)

# Search only tagged documents
remarkable_search("project", tags=["work"])

# Search the content of every document in a folder
remarkable_search(folder="/Work", grep="budget", limit=20)
```

### Response Format

```json
{
  "query": "meeting",
  "grep": "action items",
  "count": 3,
  "candidates": 3,
  "documents": [
    {
      "name": "Team Meeting Nov",
      "path": "/Work/Team Meeting Nov",
      "modified": "2025-11-28T10:30:00Z",
      "content": "...context around matches...",
      "total_pages": 2,
      "content_pages": 1,
      "grep_matches": 5,
      "match_pages": [1, 2],
      "truncated": true
    }
  ],
  "_hint": "Read 3 document(s); 2 have 12 match(es) for 'action items'. To read more: remarkable_read('/Work/Team Meeting Nov')."
}
```

### Limits

- At most 20 documents per search (5 by default); `candidates` says how many
  matched, and the hint says when some were not read
- Content is truncated to ~2000 characters per document
- Designed for quick discovery, use `remarkable_read` for full content

---

## remarkable_recent

**Get recently modified documents.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | `10` | Maximum documents to return |
| `include_preview` | bool | `False` | Include text preview for each document |

### Examples

```python
# Get last 10 documents
remarkable_recent()

# Get last 5 with previews
remarkable_recent(limit=5, include_preview=True)
```

### Response Format

```json
{
  "count": 5,
  "documents": [
    {
      "name": "Meeting Notes",
      "path": "/Work/Meeting Notes",
      "modified": "2025-11-28T10:30:00Z",
      "preview": "First 200 characters of content..."
    }
  ],
  "_hint": "Showing 5 recent documents. To read one: remarkable_read('Meeting Notes')."
}
```

### Notes

- With `include_preview=True`, limit is capped at 10 (performance)
- Notebooks skip preview (require OCR), showing `preview_skipped` instead
- PDFs and EPUBs have fast text extraction for previews

---

## remarkable_status

**Check connection and authentication status.**

### Parameters

None.

### Examples

```python
remarkable_status()
```

### Response Format

```json
{
  "authenticated": true,
  "transport": "ssh",
  "connection": "SSH to root@10.11.99.1:22",
  "status": "connected",
  "document_count": 142,
  "root_path": "/Work",
  "ocr_backend": "google",
  "_hint": "Connection healthy. Filtered to root: /Work. Use remarkable_browse('/') to explore your library."
}
```

### Fields

| Field | Description |
|-------|-------------|
| `authenticated` | Whether authentication succeeded |
| `transport` | `"local-dir"`, `"cloud"`, `"usb-web"`, or `"ssh"` |
| `connection` | Connection details |
| `document_count` | Total documents in library (filtered by root if configured) |
| `write_enabled` | Whether write tools are enabled (the default; `false` only with `--read-only`) |
| `capabilities` | Effective capabilities for the active transport (read/render/export/upload/mkdir/move/rename/tag/mark_pdf/trash) |
| `capabilities_by_transport` | The full per-transport capability matrix |
| `root_path` | Configured root path filter (only present if set) |
| `ocr_backend` | Which OCR backend is configured |

---

## remarkable_image

**Get a PNG or SVG image of a specific page.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `page` | int | `1` | Page number (1-indexed) |
| `background` | string | `"#FBFBFB"` | Background color (hex RGB or RGBA) |
| `output_format` | string | `"png"` | Output format: `"png"` or `"svg"` |
| `include_ocr` | bool | `False` | Enable OCR on the image |
| `compatibility` | bool | `False` | Return resource URI instead of embedded resource |
| `render_merged` | bool | `True` | PNG: draw PDF-backed pages over their PDF page (`True`), or the annotations alone (`False`) |
| `layers` | string | `"visible"` | Layers to draw: `"visible"` (what the tablet shows), `"all"` (include hidden layers), or one layer's name, e.g. `"Layer 3"` |
| `dpi` | int \| null | `None` | PNG resolution (36-600 dots per inch of the physical page). Renders the whole page instead of cropping to the ink; use 300 to read small handwriting |
| `region` | list[float] \| null | `None` | PNG crop box `[left, top, right, bottom]` as fractions of the page, e.g. `[0, 0.5, 1, 0.75]` |
| `marks` | bool | `True` | Draw marks added with `remarkable_mark_pdf`; `False` shows the page as it was before marking, with the user's ink |

### Background Colors

- **`"#FBFBFB"`**: default reMarkable paper color
- **`"#FFFFFF"`**: white
- **`"#00000000"`**: fully transparent
- **`"#80008080"`**: semi-transparent purple

**Tip:** Set `REMARKABLE_BACKGROUND_COLOR` environment variable to change the default for all image operations.

### Examples

```python
# Get first page with default paper background
remarkable_image("UI Mockup")

# Get specific page
remarkable_image("Meeting Notes", page=2)

# White background
remarkable_image("Diagram", background="#FFFFFF")

# Transparent background for compositing
remarkable_image("Logo Sketch", background="#00000000")

# SVG format for editing in design tools
remarkable_image("Wireframe", output_format="svg")

# SVG with custom background
remarkable_image("Sketch", output_format="svg", background="#F0F0F0")

# Enable OCR
remarkable_image("Handwritten Notes", include_ocr=True)

# Compatibility mode: return resource URI instead of embedded resource
remarkable_image("Diagram", compatibility=True)

# PDF-backed PNG pages merge the source page and annotations automatically
remarkable_image("Research Paper", page=3)

# Return only the reMarkable annotation layer
remarkable_image("Research Paper", page=3, render_merged=False)
```

### Response Format

For PNG format, returns an embedded image resource that can be displayed inline.

For SVG format, returns an embedded text resource with the SVG content.

When `compatibility=True`, a PNG is not embedded. The response is a JSON
object with a link to a temporary resource holding the rendered image, which
expires after 15 minutes:

```json
{
  "mime_type": "image/png",
  "page": 1,
  "total_pages": 5,
  "resource_uri": "remarkableexport:///png/3f2a...",
  "size": 182311,
  "expires_at": "2026-10-12T09:15:00+00:00",
  "merged": true,
  "render_source": "strokes",
  "_hint": "Page 1/5 as a PNG resource; read resource_uri before it expires..."
}
```

When `include_ocr=True`, OCR text is included in the response. Google Vision is
used when configured; otherwise OCR runs locally with Tesseract.

### Notes

- Native notebooks retain their existing stroke and blank-page rendering.
  Notebook templates are not drawn (see Page templates under remarkable_read).
- Pasted images (captures) are drawn as placed, in the tablet's stacking order
  (see remarkable_read).
- Regions from `remarkable_read` line up with the render on PDF-backed pages
  and notebooks.
- Ink uses the colors the desktop app draws; pen strokes use their stored
  widths.
- PDF-backed PNG pages merge the source page and annotations by default. Explicit
  `render_merged=False` preserves annotation-only access, including user-added
  PDF pages that have no underlay.
- SVG is always annotation-only.
- Compatibility responses preserve the same merge choice and expose `merged`.
- If older USB firmware returns only a native PDF export, PNG still renders via
  that export. SVG and explicit `render_merged=False` require the unavailable
  rmdoc annotation layer and return clear errors.
- RGBA colors (8-digit hex) allow transparency control

---

## remarkable_page_layout

**Find where things are on one tablet page.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | required | Document name or path |
| `page` | int | 1 | Tablet page number |

### Response Format

Regions are page fractions `[left, top, right, bottom]`, the frame
`remarkable_image`'s `region` takes:

- `text_lines`: the PDF's printed lines (before marks), each with `text` and
  `region`; empty for notebooks
- `ink`: boxes around the user's handwriting on visible layers, roughly a word
  or a line each (ink the MCP wrote, such as covers, is left out)
- `free`: the largest empty regions, clear of ink, text and marks
- `layers`: the page's layers, bottom first, with `visible` and `selected`
  (`remarkable_layers` shows and hides them, cloud mode)

Works in every transport and in read-only mode. `remarkable_mark_pdf`'s
`layout_page` returns the same layout. Marks and a PDF's outline are listed by
`remarkable_read` (`marks` and `outline` in its response).

---

## remarkable_export

**Export one document as a reusable PDF or Markdown resource.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `output_format` | `"pdf"` \| `"markdown"` | `"pdf"` | Export format |
| `pdf_mode` | `"merged"` \| `"annotations"` | `"merged"` | PDF-only rendering mode |
| `include_ocr` | bool | `False` | Run the existing OCR path for Markdown |
| `pages` | int \| string | all pages | PDF only: one page (`5`) or ranges (`"5-9,12"`) |
| `save_to` | string | none | Absolute path of a `.pdf` or `.md` file to save the export at, inside a folder listed in `REMARKABLE_EXPORT_DIRS` |
| `overwrite` | bool | `False` | With `save_to`: replace an existing file that is an earlier export of the same document |
| `folder` | string | none | Export every document in this folder and its subfolders (needs `save_to` as a folder) |
| `tags` | list | none | Export documents with any of these tags (needs `save_to` as a folder) |

### Delivery and lifecycle

The tool returns a small JSON summary plus an MCP `ResourceLink`. It does not
embed PDF/Markdown bytes in the tool response and does not accept or invent a host
output path. The server writes the generated file beneath a private temporary
directory:

- links expire 15 minutes after creation;
- at most eight exports are retained, with least-recently-used eviction;
- expired and evicted links stop resolving;
- all managed exports are removed at server shutdown.

### Saving to a folder

With `save_to`, the export is saved at that path on the machine running the
server instead of being returned as a resource. Only paths inside the folders
listed in `REMARKABLE_EXPORT_DIRS` (separated by `:`) are accepted, checked
after resolving symlinks; any other path, or a save without that setting, is
refused. Missing folders are created, and the file is written under a
temporary name in the same folder and renamed into place. An existing file is
replaced only with `overwrite=True`, and only when it is an earlier export of the
same reMarkable document (its id is in a PDF export's keywords and a Markdown
export's front matter); nothing is ever deleted. The response gives
`saved_path`, `size`, `pages`, `sha256`, `status`, `failed_pages` and
`warnings`, so the copy can be checked. `pdf_mode` and `pages` work the same as
without `save_to`.

```json
"env": {
    "REMARKABLE_EXPORT_DIRS": "/Users/me/Library/Mobile Documents/com~apple~CloudDocs/Projects"
}
```

### Exporting a folder or tagged documents

Instead of `document`, give `folder` (every document in it and its
subfolders) and/or `tags` (documents with any of the tags; the whole library
without `folder`), with `save_to` a folder inside `REMARKABLE_EXPORT_DIRS`.
Subfolders are recreated under it and files are named after the documents
(characters a file name cannot hold become `-`; two documents with the same
name in one folder get the second's id appended). Each document is exported
on its own, so one failure does not stop the rest: the response lists `saved`
(each file's `saved_path`, `size`, `pages`, `sha256`, `status`, `warnings`)
and `failed` (with the error, such as `file_exists`). `overwrite` works per
file as above; `pages` is not available. At most 200 documents are exported
in one call.

```python
remarkable_export(folder="/Classes/Physics/Worksheets", tags=["Graded"],
                  save_to="/Users/me/Projects/Classes/Physics/Graded")
```

These local writes are why the tool declares `readOnlyHint=false`.
The reMarkable tablet and library are never modified, and the tool remains
available when the server runs with `--read-only`.

### PDF behavior

- Marks added with `remarkable_mark_pdf` in a named color are re-drawn in the
  desktop and browser apps' colors (the document itself keeps colors tuned for
  the tablet's screen). The embedded original and mark list are left out.
- Physical page order comes from reMarkable `.content` metadata.
- `pages` exports only the selected physical pages, numbered as in
  `remarkable_image`, in document order. The response lists them in
  `selected_pages`, with the document's page count in `document_pages`.
- `pdf_mode="merged"` combines mapped PDF underlays and annotations by default.
- Native notebooks use complete full-page ink/blank renders.
- `pdf_mode="annotations"` returns complete annotation layers only.
- A flattened native PDF (older USB firmware) remains exportable in merged mode,
  but cannot be separated into annotation-only layers.
- A page render failure is represented by a labeled placeholder at the same page
  ordinal and reported with `status: "partial"`; pages are never silently skipped.
- EPUB source pages use a transport-native PDF when one is available. Otherwise
  available annotation pages are exported with an explicit partial warning.

### Markdown behavior

Markdown starts with basic front matter containing the stable reMarkable document
UUID, title, path, source type, physical page count, modified time, tags, and
completeness. It then uses fixed sections:

1. Source text
2. Typed text
3. Annotations
4. Highlights
5. OCR
6. Export notes (only for partial exports)

Extracted text is emitted verbatim. The exporter does not claim to detect headings,
lists, paragraphs, links, or physical-page attribution that the existing extraction
result cannot prove. OCR is opt-in and uses the same configured backend and cache as
`remarkable_read`.

### Examples

```python
# Complete PDF (source underlay + annotations where available)
remarkable_export("Meeting Notes")

# Full-page annotation layers only
remarkable_export("Research Paper", pdf_mode="annotations")

# Markdown with handwriting OCR
remarkable_export("Journal", output_format="markdown", include_ocr=True)

# Export pages 3-5 and 9 only
remarkable_export("Lecture Notes", pages="3-5,9")

# Save into an allowed folder (REMARKABLE_EXPORT_DIRS)
remarkable_export("Week 3 worksheet",
                  save_to="/Users/me/Projects/Course/Worksheets/Week 3 - done.pdf")
```

### Response

```json
{
  "document": "Meeting Notes",
  "document_id": "11111111-2222-3333-4444-555555555555",
  "path": "/Work/Meeting Notes",
  "format": "pdf",
  "filename": "Meeting Notes.pdf",
  "size": 481203,
  "pages": 7,
  "status": "complete",
  "failed_pages": [],
  "warnings": [],
  "resource_uri": "remarkableexport:///pdf/opaque-resource-id",
  "expires_at": "2026-08-14T22:15:00+00:00",
  "temporary_local_file": true,
  "_hint": "Temporary PDF export ready as an MCP resource..."
}
```

Fetch and save the accompanying `ResourceLink` before it expires when durable
storage is required.

---

## Error Handling

All tools return structured errors with suggestions. An error reply sets the
MCP `isError` flag on the tool result; its text is the JSON object below:

```json
{
  "_error": {
    "type": "document_not_found",
    "message": "Document 'Meting Notes' not found",
    "suggestion": "Did you mean: 'Meeting Notes', 'Meeting Notes 2'?",
    "did_you_mean": ["Meeting Notes", "Meeting Notes 2"]
  }
}
```

Common error types:
- `document_not_found`: document does not exist (includes `did_you_mean`
  suggestions from inside the configured root)
- `folder_not_found`: a folder parameter names no folder (`did_you_mean`
  lists folders)
- `invalid_parameter`: a parameter is wrong; the message starts with the
  parameter name, e.g. `pages: ...` or `document: 'Course' is a folder, not a
  PDF document.` (also used for a notebook or EPUB given to a PDF-only tool)
- `authentication_failed`: token invalid or SSH connection failed
- `connection_error`: network or SSH connection issue

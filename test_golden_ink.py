"""Golden tests for the ink the MCP writes (covers, text highlights, erasable
marks): they pin the exact bytes and structure the tablet was seen to accept.

fixtures/tablet/ink_writes/ holds page files from tests on a reMarkable Paper
Pro (2026-10-19 to 21):

- tablet_highlight_page.rm: a page with a text highlight the tablet made.
- tablet_page_with_cover.rm: a page after the tablet synced the MCP's cover
  layer and the user wrote on it (two authors).
- tablet_after_erase.rm: a page after the tablet erased the MCP's stroke.
- accepted_cover_first_page.rm, accepted_cover_on_tablet_page.rm: files the
  MCP wrote (a first ink file, and a cover added to the tablet's file) that
  the tablet accepted.

golden/ holds the writers' output for fixed inputs. After an intended change
to the written format, regenerate it with ``GOLDEN_UPDATE=1 pytest
test_golden_ink.py`` and re-check the new output on a tablet.
"""

import io
import os
import struct
import uuid
from pathlib import Path

import pytest
from rmscene import HEADER_V6, read_blocks, write_blocks

from remarkable_mcp import cover_layers as cl
from remarkable_mcp import ink_marks, rm_ink
from remarkable_mcp import text_highlights as th

FIXTURES = Path(__file__).parent / "fixtures" / "tablet" / "ink_writes"
GOLDEN = FIXTURES / "golden"
AUTHOR = uuid.UUID("8efeea22-0545-4297-a2c6-c5b0e5e17c43")
A4 = (595.0, 842.0)
TABLET_FILES = [
    "tablet_highlight_page.rm",
    "tablet_page_with_cover.rm",
    "tablet_after_erase.rm",
    "accepted_cover_first_page.rm",
    "accepted_cover_on_tablet_page.rm",
]


def _raw_blocks(data: bytes):
    pos, out = len(HEADER_V6), []
    while pos < len(data):
        length = struct.unpack_from("<I", data, pos)[0]
        out.append((data[pos + 7], data[pos : pos + 8 + length]))
        pos += 8 + length
    return out


def _check_golden(name: str, data: bytes) -> None:
    path = GOLDEN / name
    if os.environ.get("GOLDEN_UPDATE"):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert path.exists(), f"missing golden {name}; run with GOLDEN_UPDATE=1"
    assert data == path.read_bytes(), f"{name} changed; see this file's docstring"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize("name", TABLET_FILES)
def test_tablet_blocks_re_encode_byte_for_byte(name):
    """The blocks the MCP rewrites (the author table, and strokes and text
    highlights it turns into tombstones) re-encode exactly, so rewriting one
    cannot alter what the tablet wrote. Other blocks are copied as raw bytes."""
    data = _read(name)
    for btype, raw in _raw_blocks(data):
        if btype not in (0x03, 0x05, 0x09):
            continue
        (block,) = read_blocks(io.BytesIO(HEADER_V6 + raw))
        buf = io.BytesIO()
        write_blocks(buf, [block], options={"version": "3.1"})
        assert buf.getvalue()[len(HEADER_V6) :] == raw, f"block type {btype:#x}"


def _rows():
    return [[[0.1, 0.1], [0.3, 0.1]], [[0.1, 0.11], [0.3, 0.11]]]


def _writes():
    """``{golden name: bytes}`` for each writer on fixed inputs."""
    tablet = _read("tablet_highlight_page.rm")
    first = rm_ink.first_page_rm(AUTHOR)
    cover, (placed,) = cl.edit_page(
        tablet, AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows(), "shown": False}]
    )
    parts = [{"start": 10, "length": 5, "text": "hello", "rect": (100, 200, 140, 212)}]
    highlight, _ = th.add_highlights(tablet, AUTHOR, A4, parts, "blue")
    named, _ = th.add_highlights(tablet, AUTHOR, A4, parts, "yellow", layer="Claude highlights")
    strokes = [
        {"points": [(100, 300), (150, 320), (200, 300)], "width": 1.5},
        ink_marks._dot((210, 300), 3.2),
    ]
    ink, ids = ink_marks.add_strokes(tablet, AUTHOR, A4, strokes, "fineliner", "red")
    erased = rm_ink.tombstone(ink, ids[:1])
    marker, _ = ink_marks.add_strokes(tablet, AUTHOR, A4, strokes[:1], "highlighter", "orange")
    return {
        "first_page.rm": first,
        "cover_on_tablet_page.rm": cover,
        "text_highlight.rm": highlight,
        "text_highlight_named_layer.rm": named,
        "erasable_strokes.rm": ink,
        "erasable_stroke_tombstoned.rm": erased,
        "erasable_highlighter.rm": marker,
    }


@pytest.mark.parametrize("name", list(_writes()))
def test_writers_match_their_golden_output(name):
    _check_golden(name, _writes()[name])


@pytest.mark.parametrize("name", list(_writes()))
def test_writes_keep_the_tablets_blocks(name):
    """Every block of the tablet's file is kept, in order and unchanged, except
    the author table (which gains the MCP's author)."""
    if name == "first_page.rm":
        return
    before = [raw for t, raw in _raw_blocks(_read("tablet_highlight_page.rm")) if t != 0x09]
    after = [raw for t, raw in _raw_blocks(_writes()[name]) if t != 0x09]
    remaining = iter(after)
    for raw in before:
        assert any(raw == candidate for candidate in remaining), "a tablet block changed"


def test_first_ink_file_has_the_accepted_structure():
    """A first ink file has the blocks the accepted one had, before its
    strokes: authors, migration, page info, SceneInfo selecting Layer 1, the
    tree, and no typed-text block."""
    accepted = [t for t, _ in _raw_blocks(_read("accepted_cover_first_page.rm"))]
    ours, (placed,) = cl.edit_page(
        None, AUTHOR, A4, add=[{"layer": "Mark cover", "rows": _rows(), "shown": False}]
    )
    types = [t for t, _ in _raw_blocks(ours)]
    assert [t for t in types if t != 0x05] == [t for t in accepted if t != 0x05]
    assert 0x07 not in types
    blocks = list(read_blocks(io.BytesIO(ours)))
    info = next(b for b in blocks if type(b).__name__ == "SceneInfo")
    assert (info.current_layer.value.part1, info.current_layer.value.part2) == rm_ink.LAYER_1


def test_cover_layer_sits_at_the_bottom_as_accepted():
    accepted = _read("accepted_cover_on_tablet_page.rm")
    for data in (accepted, _writes()["cover_on_tablet_page.rm"]):
        layers = rm_ink.page_layers(data)
        assert layers[0]["id"][0] == rm_ink.author_number(data, AUTHOR)
        assert layers[-1]["name"] == "Layer 1"


def test_tablet_keeps_the_mcp_author_and_tombstones():
    """After the tablet synced, the MCP's author and layer survive, and an
    erased MCP stroke is a tombstone, not removed bytes."""
    synced = _read("tablet_page_with_cover.rm")
    assert rm_ink.author_number(synced, AUTHOR) is not None
    assert [layer["name"] for layer in rm_ink.page_layers(synced)][0] == "Mark cover"
    erased = _read("tablet_after_erase.rm")
    blocks = [
        b for b in read_blocks(io.BytesIO(erased)) if type(b).__name__ == "SceneLineItemBlock"
    ]
    assert any(b.item.value is None for b in blocks)


def _layer_hidden():
    """The tablet's page with the MCP's cover layer shown, and that page with
    the layer hidden."""
    tablet = _read("tablet_page_with_cover.rm")
    writer = rm_ink.PageWriter(tablet, AUTHOR)
    assert writer.set_layer_visible(writer.find_layer("Mark cover"), False)
    return tablet, writer.data


def test_layer_toggle_matches_its_golden_output():
    _check_golden("layer_hidden.rm", _layer_hidden()[1])


def test_layer_toggle_keeps_the_tablets_blocks():
    """Only the layer's node block changes, and it keeps its header bytes;
    the tablet's node blocks re-encode with the same body."""
    tablet, hidden = _layer_hidden()
    before, after = _raw_blocks(tablet), _raw_blocks(hidden)
    assert [t for t, _ in before] == [t for t, _ in after]
    changed = [(old, new) for (_, old), (_, new) in zip(before, after) if old != new]
    assert len(changed) == 1
    old, new = changed[0]
    assert old[7] == 0x02 and new[4:8] == old[4:8]
    for btype, raw in before:
        if btype != 0x02:
            continue
        (block,) = read_blocks(io.BytesIO(HEADER_V6 + raw))
        buf = io.BytesIO()
        write_blocks(buf, [block], options={"version": "3.1"})
        assert buf.getvalue()[len(HEADER_V6) + 8 :] == raw[8:]
    assert [(layer["name"], layer["visible"]) for layer in rm_ink.page_layers(hidden)] == [
        ("Mark cover", False),
        ("Layer 1", True),
    ]

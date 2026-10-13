"""Images pasted onto a page with the tablet's capture feature (block types 14, 15)."""

import io
import json
import uuid
import zipfile

from rmscene.tagged_block_common import CrdtId, LwwValue
from rmscene.tagged_block_writer import TaggedBlockWriter

from remarkable_mcp.extract import (
    _render_rm_v6_to_svg,
    _v6_blocks,
    extract_text_from_document_zip,
)
from remarkable_mcp.images import pasted_images
from test_annotations import _read_annotations, build_rm, stroke

LAYER_1 = CrdtId(0, 10)  # first layer node id written by build_rm
LAYER_2 = CrdtId(0, 11)
KEY = uuid.UUID("65094cb5-d3fb-36b7-0341-4f3c05f057a4").bytes
FILE_NAME = "0bf3d84b-bb3b-4f87-aa67-89b55aef9144.png"
# A 917 x 152 unit quad, as written by firmware for a pasted capture.
QUAD = [
    (-453.5, 830.0, 0.0, 0.0),
    (463.5, 830.0, 1.0, 0.0),
    (463.5, 982.0, 1.0, 1.0),
    (-453.5, 982.0, 0.0, 1.0),
]
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201"
    "5a3a8c2b0000000049454e44ae426082"
)


def catalog_block(entries) -> bytes:
    buf = io.BytesIO()
    writer = TaggedBlockWriter(buf)
    with writer.write_block(14, 3, 3):
        with writer.write_subblock(1):
            writer.data.write_varuint(len(entries))
            for key, name in entries:
                with writer.write_subblock(0):
                    writer.data.write_bytes(key)
                    writer.write_lww_string(1, LwwValue(CrdtId(1, 16), name))
                    with writer.write_subblock(2):
                        writer.write_id(1, CrdtId(0, 0))
                        with writer.write_subblock(2):
                            writer.write_byte(1, 0)
    return buf.getvalue()


def image_block(
    item_id: CrdtId,
    parent_id: CrdtId,
    key=KEY,
    quad=QUAD,
    deleted=0,
    left: CrdtId = CrdtId(0, 0),
) -> bytes:
    buf = io.BytesIO()
    writer = TaggedBlockWriter(buf)
    with writer.write_block(15, 2, 2):
        writer.write_id(1, parent_id)
        writer.write_id(2, item_id)
        writer.write_id(3, left)
        writer.write_id(4, CrdtId(0, 0))
        writer.write_int(5, deleted)
        if quad is not None:
            with writer.write_subblock(6):
                writer.data.write_uint8(7)
                with writer.write_subblock(1):
                    writer.write_id(1, CrdtId(1, 53))
                    with writer.write_subblock(2):
                        writer.data.write_bytes(key)
                writer.write_id(2, CrdtId(1, 52))
                with writer.write_subblock(3):
                    writer.data.write_varuint(4 * len(quad))
                    for vertex in quad:
                        for value in vertex:
                            writer.data.write_float32(value)
                with writer.write_subblock(4):
                    writer.data.write_varuint(6)
                    for index in (0, 1, 2, 2, 3, 0):
                        writer.data.write_uint32(index)
                writer.write_id(5, CrdtId(1, 17))
    return buf.getvalue()


def page_with_image(*extra_blocks: bytes, layers=None) -> bytes:
    layers = layers or [("Layer 1", True, [stroke(0, 17, [(0, 100), (50, 100)])])]
    return (
        build_rm(layers)
        + catalog_block([(KEY, FILE_NAME)])
        + image_block(CrdtId(1, 17), LAYER_1, quad=None, deleted=2)
        + image_block(CrdtId(1, 19), LAYER_1)
        + b"".join(extra_blocks)
    )


def write_page(tmp_path, data: bytes, with_png: bool = True):
    rm_file = tmp_path / "doc" / "p1.rm"
    rm_file.parent.mkdir(parents=True, exist_ok=True)
    rm_file.write_bytes(data)
    if with_png:
        (tmp_path / "doc" / "p1").mkdir(exist_ok=True)
        (tmp_path / "doc" / "p1" / FILE_NAME).write_bytes(PNG)
    return rm_file


def test_pasted_images_parsed_from_scene_blocks(tmp_path):
    images = pasted_images(_v6_blocks(write_page(tmp_path, page_with_image())))

    assert list(images) == [CrdtId(1, 19)]
    image = images[CrdtId(1, 19)]
    assert image.file_name == FILE_NAME
    assert image.parent_id == LAYER_1
    assert image.vertices == QUAD
    assert image.bbox() == [-453.5, 830.0, 463.5, 982.0]


def test_deleted_pasted_image_is_dropped(tmp_path):
    data = page_with_image(image_block(CrdtId(1, 19), LAYER_1, quad=None, deleted=1))

    assert pasted_images(_v6_blocks(write_page(tmp_path, data))) == {}


def test_render_draws_pasted_image_from_page_folder(tmp_path):
    svg = _render_rm_v6_to_svg(write_page(tmp_path, page_with_image()))

    assert '<image width="1" height="1" preserveAspectRatio="none"' in svg
    assert 'transform="matrix(917.000 0.000 0.000 152.000 -453.500 830.000)"' in svg
    assert 'href="data:image/png;base64,' in svg


def test_render_skips_image_without_file(tmp_path):
    svg = _render_rm_v6_to_svg(write_page(tmp_path, page_with_image(), with_png=False))

    assert "<image" not in svg


def test_image_on_hidden_layer_follows_layer_selection(tmp_path):
    layers = [
        ("Layer 1", True, [stroke(0, 17, [(0, 100), (50, 100)])]),
        ("Layer 2", False, []),
    ]
    data = (
        build_rm(layers) + catalog_block([(KEY, FILE_NAME)]) + image_block(CrdtId(1, 19), LAYER_2)
    )
    rm_file = write_page(tmp_path, data)

    assert "<image" not in _render_rm_v6_to_svg(rm_file)
    assert "<image" in _render_rm_v6_to_svg(rm_file, layers="all")


def test_extraction_reports_pasted_image(tmp_path):
    zip_path = tmp_path / "doc.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("doc.content", json.dumps({"pages": ["p0", "p1"]}))
        zf.writestr("doc/p1.rm", page_with_image(layers=[("Layer 1", True, [])]))
        zf.writestr(f"doc/p1/{FILE_NAME}", PNG)

    result = extract_text_from_document_zip(zip_path)

    page = result["annotated_pages"][0]
    assert page["page"] == 2
    assert page["has_handwriting"] is False
    assert page["layers"][0]["images"] == 1
    image = result["images"][0]
    assert image["page"] == 2
    assert image["file"] == FILE_NAME
    assert image["layer"] == "Layer 1"
    # x is centered on the 1404-unit page; y from the top of the 1872-unit page.
    assert image["region"] == [0.177, 0.443, 0.83, 0.525]
    assert "bbox" not in image


async def test_read_annotations_lists_pasted_image():
    data = await _read_annotations(
        {
            "typed_text": [],
            "highlights": [],
            "handwritten_text": None,
            "pages": 4,
            "page_ids": ["p1", "p2", "p3", "p4"],
            "annotated_pages": [
                {
                    "page": 4,
                    "page_id": "p4",
                    "has_handwriting": True,
                    "highlights": [],
                    "images": [
                        {
                            "file": FILE_NAME,
                            "layer": "Layer 1",
                            "bbox_mm": [27.7, 93.0, 130.4, 110.1],
                            "region": [0.176, 0.445, 0.831, 0.527],
                        }
                    ],
                    "layers": [{"name": "Layer 1", "visible": True, "images": 1}],
                }
            ],
            "images": [{"page": 4, "file": FILE_NAME}],
            "ocr_backend": None,
            "tags": [],
        }
    )

    assert "Page 4: handwritten notes, 1 pasted image" in data["content"]
    assert "  + pasted image, region [0.176, 0.445, 0.831, 0.527]" in data["content"]
    assert data["images"] == [{"page": 4, "file": FILE_NAME}]


# --- stacking order ---------------------------------------------------------


def _stroke_ids(data: bytes) -> list:
    from rmscene.scene_stream import SceneLineItemBlock, read_blocks

    return [
        b.item.item_id for b in read_blocks(io.BytesIO(data)) if isinstance(b, SceneLineItemBlock)
    ]


def _image_above_stroke(tmp_path, data: bytes) -> bool:
    svg = _render_rm_v6_to_svg(write_page(tmp_path, data))
    return svg.index("<image") > svg.index("<path")


def _layer_with_stroke() -> bytes:
    return build_rm([("Layer 1", True, [stroke(0, 17, [(0, 900), (50, 900)])])])


def _line_block(item_id: CrdtId, left: CrdtId) -> bytes:
    """A stroke on layer 1 placed after ``left`` in the layer's sequence."""
    from rmscene.crdt_sequence import CrdtSequenceItem
    from rmscene.scene_stream import SceneLineItemBlock, write_blocks

    line = stroke(0, 17, [(0, 900), (50, 900)])
    buf = io.BytesIO()
    write_blocks(
        buf,
        [
            SceneLineItemBlock(
                parent_id=LAYER_1,
                item=CrdtSequenceItem(item_id, left, CrdtId(0, 0), 0, line),
            )
        ],
    )
    return buf.getvalue()[43:]  # drop the file header


def test_stroke_written_over_image_is_drawn_on_top(tmp_path):
    data = (
        build_rm([("Layer 1", True, [])])
        + catalog_block([(KEY, FILE_NAME)])
        + image_block(CrdtId(1, 19), LAYER_1)
        + _line_block(CrdtId(1, 30), left=CrdtId(1, 19))
    )

    assert not _image_above_stroke(tmp_path, data)


def test_image_after_stroke_in_sequence_is_drawn_on_top(tmp_path):
    base = _layer_with_stroke()
    first_stroke = _stroke_ids(base)[0]
    data = (
        base
        + catalog_block([(KEY, FILE_NAME)])
        + image_block(CrdtId(1, 19), LAYER_1, left=first_stroke)
    )

    assert _image_above_stroke(tmp_path, data)


def test_moved_image_comes_to_the_top(tmp_path):
    data = (
        build_rm([("Layer 1", True, [])])
        + catalog_block([(KEY, FILE_NAME)])
        # Pasted, then written over ...
        + image_block(CrdtId(1, 19), LAYER_1)
        + _line_block(CrdtId(1, 30), left=CrdtId(1, 19))
        # ... then moved: the old item is deleted and a new one is inserted
        # at the end of the sequence.
        + image_block(CrdtId(1, 19), LAYER_1, quad=None, deleted=1)
        + image_block(CrdtId(1, 40), LAYER_1, left=CrdtId(1, 30))
    )

    images = pasted_images(_v6_blocks(write_page(tmp_path, data)))
    assert list(images) == [CrdtId(1, 40)]
    assert _image_above_stroke(tmp_path, data)

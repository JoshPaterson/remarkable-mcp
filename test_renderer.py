"""The page renderer: strokes land where they are written, in their colors,
on the right page, over the right PDF page."""

import io
import json
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image

from remarkable_mcp import extract, rm_ink, strokes

DOC = "doc-1"


def _page(*lines) -> bytes:
    """A first ink file with strokes ``(points, color)`` in page fractions."""
    data = rm_ink.first_page_rm(rm_ink.author_uuid())
    return strokes.append_strokes(
        data, [{"points": points, "color": color, "tool": "fineliner"} for points, color in lines]
    )


def _zip(tmp_path: Path, pages: dict, content: dict, pdf: bytes = None) -> Path:
    path = tmp_path / "doc.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{DOC}.content", json.dumps(content))
        for page_id, data in pages.items():
            archive.writestr(f"{DOC}/{page_id}.rm", data)
        if pdf is not None:
            archive.writestr(f"{DOC}.pdf", pdf)
    return path


def _image(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def _dark(pixel) -> bool:
    return sum(pixel) < 3 * 160


HORIZONTAL = [[0.2, 0.5], [0.8, 0.5]]


def test_full_page_strokes_land_where_written(tmp_path):
    rm = tmp_path / "page.rm"
    rm.write_bytes(_page((HORIZONTAL, "black")))
    png, (width, height) = extract.render_rm_file_full_page_png(rm, "#FFFFFF")
    image = _image(png)
    w, h = image.size
    assert _dark(image.getpixel((w // 2, h // 2)))
    assert _dark(image.getpixel((int(w * 0.25), h // 2)))
    assert not _dark(image.getpixel((w // 2, int(h * 0.3))))  # above the line
    assert not _dark(image.getpixel((int(w * 0.1), h // 2)))  # left of its start
    assert width > 0 and height > width


def test_stroke_width_matches_a_fineliner(tmp_path):
    rm = tmp_path / "page.rm"
    rm.write_bytes(_page((HORIZONTAL, "black")))
    png, _ = extract.render_rm_file_full_page_png(rm, "#FFFFFF")
    image = _image(png)
    w, h = image.size
    column = [_dark(image.getpixel((w // 2, y))) for y in range(h)]
    thickness = sum(column)
    # A fineliner is a fraction of a millimetre: a few pixels at this size.
    assert 1 <= thickness <= max(8, h // 150)


def test_pen_colors_render_in_their_color(tmp_path):
    rm = tmp_path / "page.rm"
    rm.write_bytes(_page((HORIZONTAL, "red"), ([[0.2, 0.7], [0.8, 0.7]], "blue")))
    png, _ = extract.render_rm_file_full_page_png(rm, "#FFFFFF")
    image = _image(png)
    w, h = image.size
    red = image.getpixel((w // 2, h // 2))
    blue = image.getpixel((w // 2, int(h * 0.7)))
    assert red[0] > 150 and red[2] < 120
    assert blue[2] > 150 and blue[0] < 120


def test_svg_has_one_path_per_stroke(tmp_path):
    path = _zip(
        tmp_path,
        {"p1": _page((HORIZONTAL, "black"), ([[0.3, 0.3], [0.4, 0.4]], "black"))},
        {"fileType": "notebook", "cPages": {"pages": [{"id": "p1"}]}},
    )
    svg = extract.render_page_from_document_zip_svg(path, 1, "#FFFFFF")
    assert svg.count("<path") >= 2 and "#FFFFFF".lower() in svg.lower()


def test_the_requested_page_is_rendered_even_after_blank_pages(tmp_path):
    """Ink belongs to its page id, not to its position among inked pages."""
    content = {
        "fileType": "notebook",
        "cPages": {"pages": [{"id": "blank"}, {"id": "inked"}]},
    }
    path = _zip(tmp_path, {"inked": _page((HORIZONTAL, "black"))}, content)
    page2 = extract.render_page_full_page_from_document_zip(path, 2, "#FFFFFF")
    assert page2 is not None
    image = _image(page2[0])
    w, h = image.size
    assert _dark(image.getpixel((w // 2, h // 2)))
    page1 = extract.render_page_full_page_from_document_zip(path, 1, "#FFFFFF")
    if page1 is not None:
        image = _image(page1[0])
        w, h = image.size
        assert not _dark(image.getpixel((w // 2, h // 2)))


def _pdf(pages) -> bytes:
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=595, height=842)
        # A black block whose position tells the pages apart.
        page.draw_rect(pymupdf.Rect(50, 60 * number, 250, 60 * number + 40), fill=(0, 0, 0))
    return doc.tobytes()


def test_pdf_pages_render_at_the_requested_size():
    png = extract.render_tablet_pdf_page_to_png(_pdf(2), page=2, target_long_edge=1000)
    image = _image(png)
    assert max(image.size) == 1000
    w, h = image.size
    assert _dark(image.getpixel((int(w * 150 / 595), int(h * 140 / 842))))  # page 2's block


def test_mapped_pdf_page_follows_the_page_map(tmp_path):
    """A page added on the tablet has no underlay; later pages map past it."""
    content = {
        "fileType": "pdf",
        "cPages": {
            "pages": [
                {"id": "a", "redir": {"value": 0}},
                {"id": "added"},
                {"id": "b", "redir": {"value": 1}},
            ]
        },
    }
    path = _zip(tmp_path, {}, content, pdf=_pdf(2))
    png, has_pdf = extract.render_mapped_pdf_page_from_document_zip(path, 3, 842)
    image = _image(png)
    w, h = image.size
    assert has_pdf and _dark(image.getpixel((int(w * 150 / 595), int(h * 140 / 842))))
    none, has_pdf = extract.render_mapped_pdf_page_from_document_zip(path, 2, 842)
    assert has_pdf and none is None


def test_page_count_comes_from_the_content(tmp_path):
    content = {"fileType": "notebook", "cPages": {"pages": [{"id": "a"}, {"id": "b"}]}}
    assert extract.get_document_page_count(_zip(tmp_path, {}, content)) == 2

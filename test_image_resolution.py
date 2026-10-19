"""remarkable_image dpi and region parameters."""

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from remarkable_mcp.export_resources import export_store
from remarkable_mcp.server import mcp

FIXTURE = Path(__file__).parent / "fixtures" / "tablet" / "layers_and_tags"
# 1404 stroke units * 0.3177 pt/unit = 446 pt = 6.2 in wide.
PAGE_WIDTH_IN = 1404 * 0.3177 / 72
PAGE_HEIGHT_IN = 1872 * 0.3177 / 72


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path in sorted(FIXTURE.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURE))
    return buf.getvalue()


async def _image(**arguments) -> dict:
    client = Mock()
    doc = Mock()
    doc.VissibleName = "Layers"
    doc.ID = "doc"
    doc.Parent = ""
    doc.ModifiedClient = None
    doc.is_folder = False
    doc.tags = []
    client.get_meta_items.return_value = [doc]
    client.download.return_value = _zip_bytes()
    with (
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools.download_raw_file", return_value=None),
    ):
        result = await mcp.call_tool(
            "remarkable_image", {"document": "Layers", "compatibility": True, **arguments}
        )
    return json.loads(result.content[0].text)


def _size(data: dict) -> tuple:
    assert "image_base64" not in data
    png = export_store.read_bytes(data["resource_uri"].rsplit("/", 1)[-1], "png")
    return Image.open(io.BytesIO(png)).size


async def test_dpi_sizes_full_page_from_physical_extent():
    data = await _image(page=3, dpi=300)

    width, height = _size(data)
    assert abs(width - PAGE_WIDTH_IN * 300) <= 2
    assert abs(height - PAGE_HEIGHT_IN * 300) <= 2
    assert data["dpi"] == 300


async def test_region_crops_page_fractions():
    full = _size(await _image(page=3, dpi=200))
    data = await _image(page=3, dpi=200, region=[0.25, 0.5, 0.75, 1.0])

    width, height = _size(data)
    assert abs(width - full[0] / 2) <= 1
    assert abs(height - full[1] / 2) <= 1
    assert data["region"] == [0.25, 0.5, 0.75, 1.0]


async def test_invalid_dpi_and_region_rejected():
    assert (await _image(dpi=5000))["_error"]["type"] == "invalid_parameter"
    assert (await _image(region=[0.5, 0, 0.4, 1]))["_error"]["type"] == "invalid_parameter"
    assert (await _image(region=[0, 0, 1]))["_error"]["type"] == "invalid_parameter"
    svg = await _image(output_format="svg", dpi=300)
    assert svg["_error"]["type"] == "invalid_parameter"

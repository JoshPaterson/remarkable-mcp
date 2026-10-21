"""remarkable_export page selection (single pages and ranges, PDF only)."""

from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fitz
import pytest
from mcp import types

import remarkable_mcp.tools as tool_module
from remarkable_mcp.export_resources import export_store
from remarkable_mcp.exporters import ExportMetadata, RenderedPage, write_rendered_pdf_export
from remarkable_mcp.server import mcp
from remarkable_mcp.tools import _parse_page_spec


def _document(name: str = "Lecture", doc_id: str = "doc-1"):
    return SimpleNamespace(
        VissibleName=name, ID=doc_id, Parent="", ModifiedClient=None, is_folder=False, tags=[]
    )


def _pdf_bytes(pages: int) -> bytes:
    document = fitz.open()
    for number in range(1, pages + 1):
        page = document.new_page(width=445, height=594)
        page.insert_text((40, 50), f"source page {number}")
    data = document.tobytes()
    document.close()
    return data


def _pdf_archive(pages: int, doc_id: str = "doc-1") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            f"{doc_id}.content",
            json.dumps(
                {
                    "fileType": "pdf",
                    "cPages": {
                        "pages": [{"id": f"p{i}", "redir": {"value": i}} for i in range(pages)]
                    },
                }
            ),
        )
        archive.writestr(f"{doc_id}.pdf", _pdf_bytes(pages))
    return output.getvalue()


@pytest.fixture(autouse=True)
def _clean_export_store():
    export_store.cleanup()
    yield
    export_store.cleanup()


async def _export(payload: bytes, file_type: str = "pdf", **arguments):
    client = Mock()
    client.get_meta_items.return_value = [_document()]
    client.download.return_value = payload
    with (
        patch.object(tool_module, "get_rmapi", return_value=client),
        patch.object(tool_module, "get_file_type", return_value=file_type),
    ):
        result = await mcp.call_tool("remarkable_export", {"document": "Lecture", **arguments})
    return json.loads(result.content[0].text), result


async def _exported_texts(result) -> list:
    link = next(c for c in result.content if isinstance(c, types.ResourceLink))
    resource = await mcp.read_resource(link.uri)
    with fitz.open(stream=resource[0].content, filetype="pdf") as exported:
        return [page.get_text().strip() for page in exported]


def test_parse_page_spec():
    assert _parse_page_spec(5) == [(5, 5)]
    assert _parse_page_spec("5") == [(5, 5)]
    assert _parse_page_spec(" 5-9, 12 ") == [(5, 9), (12, 12)]
    for bad in ("", "a", "0", "9-5", "1-", "-3", "1,,2", True):
        with pytest.raises(ValueError):
            _parse_page_spec(bad)


async def test_native_pdf_exports_selected_ranges():
    data, result = await _export(_pdf_bytes(6), pages="5,2-3")

    assert data["status"] == "complete"
    assert data["pages"] == 3
    assert data["selected_pages"] == [2, 3, 5]
    assert data["document_pages"] == 6
    assert await _exported_texts(result) == ["source page 2", "source page 3", "source page 5"]


async def test_native_pdf_exports_single_page():
    data, result = await _export(_pdf_bytes(6), pages=4)

    assert data["pages"] == 1
    assert await _exported_texts(result) == ["source page 4"]


async def test_archive_renders_only_selected_pages():
    from remarkable_mcp import exporters

    rendered = []
    real = exporters.render_merged_page_from_extracted_document

    def spy(extracted, page, **kwargs):
        rendered.append(page)
        return real(extracted, page=page, **kwargs)

    with patch.object(exporters, "render_merged_page_from_extracted_document", side_effect=spy):
        data, result = await _export(_pdf_archive(4), pages="2-3")

    assert rendered == [2, 3]
    assert data["status"] == "complete"
    assert data["pages"] == 2
    assert data["selected_pages"] == [2, 3]


@pytest.mark.parametrize(
    "arguments, error_type, message",
    [
        ({"pages": "7"}, "invalid_parameter", "has 6 page(s)"),
        ({"pages": "5-8"}, "invalid_parameter", "5-8 is outside"),
        ({"pages": "x"}, "invalid_parameter", "Invalid page selection"),
        ({"pages": "3", "output_format": "markdown"}, "invalid_parameter", "PDF exports"),
    ],
)
async def test_bad_selections_rejected(arguments, error_type, message):
    data, result = await _export(_pdf_bytes(6), **arguments)

    assert data["_error"]["type"] == error_type
    assert message in data["_error"]["message"]
    assert not any(isinstance(c, types.ResourceLink) for c in result.content)


def test_rendered_export_checks_selected_order(tmp_path):
    metadata = ExportMetadata(
        document_id="doc", title="T", path="/T", source_type="notebook", page_count=6
    )
    pages = [RenderedPage(page=2, image=None), RenderedPage(page=4, image=None)]

    with pytest.raises(ValueError, match="expected 3"):
        write_rendered_pdf_export(tmp_path / "out.pdf", metadata, pages, page_numbers=[2, 3])

    build = write_rendered_pdf_export(
        tmp_path / "out.pdf", metadata, iter(pages), page_numbers=[2, 4]
    )
    assert build.pages == 2
    assert build.failed_pages == (2, 4)

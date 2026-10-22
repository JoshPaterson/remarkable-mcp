"""Server instructions list the registered tools; the read-only page layout tool."""

import io
import json
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pymupdf

from remarkable_mcp.server import _build_instructions, mcp, tool_summary


def test_instructions_mention_every_registered_tool():
    for tool in mcp._tool_manager.list_tools():
        assert f"`{tool.name}(" in mcp.instructions, tool.name


def test_tool_list_follows_the_registry():
    tools = [t for t in mcp._tool_manager.list_tools() if t.name != "remarkable_trash"]
    instructions = _build_instructions(tools)
    assert "`remarkable_trash(" not in instructions
    assert "`remarkable_search(" in instructions


def test_summary_is_the_first_usecase_sentence():
    description = "\n<usecase>Read, set or remove\n an outline: the table. More.</usecase>"
    assert tool_summary(description) == "Read, set or remove an outline."


def test_cloud_instructions_do_not_send_raw_reads_to_ssh():
    env = {k: v for k, v in os.environ.items() if not k.startswith("REMARKABLE_USE")}
    env.pop("REMARKABLE_LOCAL_DIR", None)
    with patch.dict(os.environ, env, clear=True):
        instructions = _build_instructions(mcp._tool_manager.list_tools())
    assert "## Cloud Mode (Active)" in instructions
    assert 'including\n`content_type="raw"`' in instructions
    assert "require SSH" not in instructions


def _pdf_archive():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((100, 200), "Answer: ____", fontsize=12)
    pdf = doc.tobytes()
    content = {"fileType": "pdf", "pages": ["page-a"], "redirectionPageMap": [0]}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("doc.content", json.dumps(content))
        zf.writestr("doc.pdf", pdf)
    return out.getvalue(), pdf


async def test_page_layout_tool_reads_any_transport():
    archive, pdf = _pdf_archive()
    doc = SimpleNamespace(
        ID="doc", VissibleName="Worksheet", Parent="", is_folder=False, ModifiedClient=None
    )
    client = Mock()
    client.get_meta_items.return_value = [doc]
    client.download.return_value = archive
    client.get_file_type.return_value = "pdf"
    with (
        patch("remarkable_mcp.tools.get_rmapi", return_value=client),
        patch("remarkable_mcp.tools.download_raw_file", return_value=pdf),
    ):
        result = await mcp.call_tool("remarkable_page_layout", {"document": "Worksheet"})
        missing = await mcp.call_tool(
            "remarkable_page_layout", {"document": "Worksheet", "page": 3}
        )
    data = json.loads(result.content[0].text)
    assert [line["text"] for line in data["text_lines"]] == ["Answer: ____"]
    assert data["ink"] == [] and data["free"]
    error = json.loads(missing.content[0].text)["_error"]
    assert error["type"] == "invalid_parameter" and error["message"].startswith("page:")

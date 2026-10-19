"""Notebook page templates (page backgrounds).

Each page of a notebook names its template in the document's ``.content``
file (``cPages.pages[].template.value``, e.g. ``"P Dots S"``). The template
artwork is not part of the document, so templates are reported but not drawn.
"""

from typing import Any, Dict, List, Tuple

BLANK_TEMPLATE = "Blank"


def content_page_templates(content: Any) -> Dict[str, str]:
    """Page id -> template name from a ``.content`` dict (notebook pages)."""
    if not isinstance(content, dict):
        return {}
    cpages = content.get("cPages")
    if not isinstance(cpages, dict) or not isinstance(cpages.get("pages"), list):
        return {}
    templates = {}
    for entry in cpages["pages"]:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        template = entry.get("template")
        if isinstance(template, dict):
            template = template.get("value")
        if isinstance(template, str) and template:
            templates[entry["id"]] = template
    return templates


def group_pages(templates: List[Tuple[int, str]]) -> List[Tuple[int, int, str]]:
    """Collapse ``(page, template)`` pairs into ``(first, last, template)`` runs."""
    runs: List[Tuple[int, int, str]] = []
    for page, name in sorted(templates):
        if runs and runs[-1][2] == name and runs[-1][1] == page - 1:
            runs[-1] = (runs[-1][0], page, name)
        else:
            runs.append((page, page, name))
    return runs

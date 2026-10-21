"""
Response helpers for MCP tools.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional


class DateTimeEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime objects."""

    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def make_response(data: Dict[str, Any], hint: str) -> str:
    """Create a JSON response with a hint for the model."""
    data["_hint"] = hint
    return json.dumps(data, indent=2, cls=DateTimeEncoder)


def make_error(
    error_type: str, message: str, suggestion: str, did_you_mean: Optional[List[str]] = None
) -> str:
    """Create an educational error response."""
    error: Dict[str, Any] = {
        "_error": {"type": error_type, "message": message, "suggestion": suggestion}
    }
    if did_you_mean:
        error["_error"]["did_you_mean"] = did_you_mean
    return json.dumps(error, indent=2, cls=DateTimeEncoder)


def invalid_param(message: str, suggestion: str) -> str:
    """An ``invalid_parameter`` error; ``message`` names the parameter."""
    return make_error("invalid_parameter", message, suggestion)


def is_error_response(text: str) -> bool:
    """Whether a tool's JSON text reply is an error (has an ``_error`` key)."""
    if not text.lstrip().startswith("{"):
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and "_error" in data


def resolve_alias(value, alias, name: str, alias_name: str, default=None):
    """``(value, None)`` for a parameter that also has a deprecated alias:
    the alias's value when only it is given, or ``(None, error)`` when both
    are given with different values. ``default`` is the parameter's default."""
    if alias is None:
        return value, None
    if value != default and value != alias:
        return None, invalid_param(
            f"{alias_name}: give {name} or its old name {alias_name}, not both.",
            f"Use {name}.",
        )
    return alias, None

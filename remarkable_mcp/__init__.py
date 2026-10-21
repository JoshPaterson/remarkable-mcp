"""
reMarkable MCP Server

An MCP server that provides access to reMarkable tablet data through the reMarkable Cloud API.
"""

# Capability checking utilities
from remarkable_mcp.capabilities import (
    client_supports_elicitation,
    get_client_capabilities,
)

__version__ = "0.1.0"


def get_mcp():
    """Get the MCP server instance. Only imports when called."""
    from remarkable_mcp.server import mcp

    return mcp


__all__ = [
    "get_mcp",
    "__version__",
    # Capability checking
    "get_client_capabilities",
    "client_supports_elicitation",
]

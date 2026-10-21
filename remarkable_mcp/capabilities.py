"""Client capability checks for tool and resource handlers.

Each helper takes the request's MCPServer ``Context`` and reports what the
connected client declared: elicitation support and protocol extensions such as
the MCP Apps UI extension.
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context
    from mcp.types import ClientCapabilities


# MCP Apps (SEP-1865) UI extension. Clients that can render interactive HTML
# app surfaces advertise this extension during the initialize handshake:
#   capabilities.extensions["io.modelcontextprotocol/ui"] = {
#       "mimeTypes": ["text/html;profile=mcp-app"]
#   }
# MCP SDK v2 exposes this as ClientCapabilities.extensions.
APP_UI_EXTENSION_ID = "io.modelcontextprotocol/ui"
APP_UI_MIME = "text/html;profile=mcp-app"


def get_client_capabilities(ctx: "Context") -> Optional["ClientCapabilities"]:
    """Get the client's declared capabilities from the MCP context.

    Returns None if the context is not available or client hasn't sent capabilities.

    Args:
        ctx: The MCPServer Context object from a tool or resource function

    Returns:
        ClientCapabilities object or None if not available

    Example:
        @mcp.tool()
        async def my_tool(ctx: Context) -> str:
            caps = get_client_capabilities(ctx)
            if caps and caps.elicitation:
                # Client supports elicitation
                pass
    """
    try:
        return ctx.client_capabilities
    except (ValueError, AttributeError):
        # Context not available (e.g., outside of request lifecycle)
        pass
    return None


def client_supports_elicitation(ctx: "Context") -> bool:
    """Check if the client supports user elicitation.

    When true, the server can request interactive user input during tool execution.

    Args:
        ctx: The MCPServer Context object

    Returns:
        True if client supports elicitation, False otherwise
    """
    caps = get_client_capabilities(ctx)
    return caps is not None and caps.elicitation is not None


def get_client_extensions(ctx: "Context") -> dict:
    """Get the client's declared protocol extensions (SEP-1724).

    Extensions are a typed field on ClientCapabilities in MCP SDK v2.

    Args:
        ctx: The MCPServer Context object

    Returns:
        The extensions mapping (extension id -> config dict), or {} if none.
    """
    caps = get_client_capabilities(ctx)
    if caps is None:
        return {}
    extensions = getattr(caps, "extensions", None)
    if extensions is None:
        extra = getattr(caps, "model_extra", None) or {}
        extensions = extra.get("extensions")
    return extensions if isinstance(extensions, dict) else {}


def client_supports_apps(ctx: "Context") -> bool:
    """Check if the client can render MCP Apps interactive UI surfaces.

    MCP Apps (SEP-1865) lets a server return an HTML app, rendered by the client
    in a sandboxed iframe, that talks back to the server over a postMessage
    bridge. Clients advertise support via the "io.modelcontextprotocol/ui"
    extension during the initialize handshake.

    A client counts as app-capable when it declares the UI extension and either
    lists no specific mime types or includes a text/html-based mime type (the
    app profile is ``text/html;profile=mcp-app``). When the client is not
    app-capable, app tools should still return a useful text/image fallback.

    Args:
        ctx: The MCPServer Context object

    Returns:
        True if the client advertises the MCP Apps UI extension, else False.
    """
    extensions = get_client_extensions(ctx)
    ui = extensions.get(APP_UI_EXTENSION_ID)
    if not isinstance(ui, dict):
        return False
    mime_types = ui.get("mimeTypes")
    if mime_types is None:
        # Extension present without a mime-type list: treat as supported.
        return True
    if not isinstance(mime_types, list):
        return False
    for mime in mime_types:
        if isinstance(mime, str) and mime.split(";")[0].strip().lower() == "text/html":
            return True
    return False

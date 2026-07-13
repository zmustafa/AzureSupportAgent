"""Stdio launcher for the vendored EntraID (Microsoft Graph) MCP server.

The upstream `server.py` builds a FastMCP instance named ``mcp`` and registers its
tools at import time, but has no ``__main__`` entry point and imports its sibling
modules by top-level name (``from auth.graph_auth import ...``). This launcher makes the
package importable and runs the server over stdio so the host app can spawn it the same
way it spawns the Azure MCP server.

Credentials (TENANT_ID / CLIENT_ID / CLIENT_SECRET, or certificate vars) are read from
the process environment, which the host injects from the selected Azure connection.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src", "msgraph_mcp_server")

# Make `auth`, `resources`, `utils`, `server` importable by their top-level names.
sys.path.insert(0, _SRC)
os.chdir(_SRC)

import server  # noqa: E402  (import after sys.path setup; builds `mcp` + tools)

if __name__ == "__main__":
    # FastMCP defaults to the stdio transport, which is what the host's MCP client speaks.
    server.mcp.run()

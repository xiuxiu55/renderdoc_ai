"""Cursor sidecar: CodeBuddy-compatible HTTP/ACP front-end over cursor-sdk.

Run:
    python -m renderdoc_mcp.cursor_sidecar --port 8080

Requires CURSOR_API_KEY (or --api-key). The in-RenderDoc AI panel can then
Reconnect to 127.0.0.1:8080 and chat with Cursor models. CodeBuddy remains
supported — start whichever serve process you have quota for.
"""

__all__ = ["main"]


def main():
    from .server import main as _main
    _main()

"""
server.py — fastmcp HTTP Server for Windows EDR GUI Automation
===============================================================

Usage:
    # Local stdio
    python -m edr_wd.server

    # HTTP mode (for SSH tunnel / remote access)
    python -m edr_wd.server --http --port 8765

    # Custom host
    python -m edr_wd.server --http --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import argparse
import json
import logging

from fastmcp import FastMCP

from pywinauto_client import WindowsGUI

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("edr-wd")

# ---------------------------------------------------------------------------
# Global GUI client (singleton per server instance)
# ---------------------------------------------------------------------------
_gui: WindowsGUI = WindowsGUI()


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("edr-wd")


@mcp.tool(
    name="connect",
    description=(
        "Connect to a Windows application by window title (regex), process name, or PID. "
        "Must be called before any other operation."
    ),
)
def connect(
    title_re: str = None,
    process_name: str = None,
    pid: int = None,
    timeout: float = 10.0,
) -> str:
    if title_re:
        result = _gui.connect_by_title(title_re, timeout)
    elif process_name:
        result = _gui.connect_by_process(process_name, timeout)
    elif pid:
        result = _gui.connect_by_pid(pid)
    else:
        result = {"ok": False, "error": "Must specify title_re, process_name, or pid"}
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="dump_tree",
    description=(
        "Dump the control tree of the connected window. "
        "Returns a flat list of all controls with class_name, text, control_id, rectangle, "
        "is_visible, is_enabled. Use control_id as the unique identifier for click/select operations."
    ),
)
def dump_tree(window_title_re: str = None) -> str:
    result = _gui.dump_tree(window_title_re)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="click",
    description=(
        "Click a control by control_id, text, or class_name. "
        "control_id is the preferred identifier (unique within the window). "
        "Returns success status."
    ),
)
def click(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
    parent_text: str = None,
) -> str:
    result = _gui.click(control_id, text, class_name, parent_text)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="type_text",
    description=(
        "Type text into an edit control. "
        "Finds the control by control_id (preferred), text, or class_name, "
        "then sets the text content."
    ),
)
def type_text(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
    string: str = "",
) -> str:
    result = _gui.type_text(control_id, text, class_name, string)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="select",
    description=(
        "Select an item from a combo box (dropdown). "
        "Specify the combo by control_id, text, or class_name. "
        "Provide either item text (str) or zero-based index (int)."
    ),
)
def select(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
    item: str = None,
    index: int = None,
) -> str:
    result = _gui.select(control_id, text, class_name, item, index)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="get_text",
    description="Get the text content of a control.",
)
def get_text(
    control_id: int = None,
    text: str = None,
    class_name: str = None,
) -> str:
    result = _gui.get_text(control_id, text, class_name)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="screenshot",
    description=(
        "Take a screenshot of the connected window. "
        "Returns base64 PNG image if path is not specified. "
        "Set path to save to file instead."
    ),
)
def screenshot(path: str = None) -> str:
    result = _gui.screenshot(path)
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="EDR-WD MCP Server")
    parser.add_argument("--http", action="store_true", help="Run in HTTP mode")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    args = parser.parse_args()

    if args.http:
        logger.info(f"Starting HTTP server on {args.host}:{args.port}")
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        logger.info("Starting stdio server")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
ClaudeCodeBrowser MCP Server

A Model Context Protocol (MCP) compatible server that provides browser automation
capabilities to Claude Code and other AI assistants.

This server exposes tools for:
- Taking screenshots of web pages
- Clicking elements
- Typing text
- Scrolling
- Navigation
- Element inspection
- Page refresh/reload
- And more...

MIT License
Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
Author: dre@ligandal.com
"""

import asyncio
import concurrent.futures
import json
import logging
import math
import os
import secrets
import stat
import sys
import base64
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

# Try to import websockets for WebSocket support
try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

# HTTP server imports
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
import threading
import socket

# Configure logging
LOG_DIR = Path.home() / '.claudecodebrowser' / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / 'mcp_server.log'

# INFO with rotation. At DEBUG this file grew without bound (~18 MB/day, most
# of it /browser/poll access lines from the 500ms poll) and the WebSocket path
# would have written entire tool results - page text, network bodies, base64
# screenshots - into it. CLAUDE_BROWSER_DEBUG=1 restores DEBUG.
from logging.handlers import RotatingFileHandler

_SERVER_DEBUG = os.environ.get('CLAUDE_BROWSER_DEBUG') == '1'


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that keeps every file it creates to this user.

    chmod'ing the log once at startup is not enough: rotation creates a fresh
    file at the prevailing umask, so the current log drifts back to 0644 the
    first time it rolls over.
    """

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


try:
    LOG_DIR.chmod(0o700)
except OSError:
    pass

logging.basicConfig(
    level=logging.DEBUG if _SERVER_DEBUG else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        _PrivateRotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                                    backupCount=3),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('ClaudeCodeBrowser.MCPServer')

# Headless mode: CLAUDE_BROWSER_HEADLESS=1 or --headless flag
HEADLESS_MODE = os.environ.get('CLAUDE_BROWSER_HEADLESS', '0') == '1' or '--headless' in sys.argv

# The asyncio event loop owned by the main thread (started via asyncio.run in
# main()). Published so ThreadingHTTPServer worker threads can dispatch onto it
# with run_coroutine_threadsafe: asyncio.get_event_loop() raises in threads
# without a loop on Python 3.12+ (see issue #9).
MAIN_EVENT_LOOP: Optional[asyncio.AbstractEventLoop] = None

# How long a command waits for Playwright Firefox to finish booting before
# giving up. Launching takes ~15s while the HTTP port binds immediately.
HEADLESS_STARTUP_TIMEOUT = float(os.environ.get('CLAUDE_BROWSER_HEADLESS_STARTUP_TIMEOUT', '45'))

from safety import (get_safety_guard, prune_screenshots,
                    screenshot_filename,
                    redact_arguments, resolve_screenshots_dir)

# API token for localhost HTTP authentication
_TOKEN_FILE = Path.home() / '.claudecodebrowser' / 'api_token'


def _load_or_create_api_token() -> str:
    """Load the API token, or mint one atomically on first run.

    write_text() followed by chmod() created the file at the prevailing umask
    and wrote the secret into it before tightening, leaving it briefly
    world-readable; and an existing file was read without checking its mode,
    so one left loose by an older version stayed that way. O_EXCL also means
    two servers racing on first run cannot overwrite each other's token.
    """
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        _TOKEN_FILE.parent.chmod(0o700)
    except OSError:
        pass

    if _TOKEN_FILE.exists():
        token = _TOKEN_FILE.read_text().strip()
        if token:
            mode = _TOKEN_FILE.stat().st_mode & 0o777
            if mode & 0o077:
                logger.warning(f"Tightening permissions on {_TOKEN_FILE} "
                               f"(was {oct(mode)})")
                _TOKEN_FILE.chmod(0o600)
            return token

    token = secrets.token_hex(32)
    try:
        fd = os.open(str(_TOKEN_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = _TOKEN_FILE.read_text().strip()
        if existing:
            return existing
        raise
    with os.fdopen(fd, 'w') as handle:
        handle.write(token)
    logger.info(f"Generated new API token saved to {_TOKEN_FILE}")
    return token


API_TOKEN = _load_or_create_api_token()

# Configuration
HOST = os.environ.get('CLAUDE_BROWSER_HOST', '127.0.0.1')
HTTP_PORT = int(os.environ.get('CLAUDE_BROWSER_HTTP_PORT', '8765'))
WS_PORT = int(os.environ.get('CLAUDE_BROWSER_WS_PORT', '8766'))

# Cap on a single request body. Nothing legitimate approaches this; without it
# a bad Content-Length made the handler read unboundedly.
MAX_REQUEST_BYTES = 32 * 1024 * 1024
# How much of an unauthenticated POST body is read so its 403 arrives cleanly.
_REFUSED_BODY_DRAIN_BYTES = 64 * 1024

# How long a queued browser command stays executable. Longer than the longest
# human-in-the-loop wait (solveCaptcha, 200s) so a legitimately slow approval
# is not discarded, but finite so a command cannot execute long after the
# caller abandoned it.
COMMAND_QUEUE_TTL = float(os.environ.get('CLAUDE_BROWSER_COMMAND_TTL', '240'))

# Screenshots directory: ~/.claudecodebrowser/screenshots (0700), or wherever
# CLAUDE_BROWSER_SCREENSHOTS_DIR points.
SCREENSHOTS_DIR = resolve_screenshots_dir()


# The application log and the audit log redact through one function, defined
# in safety.py. They used to be two lists with a comment here saying they were
# kept in step: 'url' was in this one only, so a password-reset or SSO-token
# URL was written to audit.jsonl in clear, and nothing could tell you that by
# reading either file.
redact_for_log = redact_arguments


# Values a string flag may carry. MCP arguments arrive as JSON, but callers
# (shell wrappers, the stdio bridge, an agent writing JSON by hand) do send
# "false" as a string, and bool("false") is True. The flags here were read
# with a bare arguments.get(name, default) and tested for truth, so "false"
# meant ON - and save_to_file decides whether a PNG of whatever was on screen
# is written to disk and kept for the retention window. parseFlag in
# extension/background.js already does this for the browser side; this is the
# same rule on the server side.
_TRUE_FLAGS = {'true', '1', 'yes', 'on'}
_FALSE_FLAGS = {'false', '0', 'no', 'off'}

# browser_get_tabs/browser_find_tabs honour "limit" as the caller gives it.
DEFAULT_TAB_LIMIT = 50
# The cap the browser actually applies: MAX_TAB_RESULTS in
# extension/background.js. This said 200, so the schema and the tool
# descriptions promised an agent up to 200 tabs while 50 came back.
MAX_TAB_LIMIT = 50


def parse_flag(value: Any, fallback: bool) -> bool:
    """Read a boolean argument that may have arrived as a string or a number.

    Mirrors parseFlag() in extension/background.js. An unrecognised value
    falls back rather than being coerced, so a typo cannot silently mean the
    opposite of what it says.
    """
    if value is None or value == '':
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_FLAGS:
            return True
        if text in _FALSE_FLAGS:
            return False
    return fallback


def parse_number(value: Any, fallback: float) -> float:
    """Read a numeric argument that may have arrived as a string.

    The numeric counterpart of parse_flag, and the same rule as
    parse_number() in headless_backend.py: anything unparseable, infinite or
    NaN falls back. json.loads accepts the literals Infinity and NaN, so a
    client really can send them.

    OverflowError is in the except list because json.loads turns a long run
    of digits into an exact int, and float() on an int too large to represent
    raises it - not ValueError. {"tab_id": 10**400} therefore escaped
    execute_tool, which has no broad catch, and killed the HTTP connection
    with no JSON body and no audit entry.
    """
    if value is None or value == '' or isinstance(value, bool):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return number


def parse_int(value: Any, fallback: int) -> int:
    """parse_number for arguments that must be whole (lengths, limits, ms)."""
    return int(parse_number(value, fallback))


# The message says how to get a usable id, because the agent cannot guess one.
_BAD_TAB_ID = ('tab_id must be a whole non-negative tab id (browser_get_tabs '
               'lists them); got {value!r}')


def parse_tab_id(value: Any) -> Optional[int]:
    """Read a tab id that may have arrived as a string.

    Returns None when no tab was named, which means "the active tab". Raises
    ValueError for a value that cannot be a tab id, because the alternative -
    falling back to the active tab - acts on a different page from the one the
    caller named, and that page may be anything.
    """
    if value is None or value == '':
        return None
    # bool is an int in Python, so tab_id: true would otherwise mean tab 1.
    if isinstance(value, bool):
        raise ValueError(_BAD_TAB_ID.format(value=value))
    number = parse_number(value, float('nan'))
    # Tab ids are whole and non-negative; browser.tabs.TAB_ID_NONE is -1, so a
    # negative id names no tab in any browser.
    if math.isnan(number) or not number.is_integer() or number < 0:
        raise ValueError(_BAD_TAB_ID.format(value=value))
    return int(number)


def clamp_tab_limit(value: Any) -> int:
    """Clamp a caller-supplied tab limit.

    The limit was passed through untouched and the extension caps nothing, so
    {"url_pattern": "stub", "limit": 100000} returned 500 tabs - the user's
    whole browsing surface, from a tool that reads like a search.
    """
    limit = parse_int(value, DEFAULT_TAB_LIMIT)
    if limit < 1:
        return DEFAULT_TAB_LIMIT
    return min(limit, MAX_TAB_LIMIT)


def camelize_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Convert snake_case MCP argument keys to the camelCase the extension reads.

    The MCP tool schemas use snake_case (full_page, url_pattern, bypass_cache)
    but background.js/content.js read camelCase (fullPage, urlPattern,
    bypassCache). The headless Playwright backend keeps the original
    snake_case arguments.
    """
    def camel(key: str) -> str:
        head, *rest = key.split('_')
        return head + ''.join(part.title() for part in rest)
    return {camel(k): v for k, v in arguments.items()}


@dataclass
class BrowserCommand:
    """Represents a command to be sent to the browser."""
    action: str
    tab_id: Optional[int] = None
    data: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None


@dataclass
class MCPTool:
    """MCP Tool definition."""
    name: str
    description: str
    input_schema: Dict[str, Any]


# Tools implemented only by the Playwright backend. In attended Firefox they
# return "Unknown action", which is a confusing way to learn a tool does not
# apply; browser_safety_status reports this list.
# How long a single headless call may take before it is cancelled. Longer
# than the 30s cap on browser_wait_and_act, so a legitimately slow wait
# finishes rather than being cut off by this.
HEADLESS_CALL_TIMEOUT = 35

HEADLESS_ONLY_TOOLS = {
    'browser_eval_chain', 'browser_wait_and_act', 'browser_inject_observer',
}

# Define available MCP tools
MCP_TOOLS: List[MCPTool] = [
    MCPTool(
        name="browser_screenshot",
        description="Take a screenshot of the current browser tab or a specific tab. Returns base64 encoded PNG image.",
        input_schema={
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, uses active tab."},
                "full_page": {"type": "boolean", "description": "Capture the full page instead of the visible area. NOT SUPPORTED in attended Firefox - the result falls back to the visible viewport with fullPageCaptured: false. Use browser_scroll_and_capture, or the headless backend, for a whole page.", "default": False},
                "save_to_file": {"type": "boolean", "description": "Save screenshot to file.", "default": True},
                "filename": {"type": "string", "description": "Optional filename for saved screenshot."}
            }
        }
    ),
    MCPTool(
        name="browser_click",
        description="Click on an element in the browser. Can target by CSS selector, XPath, text content, or coordinates.",
        input_schema={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the element to click."},
                "xpath": {"type": "string", "description": "XPath expression to find the element."},
                "text": {"type": "string", "description": "Text content to search for and click."},
                "x": {"type": "number", "description": "X coordinate to click."},
                "y": {"type": "number", "description": "Y coordinate to click."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."},
                "double_click": {"type": "boolean", "description": "Perform double-click.", "default": False},
                "right_click": {"type": "boolean", "description": "Perform right-click.", "default": False}
            }
        }
    ),
    MCPTool(
        name="browser_type",
        description="Type text into an input field or editable element. Can target by selector, placeholder, name, or focus current element. Refused on a credential field (password, one-time code, card field, or a field whose name or id looks like a credential) unless allow_password_typing is set in ~/.claudecodebrowser/safety.json; an allow_password argument is ignored.",
        input_schema={
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "description": "Text to type."},
                "selector": {"type": "string", "description": "CSS selector for the input element."},
                "placeholder": {"type": "string", "description": "Placeholder text to find the input."},
                "name": {"type": "string", "description": "Name attribute of the input."},
                "id": {"type": "string", "description": "ID of the input element."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."},
                "clear": {"type": "boolean", "description": "Clear existing content first.", "default": False},
                "press_enter": {"type": "boolean", "description": "Press Enter after typing.", "default": False},
                "delay": {"type": "integer", "description": "Delay between keystrokes in ms. Attended mode defaults to 50 and headless to none; it is lowered so the whole text is typed within about 25 seconds in attended mode and 30 in headless."}
            }
        }
    ),
    MCPTool(
        name="browser_scroll",
        description="Scroll the page or a specific element.",
        input_schema={
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right", "top", "bottom"], "description": "Scroll direction."},
                "amount": {"type": "integer", "description": "Scroll amount in pixels.", "default": 300},
                "selector": {"type": "string", "description": "CSS selector for scrollable element."},
                "to_element": {"type": "string", "description": "CSS selector of element to scroll into view."},
                "x": {"type": "integer", "description": "Absolute horizontal scroll position. Both backends accept it; it was reachable only by bypassing this schema."},
                "y": {"type": "integer", "description": "Absolute vertical scroll position."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_navigate",
        description="Navigate to a URL in the browser.",
        input_schema={
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to."},
                "tab_id": {"type": "integer", "description": "Optional tab ID. Creates new tab if not specified."},
                "new_tab": {"type": "boolean", "description": "Open URL in new tab.", "default": False}
            }
        }
    ),
    MCPTool(
        name="browser_get_page_info",
        description="Get information about the current page including URL, title, interactive elements, forms, and headings.",
        input_schema={
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_get_elements",
        description="Find and return information about elements matching a selector.",
        input_schema={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector to find elements."},
                "limit": {"type": "integer", "description": "Maximum elements to return.", "default": 50},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_wait_for_element",
        description="Wait for an element to appear on the page.",
        input_schema={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the element."},
                "timeout": {"type": "integer", "description": "Maximum wait time in ms.", "default": 10000},
                "visible": {"type": "boolean", "description": "Wait for element to be visible.", "default": True},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_highlight",
        description="Highlight an element on the page for visual debugging.",
        input_schema={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the element."},
                "duration": {"type": "integer", "description": "Highlight duration in ms.", "default": 3000},
                "label": {"type": "string", "description": "Label to show above the element."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_execute_script",
        description="Execute JavaScript code in the browser context. The credential guard does not apply: a script can read any field, including a password. Refused when allow_script_execution is false, and on a protected URL while deny_scripts_on_protected_urls is on.",
        input_schema={
            "type": "object",
            "required": ["script"],
            "properties": {
                "script": {"type": "string", "description": "JavaScript code to execute."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_get_tabs",
        description="Get list of open browser tabs (URL, title, active status, loading state, whether playing audio). Defaults to the current window and a max of 50 tabs to keep results small when many tabs are open — pass current_window_only=false to see every window, or url_pattern to filter.",
        input_schema={
            "type": "object",
            "properties": {
                "current_window_only": {"type": "boolean", "default": True, "description": "Only list tabs in the current window. Set false to include every open Firefox window."},
                "limit": {"type": "integer", "default": DEFAULT_TAB_LIMIT, "maximum": MAX_TAB_LIMIT, "description": f"Max tabs to return. The browser returns at most {MAX_TAB_LIMIT} however high this goes."},
                "url_pattern": {"type": "string", "description": "Regex to filter tabs by URL before applying limit."},
                "include_favicon": {"type": "boolean", "default": False, "description": "Include favIconUrl (often a large base64 data URI) per tab."}
            }
        }
    ),
    MCPTool(
        name="browser_get_tab_info",
        description="Get detailed information about a specific tab including page info from content script.",
        input_schema={
            "type": "object",
            "required": ["tab_id"],
            "properties": {
                "tab_id": {"type": "integer", "description": "ID of the tab to get info for."}
            }
        }
    ),
    MCPTool(
        name="browser_find_tabs",
        description=f"Find tabs by URL pattern, title, or other criteria. At least one filter is required - with none it would hand over every tab in every window, so it is refused; use browser_get_tabs to list tabs deliberately. Returns at most {MAX_TAB_LIMIT} tabs and says so via truncated/total_matched.",
        input_schema={
            "type": "object",
            # At least one filter. The requirement was added to the handler
            # and never to the schema, so an agent reading the schema called
            # it with {} and got a runtime error it had no way to expect.
            "anyOf": [
                {"required": ["url"]},
                {"required": ["url_pattern"]},
                {"required": ["title"]},
                {"required": ["active"]},
                {"required": ["audible"]},
            ],
            "properties": {
                "url": {"type": "string", "description": "URL prefix to match."},
                "url_pattern": {"type": "string", "description": "Regex pattern to match URLs."},
                "title": {"type": "string", "description": "Text to search for in tab titles (case-insensitive)."},
                "active": {"type": "boolean", "description": "Filter by active state."},
                "audible": {"type": "boolean", "description": "Filter by playing audio."},
                "limit": {"type": "integer", "description": f"Max tabs to return. Default {DEFAULT_TAB_LIMIT}, and the browser returns at most {MAX_TAB_LIMIT} however high this goes.", "default": DEFAULT_TAB_LIMIT, "maximum": MAX_TAB_LIMIT}
            }
        }
    ),
    MCPTool(
        name="browser_screenshot_all_tabs",
        description="Activates and photographs EVERY tab in EVERY window in turn (unless filtered by URL pattern), so it reaches tabs you were never pointed at. Treated as a state-changing action: blocked in read-only mode and subject to protected-site confirmation. Restores the original focus afterwards.",
        input_schema={
            "type": "object",
            "properties": {
                "url_pattern": {"type": "string", "description": "Regex pattern to filter which tabs to screenshot."},
                "include_data": {"type": "boolean", "description": "Include base64 image data in response (large).", "default": False}
            }
        }
    ),
    MCPTool(
        name="browser_create_tab",
        description="Create a new browser tab.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open in the new tab.", "default": "about:blank"},
                "active": {"type": "boolean", "description": "Make the new tab active.", "default": True}
            }
        }
    ),
    MCPTool(
        name="browser_close_tab",
        description="Close a browser tab.",
        input_schema={
            "type": "object",
            "required": ["tab_id"],
            "properties": {
                "tab_id": {"type": "integer", "description": "ID of the tab to close."}
            }
        }
    ),
    MCPTool(
        name="browser_focus_tab",
        description="Focus/activate a browser tab.",
        input_schema={
            "type": "object",
            "required": ["tab_id"],
            "properties": {
                "tab_id": {"type": "integer", "description": "ID of the tab to focus."}
            }
        }
    ),
    MCPTool(
        name="browser_get_value",
        description="Get the value of an input element. Credential and hidden fields read back as '***' with masked: true.",
        input_schema={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the input element."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_set_value",
        description="Set the value of an input element directly (without typing simulation).",
        input_schema={
            "type": "object",
            "required": ["selector", "value"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the input element."},
                "value": {"type": "string", "description": "Value to set."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_select_option",
        description="Select an option in a dropdown/select element.",
        input_schema={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the select element."},
                "value": {"type": "string", "description": "Option value to select."},
                "text": {"type": "string", "description": "Option text to select."},
                "index": {"type": "integer", "description": "Option index to select."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_hover",
        description="Hover over an element to trigger hover effects.",
        input_schema={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the element."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_refresh",
        description="Refresh/reload the current page or a specific tab. Useful after deploying code changes.",
        input_schema={
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, refreshes active tab."},
                "bypass_cache": {"type": "boolean", "description": "Hard refresh - bypass browser cache (like Ctrl+Shift+R).", "default": False},
                "wait_for_load": {"type": "boolean", "description": "Wait for page to fully load after refresh.", "default": True}
            }
        }
    ),
    MCPTool(
        name="browser_hard_refresh",
        description="Force refresh the page bypassing all caches (equivalent to Ctrl+Shift+R). Essential after server restarts.",
        input_schema={
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, refreshes active tab."}
            }
        }
    ),
    MCPTool(
        name="browser_reload_all",
        description="Reload all open browser tabs. Optionally filter by URL pattern. Great for refreshing all dev server tabs after deployment.",
        input_schema={
            "type": "object",
            "properties": {
                "url_pattern": {"type": "string", "description": "Regex pattern to filter which tabs to reload (e.g., 'localhost' or 'ligandal\\.com')."},
                "bypass_cache": {"type": "boolean", "description": "Hard refresh all matching tabs.", "default": True}
            }
        }
    ),
    MCPTool(
        name="browser_reload_by_url",
        description="Reload all tabs matching a specific URL or pattern. Perfect for refreshing dev server tabs after launching a new server.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL prefix to match (e.g., 'http://localhost:5000')."},
                "url_pattern": {"type": "string", "description": "Regex pattern to match URLs."},
                "bypass_cache": {"type": "boolean", "description": "Hard refresh matching tabs.", "default": True}
            }
        }
    ),
    # Dynamic content tools
    MCPTool(
        name="browser_wait_for_change",
        description="Wait for DOM changes on the page. Useful after clicking elements that trigger dynamic updates, AJAX calls, or animations.",
        input_schema={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of element to observe (default: body).", "default": "body"},
                "timeout": {"type": "integer", "description": "Max time to wait in ms.", "default": 10000},
                "change_type": {"type": "string", "enum": ["childList", "attributes", "text"], "description": "Type of change to wait for."},
                "subtree": {"type": "boolean", "description": "Observe child elements too.", "default": True},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_wait_for_network_idle",
        description="Wait for network requests to settle. Perfect for waiting after actions that trigger API calls. Counted at the network layer via webRequest, so it sees fetch, XHR and subresources (a page still loading images is not idle).",
        input_schema={
            "type": "object",
            "properties": {
                "timeout": {"type": "integer", "description": "Max time to wait in ms.", "default": 10000},
                "idle_time": {"type": "integer", "description": "How long network must be idle (ms).", "default": 500},
                "persistent_after": {"type": "integer", "description": "A request still open after this many ms counts as a persistent channel (WebSocket, SSE, long-poll) and stops blocking idle. Without it, any page holding a live socket is never idle.", "default": 10000},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_observe_element",
        description="Start observing an element for changes. Call browser_stop_observing later to get accumulated changes. The observer expires on its own after max_lifetime_ms (5 minutes by default) and stops recording; browser_stop_observing then returns expired: true, so raise max_lifetime_ms up front if you mean to watch for longer.",
        input_schema={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of element to observe."},
                "observer_id": {"type": "string", "description": "ID for this observer (to stop it later)."},
                "max_lifetime_ms": {"type": "integer", "description": "How long the observer keeps recording before it expires by itself, in ms. Default 300000 (5 minutes).", "default": 300000},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_stop_observing",
        description="Stop observing an element and get all accumulated changes since observation started.",
        input_schema={
            "type": "object",
            "required": ["observer_id"],
            "properties": {
                "observer_id": {"type": "string", "description": "ID of the observer to stop."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_scroll_and_capture",
        description="Scroll through the entire page collecting information about visible elements at each viewport position. Use with browser_screenshot for full-page visual capture.",
        input_schema={
            "type": "object",
            "properties": {
                "scroll_step": {"type": "integer", "description": "Pixels to scroll each step (default: 80% of viewport)."},
                "delay": {"type": "integer", "description": "Delay between scrolls in ms.", "default": 500},
                "max_scrolls": {"type": "integer", "description": "Maximum number of scroll steps.", "default": 20},
                "restore": {"type": "boolean", "description": "Restore original scroll position after.", "default": True},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_click_and_wait",
        description="Click an element and wait for dynamic content to load. Combines click + wait for DOM changes. Perfect for buttons that open modals, load content, or trigger navigation.",
        input_schema={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the element to click."},
                "xpath": {"type": "string", "description": "XPath expression to find the element."},
                "text": {"type": "string", "description": "Text content to search for and click."},
                "wait_timeout": {"type": "integer", "description": "Max time to wait for changes in ms.", "default": 5000},
                "wait_for_selector": {"type": "string", "description": "Wait for specific element to appear after click."},
                "wait_for_change": {"type": "boolean", "description": "Wait for any DOM change.", "default": True},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    # Console and Network Logging Tools
    MCPTool(
        name="browser_start_logging",
        description="Start capturing console logs and network requests from the browser. Use this before performing actions you want to monitor. Logs are accumulated until you retrieve them. Network capture uses webRequest, so it sees fetch, XHR, WebSocket handshakes and beacons; console capture sees page errors and the extension's own output, not the page's console.log (see browser_get_console_logs). Refused on a private-browsing tab. Not implemented in headless mode. Capture is off until you call this, and stops when you call browser_stop_logging or when the tab navigates to a different origin (so start it on the site you want to watch, not on about:blank); browser_get_network_logs then reports loggingEnabled: false.",
        input_schema={
            "type": "object",
            "properties": {
                "clear_existing": {"type": "boolean", "description": "Clear any existing logs before starting.", "default": False},
                "capture_bodies": {"type": "boolean", "description": "Capture response bodies for textual responses (JSON, text, XML). Set false for metadata and headers only.", "default": True},
                "include_all_types": {"type": "boolean", "description": "Log every request type including images, fonts and stylesheets. By default only API-shaped traffic (fetch/XHR, WebSocket, beacon, ping) is logged.", "default": False},
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, uses active tab."}
            }
        }
    ),
    MCPTool(
        name="browser_stop_logging",
        description="Stop capturing console logs and network requests. Logs are preserved and can still be retrieved.",
        input_schema={
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, uses active tab."}
            }
        }
    ),
    MCPTool(
        name="browser_get_console_logs",
        description="Retrieve captured console output. IMPORTANT in attended Firefox: a content script cannot see the page's own console, so this does NOT return the page's console.log calls. It returns uncaught page errors and unhandled promise rejections (source: \"page\"), plus output from the extension's own scripts including anything browser_execute_script prints (source: \"extension\"). An empty result therefore does not mean the page logged nothing. Headless mode (Playwright) does not implement this tool at all: the call comes back with \"Unsupported headless action: getConsoleLogs\", which is an error, not an empty console. Check capturesPageConsole in the result.",
        input_schema={
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["log", "warn", "error", "info", "debug"], "description": "Filter by log level."},
                "search": {"type": "string", "description": "Filter logs containing this text."},
                "limit": {"type": "integer", "description": "Maximum number of logs to return.", "default": 100},
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, uses active tab."}
            }
        }
    ),
    MCPTool(
        name="browser_get_network_logs",
        description="Retrieve captured network requests and responses. Perfect for debugging API calls, seeing request/response data, and monitoring AI chat communications. Captured via webRequest, so fetch and XHR are both covered. Credential-bearing headers (Authorization, Cookie, Set-Cookie, X-API-Key and similar) are reported as '***'. Credential-shaped URL parameters (also in Location, Refresh and Referer) and credential fields in bodies are scrubbed too, as best effort: free page text and contenteditable content in a captured HTML body are not. Response bodies are captured for textual content types only, up to 5000 characters; request bodies up to 1000.",
        input_schema={
            "type": "object",
            "properties": {
                "url_pattern": {"type": "string", "description": "Regex pattern to filter by URL."},
                "method": {"type": "string", "description": "Filter by HTTP method (GET, POST, etc.)."},
                "status": {"type": "integer", "description": "Filter by HTTP status code."},
                "errors_only": {"type": "boolean", "description": "Only return failed requests.", "default": False},
                "limit": {"type": "integer", "description": "Maximum number of logs to return.", "default": 100},
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, uses active tab."}
            }
        }
    ),
    MCPTool(
        name="browser_clear_logs",
        description="Clear all captured console and/or network logs.",
        input_schema={
            "type": "object",
            "properties": {
                "console": {"type": "boolean", "description": "Clear console logs.", "default": True},
                "network": {"type": "boolean", "description": "Clear network logs.", "default": True},
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, uses active tab."}
            }
        }
    ),
    MCPTool(
        name="browser_eval_chain",
        description=(
            "Execute a sequence of JavaScript expressions in the page, sharing state between steps. "
            "Each step can inspect the result of the previous one and branch conditionally. "
            "Console output (console.log/warn/error) is captured per step. "
            "Ideal for multi-turn inspection tasks without round-tripping per expression. "
            "HEADLESS MODE ONLY: the Firefox extension does not implement this; "
            "in attended mode it returns \"Unknown action\". Check browser_safety_status."
        ),
        input_schema={
            "type": "object",
            "required": ["steps"],
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "Ordered list of JS expressions to evaluate. Each step receives the prior result as `$prev`.",
                    "items": {
                        "type": "object",
                        "required": ["script"],
                        "properties": {
                            "script": {"type": "string", "description": "JavaScript expression to evaluate."},
                            "label": {"type": "string", "description": "Human-readable label for this step."},
                            "capture_console": {"type": "boolean", "default": True, "description": "Capture console output for this step."},
                            "stop_on_error": {"type": "boolean", "default": True, "description": "Abort chain if this step throws."}
                        }
                    }
                },
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_wait_and_act",
        description=(
            "Poll the page until a condition is met, then execute an action. "
            "Use for waiting on async UI state (e.g. 'wait until #result is visible, then click it'). "
            "HEADLESS MODE ONLY: the Firefox extension does not implement this; "
            "in attended mode it returns \"Unknown action\". Check browser_safety_status."
        ),
        input_schema={
            "type": "object",
            "required": ["condition", "action_script"],
            "properties": {
                "condition": {"type": "string", "description": "JS expression that returns truthy when ready."},
                "action_script": {"type": "string", "description": "JS to run once condition is met."},
                "poll_interval_ms": {"type": "integer", "default": 200, "description": "How often to check condition."},
                "timeout_ms": {"type": "integer", "default": 15000, "maximum": 30000, "description": "Max wait time before giving up. Values above 30000 are capped, and the result says so with timeout_capped."},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    # Navigation history
    MCPTool(
        name="browser_go_back",
        description="Navigate back in the tab's history (like the browser Back button).",
        input_schema={
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, uses active tab."}
            }
        }
    ),
    MCPTool(
        name="browser_go_forward",
        description="Navigate forward in the tab's history (like the browser Forward button).",
        input_schema={
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "Optional tab ID. If not specified, uses active tab."}
            }
        }
    ),
    MCPTool(
        name="browser_press_key",
        description="Press a keyboard key (with optional modifiers) on the focused element or a specific element. Useful for Enter, Escape, Tab, arrow keys, and shortcuts.",
        input_schema={
            "type": "object",
            "required": ["key"],
            "properties": {
                "key": {"type": "string", "description": "Key to press, e.g. 'Enter', 'Escape', 'Tab', 'ArrowDown', 'a'."},
                "selector": {"type": "string", "description": "Optional CSS selector of element to focus first."},
                "ctrl": {"type": "boolean", "description": "Hold Ctrl.", "default": False},
                "shift": {"type": "boolean", "description": "Hold Shift.", "default": False},
                "alt": {"type": "boolean", "description": "Hold Alt.", "default": False},
                "meta": {"type": "boolean", "description": "Hold Meta/Cmd.", "default": False},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_get_text",
        description="Extract the visible text content of the page or a specific element. Lighter-weight than a screenshot for reading page content.",
        input_schema={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Optional CSS selector; defaults to the whole page body."},
                "max_length": {"type": "integer", "description": "Truncate the returned text to this many characters.", "default": 20000},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    # Human approval, workflows, and page auditing
    MCPTool(
        name="browser_request_approval",
        description="Ask the human at the browser to approve or deny an action. Opens an Approve/Deny window from the extension, which the page cannot read or click, plus an OS notification, and waits for their decision. If no window can be opened it falls back to an in-page banner and marks the result degraded: true. Use before doing anything the user might want to veto. Unavailable in headless mode (no human present).",
        input_schema={
            "type": "object",
            "required": ["message"],
            "properties": {
                "message": {"type": "string", "description": "What you are asking permission to do, in plain language."},
                "detail": {"type": "string", "description": "Optional extra context shown in smaller text."},
                "timeout": {"type": "integer", "description": "How long to wait for a decision in ms.", "default": 60000},
                "tab_id": {"type": "integer", "description": "Optional tab ID to show the banner in."}
            }
        }
    ),
    MCPTool(
        name="browser_run_workflow",
        description="Run a declarative multi-step browser workflow with per-step assertions — an end-to-end test runner for web apps. Each step calls a browser tool; optional assertions check the page afterwards (url_contains, text_contains, selector_exists). Failing steps capture a screenshot. Returns pass/fail per step.",
        input_schema={
            "type": "object",
            "required": ["steps"],
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "Ordered workflow steps.",
                    "items": {
                        "type": "object",
                        "required": ["tool"],
                        "properties": {
                            "label": {"type": "string", "description": "Human-readable step name."},
                            "tool": {"type": "string", "description": "Browser tool to call, e.g. browser_navigate, browser_click, browser_type."},
                            "arguments": {"type": "object", "description": "Arguments for the tool."},
                            "assert": {
                                "type": "object",
                                "description": "Checks to run after the step succeeds.",
                                "properties": {
                                    "url_contains": {"type": "string", "description": "Current URL must contain this substring."},
                                    "text_contains": {"type": "string", "description": "Visible page text must contain this substring."},
                                    "selector_exists": {"type": "string", "description": "This CSS selector must match at least one element."}
                                }
                            }
                        }
                    }
                },
                "stop_on_failure": {"type": "boolean", "description": "Stop at the first failing step.", "default": True},
                "screenshot_on_failure": {"type": "boolean", "description": "Capture a screenshot when a step fails.", "default": True}
            }
        }
    ),
    MCPTool(
        name="browser_solve_captcha",
        description=(
            "Detect a captcha on the page and hand it to the human to solve, then continue. "
            "Detects reCAPTCHA, hCaptcha, Cloudflare Turnstile, and generic image captchas, "
            "shows a solve banner plus an OS notification, and waits until the challenge is "
            "completed (its response token appears) or the human clicks Done. This project "
            "does NOT auto-solve captchas or use solver services — a human completes the "
            "challenge. In headless mode there is no human, so it reports what it detected "
            "and that a human is required."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "timeout": {"type": "integer", "description": "How long to wait for the human to solve it, in ms.", "default": 180000},
                "detect_only": {"type": "boolean", "description": "Only report whether a captcha is present, without waiting.", "default": False},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    MCPTool(
        name="browser_audit_page",
        description="Audits the current page for review and visual critique: heading structure, images missing alt text, unlabeled form inputs, empty links/buttons, meta/title info, viewport and element counts — plus a screenshot. It gathers this by running a fixed read-only script in the page, so it is refused when allow_script_execution is false and on a protected URL while deny_scripts_on_protected_urls is on; browser_safety_status lists the tools those settings cover. One call gathers everything needed to critique a page's structure and accessibility basics.",
        input_schema={
            "type": "object",
            "properties": {
                "screenshot": {"type": "boolean", "description": "Also capture a screenshot.", "default": True},
                "full_page": {"type": "boolean", "description": "Make the screenshot full-page.", "default": False},
                "tab_id": {"type": "integer", "description": "Optional tab ID."}
            }
        }
    ),
    # Safety
    MCPTool(
        name="browser_safety_status",
        description="Show the active safety guard policy and which mode you are in (attended Firefox or headless Playwright), including which tools are headless-only. Call this first if a tool returns 'Unknown action'. Policy: read-only mode, script toggle, credential guard state (enforced, enforced_except_scripts or advisory), protected/blocked/allowed URL patterns, rate-limit state, and audit log location. Configured in ~/.claudecodebrowser/safety.json.",
        input_schema={
            "type": "object",
            "properties": {}
        }
    ),
    MCPTool(
        name="browser_inject_observer",
        description=(
            "Inject a MutationObserver into the page that records DOM changes (not console output) "
            "into window.__ccb_mutations in the page. Nothing collects that buffer for you: read it "
            "with browser_execute_script (return window.__ccb_mutations), which the script toggle "
            "applies to. browser_get_console_logs does not see it. Useful for watching live UI updates. "
            "HEADLESS MODE ONLY: the Firefox extension does not implement this; "
            "in attended mode it returns \"Unknown action\". Check browser_safety_status."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "default": "body", "description": "Root element to observe."},
                "observe_attributes": {"type": "boolean", "default": True},
                "observe_child_list": {"type": "boolean", "default": True},
                "observe_subtree": {"type": "boolean", "default": True},
                "tab_id": {"type": "integer"}
            }
        }
    )
]


class BrowserConnectionManager:
    """Manages connections to browser extensions."""

    def __init__(self):
        self.browser_connections = {}
        self.pending_requests = {}
        self.http_pending_requests = {}
        self.request_counter = 0

    def register_browser(self, browser_id: str, connection):
        """Register a new browser connection."""
        self.browser_connections[browser_id] = connection
        logger.info(f"Browser registered: {browser_id}")

    def unregister_browser(self, browser_id: str):
        """Unregister a browser connection."""
        if browser_id in self.browser_connections:
            del self.browser_connections[browser_id]
            logger.info(f"Browser unregistered: {browser_id}")

    def get_active_browser(self):
        """Get the first available browser connection."""
        if self.browser_connections:
            return list(self.browser_connections.values())[0]
        return None

    async def send_command(self, command: BrowserCommand,
                           timeout: float = 30.0) -> Dict[str, Any]:
        """Send a command to the browser and wait for response."""
        browser = self.get_active_browser()
        if not browser:
            return {"success": False, "error": "No browser connected"}

        self.request_counter += 1
        request_id = str(self.request_counter)
        command.request_id = request_id

        # Create a future for the response
        future = asyncio.get_event_loop().create_future()
        self.pending_requests[request_id] = future

        try:
            await browser.send(json.dumps(asdict(command)))
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            return {"success": False, "error": "Command timed out"}
        finally:
            self.pending_requests.pop(request_id, None)

    def handle_response(self, response: Dict[str, Any]):
        """Handle a response from the browser."""
        request_id = response.get('requestId')
        if request_id and request_id in self.pending_requests:
            future = self.pending_requests[request_id]
            if not future.done():
                future.set_result(response)
        # Also signal HTTP polling waiters
        if request_id and request_id in self.http_pending_requests:
            event, result_holder = self.http_pending_requests[request_id]
            result_holder['response'] = response
            event.set()


# Global connection manager
connection_manager = BrowserConnectionManager()


class MCPHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MCP server."""

    def log_message(self, format, *args):
        # The native host polls every 500ms; logging that line filled the file
        # with tens of thousands of entries of nothing.
        line = format % args
        if '/browser/poll' in line or '/health' in line:
            logger.debug(f"HTTP: {line}")
        else:
            logger.info(f"HTTP: {line}")

    def send_json_response(self, data: Dict[str, Any], status: int = 200,
                           reason: Optional[str] = None):
        """Send a JSON response.

        reason overrides the status line's phrase. Every caller here is a
        local process using urllib.request.urlopen, which raises HTTPError on
        a 4xx/5xx and throws the body away - so for an error the caller is
        expected to read, the status line is the only text that reaches it.
        """
        self.send_response(status, reason)
        self.send_header('Content-Type', 'application/json')
        # No CORS headers: all callers (stdio_wrapper, native host) are local processes
        # using urllib — not browser fetch. Omitting CORS blocks cross-origin requests.
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def _check_auth(self) -> bool:
        """Validate the X-API-Key header against the server token."""
        provided = self.headers.get('X-API-Key')
        if not provided:
            return False
        # compare_digest, as the WebSocket handshake already uses: == leaks the
        # position of the first differing byte. Bytes, not str: on a non-ASCII
        # str it raises TypeError, which dropped the connection with no 403.
        return secrets.compare_digest(provided.encode('utf-8'),
                                      API_TOKEN.encode('utf-8'))

    def do_OPTIONS(self):
        """Reject CORS preflight: no cross-origin access is needed or allowed."""
        self.send_response(403)
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)

        if parsed.path == '/health':
            # Health endpoint is unauthenticated so native host can probe without the token
            self.send_json_response({
                'status': 'ok',
                'timestamp': datetime.now().isoformat(),
                'version': '1.9.6',
                'browsers_connected': len(connection_manager.browser_connections)
            })

        elif not self._check_auth():
            self.send_json_response({'error': 'Unauthorized'}, 403)

        elif parsed.path == '/mcp/tools':
            # Return list of available MCP tools
            tools = [
                {
                    'name': tool.name,
                    'description': tool.description,
                    'inputSchema': tool.input_schema
                }
                for tool in MCP_TOOLS
            ]
            self.send_json_response({'tools': tools})

        elif parsed.path == '/screenshots':
            # List saved screenshots
            screenshots = []
            # Case-folded, matching prune_screenshots: an older version let
            # the caller name capture.PNG, which was swept but never listed.
            for f in (f for f in SCREENSHOTS_DIR.iterdir()
                      if f.suffix.lower() == '.png' and f.is_file()):
                screenshots.append({
                    'name': f.name,
                    'path': str(f),
                    'size': f.stat().st_size,
                    'created': datetime.fromtimestamp(f.stat().st_ctime).isoformat()
                })
            self.send_json_response({'screenshots': sorted(screenshots, key=lambda x: x['created'], reverse=True)})

        elif parsed.path == '/browser/poll':
            # Extension/native host polls for pending commands
            # Pop under the lock: two pollers could both pass an unguarded
            # "if pending" and the loser would raise IndexError. Expired
            # commands are dropped rather than delivered: the caller stopped
            # waiting long ago, so running one now is an unrequested action.
            now = time.time()
            with _PENDING_COMMANDS_LOCK:
                pending = getattr(self.server, '_pending_commands', [])
                command = None
                while pending:
                    candidate = pending.pop(0)
                    age = now - candidate.get('queuedAt', now)
                    if age > COMMAND_QUEUE_TTL:
                        logger.warning(
                            f"Dropping stale queued command "
                            f"{candidate.get('action')} after {age:.0f}s - the "
                            f"caller has already given up on it")
                        continue
                    command = candidate
                    break
                has_more = len(pending) > 0
            if command is not None:
                self.send_json_response({'command': command, 'has_more': has_more})
            else:
                self.send_json_response({'command': None, 'has_more': False})

        else:
            self.send_json_response({'error': 'Not found'}, 404)

    def do_POST(self):
        """Handle POST requests."""
        if not self._check_auth():
            # Read a small body before refusing: closing with it unread sends
            # a reset, and the client loses the 403's body - a stale token
            # then looked like a connection fault. A large one is left unread
            # rather than accepted from an unauthenticated caller.
            try:
                length = int(self.headers.get('Content-Length', 0) or 0)
            except (TypeError, ValueError):
                length = 0
            if 0 < length <= _REFUSED_BODY_DRAIN_BYTES:
                self.rfile.read(length)
            self.send_json_response({'error': 'Unauthorized'}, 403)
            return

        parsed = urlparse(self.path)
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except (TypeError, ValueError):
            self.send_json_response({'error': 'Invalid Content-Length'}, 400)
            return
        if content_length < 0 or content_length > MAX_REQUEST_BYTES:
            self.send_json_response({'error': 'Request body too large'}, 413)
            return

        try:
            raw = self.rfile.read(content_length) if content_length > 0 else b'{}'
            body = raw.decode('utf-8')
        except (UnicodeDecodeError, OSError):
            self.send_json_response({'error': 'Body must be UTF-8'}, 400)
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_json_response({'error': 'Invalid JSON'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json_response({'error': 'Body must be a JSON object'}, 400)
            return

        if parsed.path == '/mcp/call':
            # Call an MCP tool
            tool_name = data.get('name')
            arguments = data.get('arguments', {})
            if not isinstance(arguments, dict):
                self.send_json_response(
                    {'error': 'arguments must be a JSON object'}, 400)
                return

            try:
                result = self.execute_tool(tool_name, arguments)
            except Exception as e:
                # Last resort. An exception here used to propagate out of the
                # handler, which closes the socket: the client saw a dropped
                # connection rather than an error it could read, and nothing
                # said which tool did it. Report it and keep serving.
                logger.exception(f"Unhandled error in {tool_name!r}")
                result = {'success': False,
                          'error': f'{tool_name} failed internally: '
                                   f'{type(e).__name__}: {e}'}
            self.send_json_response(result)

        elif parsed.path == '/browser/command':
            # This reported {"success": true, "message": "Command queued"}
            # while queueing nothing at all - the one caller
            # (forward_to_mcp_server in the native host) was told its work had
            # been accepted. Commands reach the browser via _dispatch_action,
            # which puts them on the poll queue; nothing should be posting
            # them here.
            logger.warning(
                "Deprecated POST /browser/command called for action "
                f"{data.get('action')!r}; it queues nothing. Commands are "
                "dispatched server-side via the MCP tool path.")
            self.send_json_response({
                'success': False,
                'error': 'POST /browser/command is not implemented and queues '
                         'nothing. It previously reported success without '
                         'doing anything. Use the MCP tool interface.'
            }, 410,
                # The body never reaches the caller: forward_to_mcp_server in
                # the native host uses urlopen, which raises HTTPError on 410
                # and discards the body, so the operator saw "MCP server
                # connection failed: HTTP Error 410: Gone" and read it as a
                # connection fault. The status line's phrase does reach them.
                reason='Gone - use the MCP tool interface, not '
                       '/browser/command')

        elif parsed.path == '/browser/response':
            # Response from browser extension
            connection_manager.handle_response(data)
            self.send_json_response({'success': True})

        else:
            self.send_json_response({'error': 'Not found'}, 404)

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an MCP tool by sending command to browser via native host."""
        logger.info(f"Executing tool: {tool_name} with args: "
                    f"{redact_for_log(arguments)}")

        # Only the safety config opens the credential guard, so drop any
        # spelling of the flag the agent sent. The server set it for six tools
        # and passed it through for the rest, so allow_password: true on
        # browser_get_text unmasked credentials - and content.js also reads
        # allowPassword, which camelize_args leaves as it is.
        for key in [k for k in arguments
                    if str(k).replace('_', '').lower() == 'allowpassword']:
            del arguments[key]

        # Normalise tab_id before anything reads it. A JSON client sends
        # tab_id: "7", and from here the value goes to the approval prompt,
        # the command envelope and the headless backend, which looks the tab
        # up in a dict keyed by int - so "1" reported "No headless tab with
        # id 1" for a tab that was open. background.js coerces its own
        # (resolveTabId), but only the id it is handed.
        if 'tab_id' in arguments:
            try:
                arguments['tab_id'] = parse_tab_id(arguments['tab_id'])
            except ValueError as e:
                return {'success': False, 'error': str(e)}

        # Safety guard: scheme/blocklist checks, read-only mode, script toggle,
        # protected-domain confirmation, rate limiting, audit logging.
        guard = get_safety_guard()
        denial = guard.check(tool_name, arguments)

        # Duo-style human approval: when a protected action needs confirmation
        # and a human is (potentially) at the browser, ask them directly with
        # an in-page Approve/Deny banner instead of bouncing a token back
        # through the agent. Headless mode has no human, so it keeps the
        # token flow.
        if (denial is not None and denial.get('confirmation_required')
                and denial.get('approval_mode', 'auto') in ('auto', 'human')
                and not HEADLESS_MODE):
            approval = self._request_human_approval(
                tool_name, arguments, denial, tab_id=arguments.get('tab_id'))
            if approval.get('success') and approval.get('approved'):
                denial = guard.check(tool_name, dict(arguments), human_approved=True)
            elif approval.get('success') and approval.get('approved') is False:
                return {'success': False,
                        'safety_decision': 'human_denied',
                        'error': f'The user denied {tool_name} via the in-browser '
                                 f'approval prompt. Do not retry without asking them why.'}
            else:
                # The prompt could not be shown or was not answered. Falling
                # through to the token denial would hand the agent a token and
                # tell it to re-send the call itself, which is not a human in
                # the loop at all. In a mode that promises a human decision,
                # no decision means no.
                return {
                    'success': False,
                    'safety_decision': 'approval_undeliverable',
                    'protected_url': denial.get('protected_url'),
                    'error': (
                        f'{tool_name} targets a protected site and the in-browser '
                        f'approval prompt could not be completed '
                        f'({approval.get("error", "no response")}). Refused. Ask the '
                        f'person to confirm what they want and to check the '
                        f'extension is connected; do not retry automatically.'),
                }

        if denial is not None:
            logger.warning(f"Safety guard blocked {tool_name}: {denial.get('safety_decision')}")
            return denial

        # Handled entirely server-side, no browser round-trip needed.
        if tool_name == 'browser_safety_status':
            status = guard.status()
            # Several tools exist in only one mode, and the agent had no way to
            # find out which it was in except by calling one and failing.
            status['mode'] = 'headless' if HEADLESS_MODE else 'attended'
            status['headless_only_tools'] = sorted(HEADLESS_ONLY_TOOLS)
            status['browsers_connected'] = len(connection_manager.browser_connections)
            return status
        if tool_name == 'browser_run_workflow':
            return self._run_workflow(arguments)
        if tool_name == 'browser_audit_page':
            return self._audit_page(arguments)

        # Map tool names to actions
        tool_action_map = {
            'browser_screenshot': 'screenshot',
            'browser_click': 'click',
            'browser_type': 'type',
            'browser_scroll': 'scroll',
            'browser_navigate': 'navigate',
            'browser_get_page_info': 'getPageInfo',
            'browser_get_elements': 'getElements',
            'browser_wait_for_element': 'waitForElement',
            'browser_highlight': 'highlight',
            'browser_execute_script': 'executeScript',
            'browser_get_tabs': 'getTabs',
            'browser_get_tab_info': 'getTabInfo',
            'browser_find_tabs': 'findTabs',
            'browser_screenshot_all_tabs': 'screenshotAllTabs',
            'browser_create_tab': 'createTab',
            'browser_close_tab': 'closeTab',
            'browser_focus_tab': 'focusTab',
            'browser_get_value': 'getValue',
            'browser_set_value': 'setValue',
            'browser_select_option': 'selectOption',
            'browser_hover': 'hover',
            'browser_refresh': 'refresh',
            'browser_hard_refresh': 'hardRefresh',
            'browser_reload_all': 'reloadAll',
            'browser_reload_by_url': 'reloadByUrl',
            # Dynamic content tools
            'browser_wait_for_change': 'waitForChange',
            'browser_wait_for_network_idle': 'waitForNetworkIdle',
            'browser_observe_element': 'observeElement',
            'browser_stop_observing': 'stopObserving',
            'browser_scroll_and_capture': 'scrollAndCapture',
            'browser_click_and_wait': 'clickAndWait',
            # Console and network logging tools
            'browser_start_logging': 'startLogging',
            'browser_stop_logging': 'stopLogging',
            'browser_get_console_logs': 'getConsoleLogs',
            'browser_get_network_logs': 'getNetworkLogs',
            'browser_clear_logs': 'clearLogs',
            # Multi-turn / conditional execution
            'browser_eval_chain': 'evalChain',
            'browser_wait_and_act': 'waitAndAct',
            'browser_inject_observer': 'injectObserver',
            # Navigation history and keyboard/text
            'browser_go_back': 'goBack',
            'browser_go_forward': 'goForward',
            'browser_press_key': 'pressKey',
            'browser_get_text': 'getText',
            # Human approval
            'browser_request_approval': 'requestApproval',
            # Captcha detection + human handoff
            'browser_solve_captcha': 'solveCaptcha'
        }

        if tool_name not in tool_action_map:
            return {'success': False, 'error': f'Unknown tool: {tool_name}'}

        action = tool_action_map[tool_name]
        tab_id = arguments.pop('tab_id', None)

        # Clamp the tab listing cap where the agent cannot reach past it:
        # the extension honours whatever limit it is handed.
        if tool_name in ('browser_get_tabs', 'browser_find_tabs') \
                and 'limit' in arguments:
            arguments['limit'] = clamp_tab_limit(arguments['limit'])

        # Password guard: <input type="password"> is neither written nor read
        # back unless the safety config explicitly allows it. The flag travels
        # with the command so the enforcement happens where the element type is
        # visible. Reads are masked rather than refused, so inspection tools
        # still report whether a field is filled.
        if tool_name in ('browser_type', 'browser_set_value',
                         'browser_get_value', 'browser_get_elements',
                         'browser_get_page_info', 'browser_press_key'):
            arguments['allow_password'] = bool(
                get_safety_guard().config.get('allow_password_typing', False))

        return self._dispatch_action(action, tab_id, arguments)

    def _dispatch_action(self, action: str, tab_id: Optional[int],
                         arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Send a browser action over whichever transport is available
        (WebSocket, headless Playwright, or native-host HTTP polling)."""
        guard = get_safety_guard()

        # Prompts that wait on a human (approval, captcha solving) can take far
        # longer than a normal command, so give the transport matching headroom.
        if action == 'requestApproval':
            wait_timeout = 90.0
        elif action == 'solveCaptcha':
            wait_timeout = 200.0
        else:
            wait_timeout = 30.0

        # Check if we have a browser connection via WebSocket
        browser = connection_manager.get_active_browser()

        if browser:
            # Use async WebSocket communication
            command = BrowserCommand(
                action=action,
                tab_id=tab_id,
                data=camelize_args(arguments)
            )

            # We run in a ThreadingHTTPServer worker thread, which owns no
            # event loop (asyncio.get_event_loop() raises here on Python
            # 3.12+), so hand the coroutine to the main thread's loop.
            loop = MAIN_EVENT_LOOP
            if loop is not None and loop.is_running():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        connection_manager.send_command(command, timeout=wait_timeout),
                        loop
                    )
                    result = future.result(timeout=wait_timeout + 5)

                    # Before the screenshot branch, not after it. A screenshot
                    # result is the one result that always carries a URL
                    # ({tab: {id, url, title}}), and returning early from here
                    # meant it never reached the guard: after a click landed on
                    # a bank, the blocklist, the allowlist and the
                    # protected-site confirmation were all still judging the
                    # previous page while the agent photographed the new one.
                    guard.note_url(result)

                    # Handle screenshot saving
                    if action == 'screenshot' and result.get('success') and result.get('data'):
                        return self._save_screenshot(result, arguments)

                    return result
                except Exception as e:
                    logger.error(f"WebSocket command failed: {e}")
                    # Fall through to HTTP method
            else:
                logger.warning("WebSocket browser registered but no event loop running; "
                               "falling back to HTTP polling")

        # Try headless Playwright backend if enabled and available
        if HEADLESS_MODE:
            from headless_backend import get_headless_browser
            # Launching Playwright Firefox takes ~15s, but the HTTP server
            # accepts requests as soon as the port binds. Rather than failing
            # the first call of a session, wait for the browser to boot.
            deadline = time.monotonic() + HEADLESS_STARTUP_TIMEOUT
            headless = get_headless_browser()
            while time.monotonic() < deadline:
                if (MAIN_EVENT_LOOP is not None and MAIN_EVENT_LOOP.is_running()
                        and headless is not None and headless.is_ready()):
                    break
                time.sleep(0.5)
                headless = get_headless_browser()

            if headless is None or not headless.is_ready():
                return {'success': False,
                        'error': f'Headless browser did not start within '
                                 f'{HEADLESS_STARTUP_TIMEOUT:.0f}s. Check the server log.'}
            # Worker threads own no event loop; dispatch onto the loop started
            # by run_with_headless() in the main thread (issue #9).
            loop = MAIN_EVENT_LOOP
            if loop is None or not loop.is_running():
                return {'success': False,
                        'error': 'Headless event loop not running yet. Wait a moment and retry.'}
            try:
                future = asyncio.run_coroutine_threadsafe(
                    headless.execute(action, tab_id, arguments), loop
                )
                result = future.result(timeout=HEADLESS_CALL_TIMEOUT)
                guard.note_url(result)
                return result
            except concurrent.futures.TimeoutError:
                # Cancel, do not just give up. Giving up left the coroutine
                # running on the loop holding HeadlessBrowser's lock, so every
                # later headless tool blocked for the life of the process -
                # one slow call wedged the whole backend. The arguments are
                # capped so this should not be reachable, which is exactly why
                # it needs handling rather than hoping.
                future.cancel()
                logger.error(f"Headless {action} exceeded "
                             f"{HEADLESS_CALL_TIMEOUT}s; cancelled")
                return {
                    'success': False,
                    'error': f'The headless browser did not finish {action} '
                             f'within {HEADLESS_CALL_TIMEOUT}s. The call was '
                             'cancelled, so the backend is still usable.'
                }
            except Exception as e:
                logger.error(f"Headless backend failed: {e}")
                return {'success': False, 'error': f'Headless execution failed: {e}'}

        # No WebSocket connection — use HTTP polling with the native host.
        # ThreadingHTTPServer ensures /browser/poll and /browser/response are
        # served concurrently while this thread blocks waiting for the response.
        # time.time() collides for two dispatches in the same microsecond,
        # which crossed two waiters' responses over.
        request_id = secrets.token_hex(8)
        command_data = {
            'action': action,
            'tabId': tab_id,
            'data': camelize_args(arguments),
            'requestId': request_id
        }

        # Register a waiter before queuing so we never miss the response
        event = threading.Event()
        result_holder = {}
        connection_manager.http_pending_requests[request_id] = (event, result_holder)

        # Timestamped so a command the caller has already given up on is not
        # executed later. Observed live: two calls timed out while the browser
        # was disconnected and their commands sat in the queue, ready to run
        # whenever it came back. A stale getTabs is harmless; a stale click or
        # type would fire on whatever page happened to be loaded by then.
        command_data['queuedAt'] = time.time()
        with _PENDING_COMMANDS_LOCK:
            if not hasattr(self.server, '_pending_commands'):
                self.server._pending_commands = []
            self.server._pending_commands.append(command_data)

        logger.info(f"Queued command {action} (requestId={request_id}), waiting for browser response...")

        if event.wait(timeout=wait_timeout):
            connection_manager.http_pending_requests.pop(request_id, None)
            response = result_holder.get('response', {})
            # Note the URL first: see the WebSocket path above.
            guard.note_url(response)
            if action == 'screenshot' and response.get('success') and response.get('data'):
                return self._save_screenshot(response, arguments)
            return response
        else:
            connection_manager.http_pending_requests.pop(request_id, None)
            logger.warning(f"Command {action} timed out after {wait_timeout:.0f}s")
            return {
                'success': False,
                'error': f'Command {action} timed out waiting for browser response after {wait_timeout:.0f}s',
                'action': action
            }

    def _request_human_approval(self, tool_name: str, arguments: Dict[str, Any],
                                denial: Dict[str, Any],
                                tab_id: Optional[int] = None) -> Dict[str, Any]:
        """Show the in-browser Approve/Deny prompt for a protected action.

        The person is asked to approve a specific action, so they have to be
        able to see what it is: redacting the script or the text here left them
        approving {"script": "***"}, which is the only part that matters. The
        audit log still masks these; this is the human-facing copy.
        """
        _PREVIEW = {'text', 'script', 'value', 'password'}

        def shown(key, value):
            if key not in _PREVIEW:
                return value
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            return text if len(text) <= 300 else text[:300] + '…'

        shown_args = {k: shown(k, v) for k, v in arguments.items() if k != 'tab_id'}
        try:
            # Send to the tab the action will actually run on, not whichever
            # tab happens to be focused.
            return self._dispatch_action('requestApproval', tab_id, {
                'message': f"Claude wants to run {tool_name} on a protected site "
                           f"({denial.get('protected_url', 'unknown URL')}).",
                'detail': json.dumps(shown_args)[:500],
                'timeout': 60000
            })
        except Exception as e:
            logger.warning(f"Human approval request failed: {e}")
            return {'success': False, 'error': str(e)}

    def _run_workflow(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a declarative list of tool steps with assertions."""
        import re as _re
        steps = arguments.get('steps') or []
        stop_on_failure = parse_flag(arguments.get('stop_on_failure'), True)
        screenshot_on_failure = parse_flag(
            arguments.get('screenshot_on_failure'), True)
        known_tools = {t.name for t in MCP_TOOLS}

        results = []
        passed = 0
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                results.append({'label': f'step_{i+1}', 'success': False,
                                'error': 'Step must be an object'})
                break
            label = step.get('label') or f'step_{i+1}'
            tool = step.get('tool')
            entry = {'label': label, 'tool': tool}

            if tool not in known_tools or tool == 'browser_run_workflow':
                entry['success'] = False
                entry['error'] = f'Unknown or disallowed tool: {tool}'
            else:
                # Each step goes through execute_tool, so the safety guard
                # applies per step exactly as it would for a direct call.
                result = self.execute_tool(tool, dict(step.get('arguments') or {}))
                entry['success'] = bool(result.get('success'))
                entry['result'] = {k: v for k, v in result.items() if k != 'data'}

                assertion = step.get('assert') or {}
                if entry['success'] and assertion:
                    failures = self._check_assertions(assertion)
                    entry['assertions'] = {'passed': not failures, 'failures': failures}
                    if failures:
                        entry['success'] = False
                        entry['error'] = 'Assertions failed: ' + '; '.join(failures)

            if entry['success']:
                passed += 1
            elif screenshot_on_failure:
                safe_label = _re.sub(r'[^A-Za-z0-9_-]', '_', label)[:40]
                shot = self.execute_tool('browser_screenshot',
                                         {'filename': f'workflow_fail_{safe_label}.png'})
                if shot.get('filepath'):
                    entry['failure_screenshot'] = shot['filepath']

            results.append(entry)
            if not entry['success'] and stop_on_failure:
                break

        return {
            # A short-circuited run has executed_steps < total_steps, so
            # comparing against len(steps) reported failure for a run that
            # passed everything it attempted, and an empty steps list reported
            # success for doing nothing.
            'success': bool(steps) and passed == len(results) == len(steps),
            'passed': passed,
            'failed': len(results) - passed,
            'total_steps': len(steps),
            'executed_steps': len(results),
            'steps': results
        }

    def _check_assertions(self, assertion: Dict[str, Any]) -> List[str]:
        """Evaluate a step's assertions against the live page; return failures."""
        failures = []

        url_contains = assertion.get('url_contains')
        if url_contains:
            info = self.execute_tool('browser_get_page_info', {})
            url = info.get('url', '') if info.get('success') else ''
            if url_contains not in url:
                failures.append(f"url_contains {url_contains!r} (actual URL: {url!r})")

        text_contains = assertion.get('text_contains')
        if text_contains:
            text_result = self.execute_tool('browser_get_text', {'max_length': 100000})
            text = text_result.get('text', '') if text_result.get('success') else ''
            if text_contains not in text:
                failures.append(f"text_contains {text_contains!r} not found on page")

        selector_exists = assertion.get('selector_exists')
        if selector_exists:
            el_result = self.execute_tool('browser_get_elements',
                                          {'selector': selector_exists, 'limit': 1})
            elements = el_result.get('elements') or []
            if not (el_result.get('success') and len(elements) > 0):
                failures.append(f"selector_exists {selector_exists!r} matched nothing")

        return failures

    # Structural/accessibility audit collected in one page pass. Kept to
    # read-only DOM inspection — the audit tool is classified as observation.
    _AUDIT_JS = r"""
(function() {
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
    .slice(0, 60).map(h => ({level: h.tagName, text: (h.innerText || '').trim().slice(0, 120)}));
  const images = Array.from(document.querySelectorAll('img'));
  const imagesMissingAlt = images.filter(i => !i.hasAttribute('alt'))
    .slice(0, 30).map(i => (i.currentSrc || i.src || '').slice(0, 200));
  const inputs = Array.from(document.querySelectorAll('input:not([type=hidden]),select,textarea'));
  const unlabeled = inputs.filter(el => {
    if (el.labels && el.labels.length) return false;
    if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')) return false;
    if (el.getAttribute('placeholder')) return false;
    return true;
  }).slice(0, 30).map(el => (el.name || el.id || el.type || el.tagName).slice(0, 80));
  const emptyLinks = Array.from(document.querySelectorAll('a')).filter(a =>
    !(a.innerText || '').trim() && !a.getAttribute('aria-label') && !a.querySelector('img[alt]')
  ).length;
  const emptyButtons = Array.from(document.querySelectorAll('button')).filter(b =>
    !(b.innerText || '').trim() && !b.getAttribute('aria-label')
  ).length;
  const meta = {};
  const desc = document.querySelector('meta[name=description]');
  if (desc) meta.description = (desc.content || '').slice(0, 300);
  const viewportTag = document.querySelector('meta[name=viewport]');
  meta.hasViewportTag = !!viewportTag;
  return {
    title: document.title,
    url: location.href,
    lang: document.documentElement.lang || null,
    meta: meta,
    headings: headings,
    headingCounts: headings.reduce((acc, h) => { acc[h.level] = (acc[h.level] || 0) + 1; return acc; }, {}),
    imageCount: images.length,
    imagesMissingAlt: imagesMissingAlt,
    formInputCount: inputs.length,
    unlabeledInputs: unlabeled,
    emptyLinks: emptyLinks,
    emptyButtons: emptyButtons,
    linkCount: document.querySelectorAll('a').length,
    viewport: {width: window.innerWidth, height: window.innerHeight,
               pageHeight: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)}
  };
})()
"""

    def _audit_page(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Collect page structure + optional screenshot for visual critique."""
        tab_id = arguments.get('tab_id')
        script_args = {'script': self._AUDIT_JS}
        # Dispatched directly rather than through the action map, so no
        # executeScript call reaches the guard. The guard therefore judges
        # browser_audit_page itself against the script rules
        # (OBSERVE_SCRIPT_TOOLS in safety.py) before execute_tool gets here:
        # this used to be the one way to run JavaScript with
        # allow_script_execution: false.
        audit = self._dispatch_action('executeScript', tab_id, script_args)
        if not audit.get('success'):
            return {'success': False,
                    'error': f"Audit script failed: {audit.get('error', 'unknown')}"}

        response = {'success': True, 'audit': audit.get('result')}

        if parse_flag(arguments.get('screenshot'), True):
            from datetime import datetime as _dt
            shot = self.execute_tool('browser_screenshot', {
                'filename': f"audit_{_dt.now().strftime('%Y%m%d_%H%M%S')}.png",
                'full_page': parse_flag(arguments.get('full_page'), False),
                **({'tab_id': tab_id} if tab_id is not None else {})
            })
            response['screenshot'] = shot.get('filepath') if shot.get('success') else None

        return response

    def _save_screenshot(self, result: Dict[str, Any], arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Save screenshot data to file."""
        try:
            data = result.get('data', '')
            # bool("false") is True, so a caller that explicitly declined
            # still had the PNG written to disk.
            save_to_file = parse_flag(arguments.get('save_to_file'), True)
            generated = f'screenshot_{datetime.now().strftime("%Y%m%d_%H%M%S")}.png'
            # One spelling of both rules, shared with the headless backend.
            # They were two copies, and the .png correction was added to this
            # one only - so headless kept writing files the sweep skips. See
            # screenshot_filename in safety.py for what each rule is for.
            filename = screenshot_filename(arguments.get('filename'), generated)

            if not save_to_file:
                return result

            # A screenshot of a private window is not written to disk. The
            # image still goes to the agent, which is what the caller asked
            # for, but keeping a copy for a week is the longer-lived record
            # that private windows exist to prevent - and startLogging
            # already refuses a private tab for the same reason.
            if result.get('privateWindow') is True:
                return {
                    **result,
                    'saved': False,
                    'note': 'Not written to disk: this is a private-browsing '
                            'window. The image is in this response only.'
                }

            # Handle base64 data URL
            if data.startswith('data:image'):
                header, encoded = data.split(',', 1)
                image_data = base64.b64decode(encoded)
            else:
                image_data = base64.b64decode(data)

            filepath = SCREENSHOTS_DIR / filename
            # O_NOFOLLOW: with a shared CLAUDE_BROWSER_SCREENSHOTS_DIR an
            # attacker can pre-create a predictable timestamped name as a
            # symlink and have an arbitrary file truncated as this user.
            flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(str(filepath), flags, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(image_data)

            logger.info(f"Screenshot saved to: {filepath}")
            # Prune here rather than only at startup: a long-running server
            # would otherwise accumulate indefinitely.
            prune_screenshots(SCREENSHOTS_DIR)

            return {
                'success': True,
                'filepath': str(filepath),
                'filename': filename,
                'size': len(image_data),
                'tab': result.get('tab', {}),
                'message': f'Screenshot saved to {filepath}'
            }
        except Exception as e:
            logger.error(f"Failed to save screenshot: {e}")
            return {
                'success': False,
                'error': f'Failed to save screenshot: {str(e)}',
                'original_result': result
            }


# Guards self.server._pending_commands, which ThreadingHTTPServer workers
# append to and /browser/poll pops from concurrently.
_PENDING_COMMANDS_LOCK = threading.Lock()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a new thread.

    Required because execute_tool() blocks waiting for browser responses while
    /browser/poll and /browser/response must be served concurrently on the same port.
    """
    daemon_threads = True


def run_http_server():
    """Run the HTTP server."""
    server = ThreadingHTTPServer((HOST, HTTP_PORT), MCPHTTPHandler)
    logger.info(f"HTTP server starting on {HOST}:{HTTP_PORT}")
    server.serve_forever()


def parse_ws_origins(raw: Optional[str]) -> Optional[List[Optional[str]]]:
    """Parse CLAUDE_BROWSER_WS_ORIGINS into a websockets `origins` list.

    None in the list means "a request with no Origin header is acceptable",
    which is the only shape a local non-browser client has; a web page always
    sends its own origin, so it is refused at the handshake rather than ten
    seconds later when the token frame fails to arrive.

    The override used to *replace* that None, so naming one custom origin
    gave every local client a 403 - the one client shape that works today.
    It now adds to the default instead. Accepted values:

      unset           only no-Origin clients (the default)
      "moz-ext://a"   that origin, and no-Origin clients
      "a,b"           both origins, and no-Origin clients
      "*"             any origin: the handshake check is off and the API
                      token is the only control. Say so out loud.

    Empty or comma-only means unset rather than "allow nothing", because an
    empty allowlist that refuses everything is never what someone typing
    CLAUDE_BROWSER_WS_ORIGINS= wanted, and a server nothing can reach is not
    a safety feature.
    """
    if raw is None:
        return [None]
    entries = [entry.strip() for entry in raw.split(',')]
    entries = [entry for entry in entries if entry]
    if not entries:
        return [None]
    if '*' in entries:
        logger.warning(
            "CLAUDE_BROWSER_WS_ORIGINS=* accepts a WebSocket handshake from "
            "any origin, including any web page the user visits. The API "
            "token is then the only thing refusing it.")
        return None
    origins: List[Optional[str]] = [None]
    for entry in entries:
        if entry not in origins:
            origins.append(entry)
    return origins


# Origins accepted at the WebSocket handshake. Extension pages present a
# moz-extension:// origin; a local tool sends no Origin at all.
ALLOWED_WS_ORIGINS = parse_ws_origins(
    os.environ.get('CLAUDE_BROWSER_WS_ORIGINS'))


async def websocket_handler(websocket, path=None):
    """Handle WebSocket connections from browser extensions.

    path is optional: websockets>=14 no longer passes it to handlers.

    The first frame must be a JSON object carrying the API token; otherwise
    any local process could register as the browser and receive automation
    commands or forge responses.
    """
    try:
        first_frame = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        auth = json.loads(first_frame)
        authorized = isinstance(auth, dict) and secrets.compare_digest(
            str(auth.get('token', '')), API_TOKEN)
    except Exception:
        authorized = False

    if not authorized:
        logger.warning("WebSocket connection refused: missing or invalid token")
        try:
            await websocket.close(1008, 'auth required')
        except Exception:
            pass
        return

    browser_id = f"browser_{id(websocket)}"
    connection_manager.register_browser(browser_id, websocket)

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                logger.debug(f"Received from browser: {data}")

                if 'requestId' in data:
                    # This is a response to a command
                    connection_manager.handle_response(data)
                else:
                    # This is an event from the browser
                    logger.info(f"Browser event: {data.get('type', 'unknown')}")

            except json.JSONDecodeError:
                logger.error(f"Invalid JSON from browser: {message}")

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Browser disconnected: {browser_id}")
    finally:
        connection_manager.unregister_browser(browser_id)


async def run_websocket_server():
    """Run the WebSocket server."""
    global MAIN_EVENT_LOOP
    MAIN_EVENT_LOOP = asyncio.get_running_loop()

    if not HAS_WEBSOCKETS:
        logger.warning("websockets module not installed, WebSocket server disabled")
        return

    # WebSockets are exempt from CORS, so any page the user visits can open a
    # connection to this loopback port. The token check refuses it, but only
    # after the handshake completes and a task has been held for up to 10s
    # waiting for the first frame. Rejecting a foreign Origin at the handshake
    # closes it before that, and costs nothing: the extension connects from a
    # moz-extension:// origin, and a legitimate local tool sends no Origin.
    server = await websockets.serve(
        websocket_handler, HOST, WS_PORT, origins=ALLOWED_WS_ORIGINS)
    logger.info(f"WebSocket server starting on {HOST}:{WS_PORT} "
                f"(allowed origins: {ALLOWED_WS_ORIGINS})")
    await server.wait_closed()


async def run_with_headless():
    """Run WebSocket server alongside headless browser startup."""
    global MAIN_EVENT_LOOP
    from headless_backend import init_headless_browser
    # Publish this loop before awaiting anything slow, so HTTP worker threads
    # can dispatch onto it as soon as the browser is up.
    MAIN_EVENT_LOOP = asyncio.get_running_loop()
    headless = await init_headless_browser()
    logger.info("Headless browser ready")

    if HAS_WEBSOCKETS:
        await run_websocket_server()
    else:
        # No WebSocket but headless is running — just keep the event loop alive
        await asyncio.Event().wait()

    await headless.stop()


def main():
    """Main entry point."""
    mode_label = "HEADLESS (Playwright)" if HEADLESS_MODE else "EXTENSION (Firefox/native-host)"
    print(f"""
+--------------------------------------------------------------+
|          ClaudeCodeBrowserX MCP Server v1.9.6                |
+--------------------------------------------------------------+
|  Mode:             {mode_label:<40} |
|  HTTP Server:      http://{HOST}:{HTTP_PORT:<5}                       |
|  WebSocket Server: ws://{HOST}:{WS_PORT:<5}                         |
|  Screenshots:      {str(SCREENSHOTS_DIR):<40} |
|  Logs:             {str(LOG_FILE):<40} |
+--------------------------------------------------------------+
    """)

    # Start HTTP server in a thread (always needed)
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    if HEADLESS_MODE:
        asyncio.run(run_with_headless())
    elif HAS_WEBSOCKETS:
        asyncio.run(run_websocket_server())
    else:
        # If no websockets, just keep the HTTP server running
        logger.info("Running HTTP server only (install websockets for WebSocket support)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Server shutting down")


if __name__ == '__main__':
    main()

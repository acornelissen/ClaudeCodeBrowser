#!/usr/bin/env python3
"""
ClaudeCodeBrowser Agent

A specialized agent for browser automation that can be called from Claude Code.
This agent provides high-level browser automation capabilities using natural language.

MIT License
Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
Author: dre@ligandal.com
"""

import asyncio
import json
import os
import sys
import time
import base64
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass
import urllib.parse
import urllib.request
import urllib.error

# Configuration
MCP_SERVER_URL = os.environ.get('CLAUDE_BROWSER_URL', 'http://127.0.0.1:8765')
_TOKEN_FILE = Path.home() / '.claudecodebrowser' / 'api_token'

# Argument names whose values must never be logged or retained. This client
# printed them in the clear to the terminal, which lands in any tee'd session
# log or agent transcript.
#
# This is the THIRD copy of this list in the project, and it was the one left
# behind: the server and the safety guard now share one definition in
# safety.py, and `key` and `url` were added there while this copy still had
# neither. It cannot import from mcp-server/ - this is a standalone client
# that runs without the server package on the path - so it is a deliberate
# mirror, and tests/test_browser_agent.py pins it against safety.py's list so
# the two cannot drift again.
_SENSITIVE_ARGS = {'text', 'script', 'value', 'password', 'steps',
                   'action_script', 'condition', 'key', 'url_pattern'}

# Arguments that are URLs: reduced rather than masked, because which page was
# acted on is the thing a log is read for, while the userinfo, query and
# fragment are where a reset token, an SSO code or a password lives.
#
# Also matched as result keys: browser_navigate answers with the url it was
# asked for under `requestedUrl`, so the token the argument log just reduced
# came straight back whole in the logged result.
#
# href and protectedUrl are results only: get_elements lists each link's
# href, and a protected-site denial names the page it refused.
_URL_ARGS = {'url', 'requestedUrl', 'href', 'protectedUrl'}

_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1', '[::1]'}

# Shortest value worth scrubbing out of a server message. A one- or
# two-character value is indistinguishable from an ordinary word in prose, and
# type_text('', press_enter=True) - how this client presses Enter - would match
# at every position and blank the whole message.
_MIN_SCRUB_LEN = 3


def _is_loopback(url: str) -> bool:
    """True when the URL names this machine."""
    try:
        host = urllib.parse.urlparse(url).hostname
    except Exception:
        return False
    return host in _LOOPBACK_HOSTS or host == '127.0.0.1'


def _sensitive_values(arguments: dict) -> List[str]:
    """The values a call sent that must not come back to us in the clear.

    Redacting by key name cannot protect a free-text message: the extension
    throws `Element not found with options: ${JSON.stringify(options)}`, so the
    typed password arrives inside the 'error' string, where no key says
    "secret". What we do know is what we just sent, so that is what we look
    for.
    """
    return [value for key, value in (arguments or {}).items()
            if key in _SENSITIVE_ARGS and isinstance(value, str)
            and len(value) >= _MIN_SCRUB_LEN]


def _reduce_url(value):
    """Keep the scheme, host and path; drop userinfo, query and fragment.

    Mirrors redact_url() in mcp-server/safety.py. A URL in a scheme that is
    not http(s) is a payload rather than a location, so only its scheme name
    is kept.
    """
    if value is None or value == '':
        return value
    if not isinstance(value, str):
        # A URL key holding a dict or a list is unexpected, so it is masked
        # rather than passed through unexamined.
        return '***'
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return '***'
    if parts.scheme and parts.scheme not in ('http', 'https'):
        return f'{parts.scheme}:***' if parts.scheme != 'about' else value
    authority = parts.netloc
    if '@' in authority:
        authority = '***@' + authority.rpartition('@')[2]
    out = urllib.parse.urlunsplit(
        (parts.scheme, authority, parts.path, '', ''))
    if parts.query:
        out += '?***'
    if parts.fragment:
        out += '#***'
    return out


def _scrub(value, secrets):
    """Replace known sent values wherever they appear, at any depth.

    Masks a credential-shaped KEY at any depth as well. The key pass used to
    run only at the top level and inside `element`, so a value under a nested
    ordinary key - {'elements': [{'value': ...}]} from browser_get_elements -
    was caught only by the sent-value scrub, i.e. only if this client had sent
    it. Anything the page already held was not.
    """
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, '***')
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _SENSITIVE_ARGS:
                out[k] = '***'
            elif k in _URL_ARGS:
                out[k] = _reduce_url(v)
            else:
                out[k] = _scrub(v, secrets)
        return out
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    return value


def _redact_result(result, secrets=()):
    """A log-safe and history-safe copy of a tool result.

    Two passes, because neither one is enough alone: the keys that are known
    to carry secrets are masked, and the values this call sent are scrubbed
    out of everything else - 'error' above all, which the key pass never
    touched and which quotes the arguments back at us verbatim.
    """
    # One pass now: _scrub masks sensitive keys at every depth, so the
    # top-level and `element` special cases it used to need are gone - and
    # with them the gap at every other depth.
    return _scrub(result, secrets)


def _redact(arguments: dict) -> dict:
    """A log-safe and history-safe copy of a tool's arguments, at every depth.

    Top-level keys only used to be looked at, so a credential nested in an
    argument was printed whole. _scrub with no sent values does exactly the
    key pass, recursively.
    """
    return _scrub(arguments or {}, ())


def _api_headers(url: Optional[str] = None) -> dict:
    """Headers for a request, including the API token for a local server only.

    The token is full control of the user's browser. MCP_SERVER_URL comes from
    the environment with no validation, so attaching the token unconditionally
    meant one stray CLAUDE_BROWSER_URL in a shell profile or CI env sent it in
    cleartext to that host on the first call. Set
    CLAUDE_BROWSER_ALLOW_REMOTE=1 to override deliberately.
    """
    # Read MCP_SERVER_URL at call time, not as a default argument: bound at
    # definition time it froze the value the module was imported with, so a
    # later change to the server URL was judged against the old one and the
    # token went to the new host anyway.
    if url is None:
        url = MCP_SERVER_URL
    headers = {'Content-Type': 'application/json'}
    if not _is_loopback(url) and os.environ.get('CLAUDE_BROWSER_ALLOW_REMOTE') != '1':
        return headers
    try:
        if _TOKEN_FILE.exists():
            token = _TOKEN_FILE.read_text().strip()
            # An empty token file is "not configured", not an empty credential:
            # sending '' produced a 403 and told the user they were
            # unauthorised rather than that their token file was empty.
            if token:
                headers['X-API-Key'] = token
    except Exception:
        pass
    return headers


class BrowserAgentDenied(RuntimeError):
    """Raised when the safety guard refused a call.

    Distinct from "the page had nothing": a denial that returns the same
    empty value as a successful-but-empty read lets a caller report absent
    content when the guard simply refused to look.
    """


@dataclass
class BrowserAction:
    """Represents a browser automation action."""
    action_type: str
    description: str
    parameters: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None
    success: bool = False
    error: Optional[str] = None


class BrowserAutomationAgent:
    """
    Agent for browser automation tasks.

    This agent can interpret natural language commands and execute
    browser automation tasks through the MCP server.
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.action_history: List[BrowserAction] = []
        self.current_page_info: Optional[Dict[str, Any]] = None

    def log(self, message: str):
        """Log a message if verbose mode is enabled."""
        if self.verbose:
            print(f"[BrowserAgent] {message}")

    def _make_request(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make an HTTP request to the MCP server."""
        url = f"{MCP_SERVER_URL}{endpoint}"
        # The URL this request actually goes to decides whether the token
        # travels with it.
        headers = _api_headers(url)

        try:
            if data:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(data).encode('utf-8'),
                    headers=headers
                )
            else:
                req = urllib.request.Request(url, headers=headers)

            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read().decode('utf-8', errors='replace')
            try:
                return json.loads(body)
            except ValueError:
                # A proxy or captive portal answering 200 with HTML gave the
                # caller "Expecting value: line 1 column 1 (char 0)", which
                # says nothing about where the problem is.
                return {
                    'success': False,
                    'transport_error': 'invalid_response',
                    'error': 'The server did not return JSON. The URL may not '
                             f'be the MCP server: {body[:200]!r}'
                }

        # HTTPError is a subclass of URLError, so this has to come first.
        # Without it, a rejected token read as "Connection failed: HTTP Error
        # 403: Forbidden" - so the user restarted a server that was never
        # down - and the server's own {"error": "Unauthorized"} body was
        # discarded.
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', errors='replace')[:200]
            except Exception:
                pass
            return {
                'success': False,
                'transport_error': 'auth' if e.code in (401, 403) else 'http',
                'status': e.code,
                'error': f'The MCP server returned HTTP {e.code} '
                         f'{e.reason}.{" " + detail if detail else ""}'
            }
        except urllib.error.URLError as e:
            return {'success': False, 'transport_error': 'connection',
                    'error': f'Connection failed: {str(e)}'}
        except Exception as e:
            # transport_error, so a caller can tell "the request did not get
            # through, retry it" from "the tool ran and said no". Both used to
            # arrive as {'success': False, 'error': ...} and nothing else.
            return {'success': False, 'transport_error': 'request', 'error': str(e)}

    def call_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """Call an MCP tool."""
        self.log(f"Calling tool: {tool_name}")
        self.log(f"Arguments: {_redact(kwargs)}")

        secrets = _sensitive_values(kwargs)

        result = self._make_request('/mcp/call', {
            'name': tool_name,
            'arguments': kwargs
        })
        safe_result = _redact_result(result, secrets)

        action = BrowserAction(
            action_type=tool_name,
            description=f"Called {tool_name}",
            # Redacted: a plaintext password stored here stayed reachable for
            # the life of the agent, in any repr() and in any traceback that
            # renders the frame.
            parameters=_redact(kwargs),
            result=safe_result,
            success=result.get('success', False),
            # The scrubbed copy, not result['error']: the extension's
            # not-found error quotes the options it was given, so the raw
            # string holds the password that was just typed.
            error=safe_result.get('error')
        )
        self.action_history.append(action)

        if self.verbose:
            if result.get('success'):
                # Results can carry a credential back: browser_get_value
                # returns a field's contents, and the extension's
                # element-not-found error embeds the whole options dict.
                self.log(f"Success: {safe_result}")
            else:
                self.log(f"Error: {safe_result.get('error')}")

        return result

    def check_server(self) -> bool:
        """Check if the MCP server is running."""
        result = self._make_request('/health')
        return result.get('status') == 'ok'

    # High-level actions

    def screenshot(self, filename: Optional[str] = None, full_page: bool = False) -> Dict[str, Any]:
        """Take a screenshot of the current page."""
        return self.call_tool(
            'browser_screenshot',
            filename=filename,
            full_page=full_page,
            save_to_file=True
        )

    def navigate(self, url: str, new_tab: bool = False) -> Dict[str, Any]:
        """Navigate to a URL."""
        return self.call_tool('browser_navigate', url=url, new_tab=new_tab)

    def click(self,
              selector: Optional[str] = None,
              text: Optional[str] = None,
              xpath: Optional[str] = None,
              x: Optional[int] = None,
              y: Optional[int] = None,
              double_click: bool = False,
              right_click: bool = False) -> Dict[str, Any]:
        """Click on an element."""
        kwargs = {}
        if selector:
            kwargs['selector'] = selector
        if text:
            kwargs['text'] = text
        if xpath:
            kwargs['xpath'] = xpath
        if x is not None and y is not None:
            kwargs['x'] = x
            kwargs['y'] = y
        kwargs['double_click'] = double_click
        kwargs['right_click'] = right_click

        return self.call_tool('browser_click', **kwargs)

    def type_text(self,
                  text: str,
                  selector: Optional[str] = None,
                  placeholder: Optional[str] = None,
                  name: Optional[str] = None,
                  element_id: Optional[str] = None,
                  clear: bool = False,
                  press_enter: bool = False) -> Dict[str, Any]:
        """Type text into an element."""
        kwargs = {'text': text, 'clear': clear, 'press_enter': press_enter}
        if selector:
            kwargs['selector'] = selector
        if placeholder:
            kwargs['placeholder'] = placeholder
        if name:
            kwargs['name'] = name
        if element_id:
            kwargs['id'] = element_id

        return self.call_tool('browser_type', **kwargs)

    def scroll(self,
               direction: str = 'down',
               amount: int = 300,
               to_element: Optional[str] = None) -> Dict[str, Any]:
        """Scroll the page."""
        kwargs = {'direction': direction, 'amount': amount}
        if to_element:
            kwargs['to_element'] = to_element

        return self.call_tool('browser_scroll', **kwargs)

    def get_page_info(self) -> Dict[str, Any]:
        """Get information about the current page."""
        result = self.call_tool('browser_get_page_info')
        if result.get('success'):
            self.current_page_info = result
        return result

    def get_elements(self, selector: str, limit: int = 50) -> Dict[str, Any]:
        """Get elements matching a selector."""
        return self.call_tool('browser_get_elements', selector=selector, limit=limit)

    def wait_for_element(self, selector: str, timeout: int = 10000, visible: bool = True) -> Dict[str, Any]:
        """Wait for an element to appear."""
        return self.call_tool('browser_wait_for_element', selector=selector, timeout=timeout, visible=visible)

    def highlight(self, selector: str, duration: int = 3000, label: Optional[str] = None) -> Dict[str, Any]:
        """Highlight an element."""
        kwargs = {'selector': selector, 'duration': duration}
        if label:
            kwargs['label'] = label
        return self.call_tool('browser_highlight', **kwargs)

    def execute_script(self, script: str) -> Dict[str, Any]:
        """Execute JavaScript in the browser."""
        return self.call_tool('browser_execute_script', script=script)

    def get_tabs(self) -> Dict[str, Any]:
        """Get list of browser tabs."""
        return self.call_tool('browser_get_tabs')

    def new_tab(self, url: str = 'about:blank') -> Dict[str, Any]:
        """Create a new tab."""
        return self.call_tool('browser_create_tab', url=url)

    def close_tab(self, tab_id: int) -> Dict[str, Any]:
        """Close a tab."""
        return self.call_tool('browser_close_tab', tab_id=tab_id)

    def focus_tab(self, tab_id: int) -> Dict[str, Any]:
        """Focus a tab."""
        return self.call_tool('browser_focus_tab', tab_id=tab_id)

    def get_value(self, selector: str) -> Dict[str, Any]:
        """Get the value of an input element."""
        return self.call_tool('browser_get_value', selector=selector)

    def set_value(self, selector: str, value: str) -> Dict[str, Any]:
        """Set the value of an input element."""
        return self.call_tool('browser_set_value', selector=selector, value=value)

    def select_option(self, selector: str, value: Optional[str] = None, text: Optional[str] = None, index: Optional[int] = None) -> Dict[str, Any]:
        """Select an option in a dropdown."""
        kwargs = {'selector': selector}
        if value:
            kwargs['value'] = value
        if text:
            kwargs['text'] = text
        if index is not None:
            kwargs['index'] = index
        return self.call_tool('browser_select_option', **kwargs)

    def hover(self, selector: str) -> Dict[str, Any]:
        """Hover over an element."""
        return self.call_tool('browser_hover', selector=selector)

    # Refresh/Reload functionality

    def refresh(self, bypass_cache: bool = False, wait_for_load: bool = True) -> Dict[str, Any]:
        """Refresh the current page."""
        return self.call_tool('browser_refresh', bypass_cache=bypass_cache, wait_for_load=wait_for_load)

    def hard_refresh(self) -> Dict[str, Any]:
        """Force refresh bypassing cache (like Ctrl+Shift+R)."""
        return self.call_tool('browser_hard_refresh')

    def reload_all(self, url_pattern: Optional[str] = None, bypass_cache: bool = True) -> Dict[str, Any]:
        """Reload all browser tabs, optionally filtered by URL pattern."""
        kwargs = {'bypass_cache': bypass_cache}
        if url_pattern:
            kwargs['url_pattern'] = url_pattern
        return self.call_tool('browser_reload_all', **kwargs)

    def reload_by_url(self, url: Optional[str] = None, url_pattern: Optional[str] = None, bypass_cache: bool = True) -> Dict[str, Any]:
        """Reload all tabs matching a specific URL or pattern."""
        kwargs = {'bypass_cache': bypass_cache}
        if url:
            kwargs['url'] = url
        if url_pattern:
            kwargs['url_pattern'] = url_pattern
        return self.call_tool('browser_reload_by_url', **kwargs)

    def reload_localhost(self, port: Optional[int] = None) -> Dict[str, Any]:
        """Reload all localhost tabs. Optionally filter by port number."""
        # `if port:` made port=0 mean "every localhost tab", which is the
        # opposite of naming one port, and --reload-localhost 0 reaches here.
        # 0 is not a port a server listens on, so refuse it rather than
        # quietly widening the request.
        if port is not None:
            if not 1 <= int(port) <= 65535:
                return {'success': False,
                        'error': f'{port} is not a usable port number (1-65535).'}
            return self.reload_by_url(url=f'http://localhost:{port}')
        return self.reload_by_url(url_pattern=r'https?://localhost')

    def reload_dev_servers(self) -> Dict[str, Any]:
        """Reload all common dev server tabs (localhost, 127.0.0.1, dev domains)."""
        return self.reload_by_url(url_pattern=r'https?://(localhost|127\.0\.0\.1|.*\.local|.*\.dev)')

    # Workflow helpers

    @staticmethod
    def _refusal(result: Dict[str, Any]) -> Optional[str]:
        """The reason the call was refused, if it was refused.

        A refusal is not a wrong locator and not an empty page, so no caller
        should retry it or report it as an absence. Two sources: the safety
        guard names its decision, and the extension's own credential guard
        answers with a plain "Refused: ..." message and no decision field.
        """
        if not isinstance(result, dict):
            return None
        decision = result.get('safety_decision')
        if decision:
            return decision
        error = result.get('error')
        if isinstance(error, str) and error.startswith('Refused:'):
            return 'browser_refused'
        return None

    @staticmethod
    def _looks_like_a_wrong_locator(result: Dict[str, Any]) -> bool:
        """True only when the failure was "nothing matched that locator".

        fill_form retries with a different locator, and the retry is only
        right for this one case. It used to retry on ANY failure, so a
        password-field refusal posted the secret to the server a second time -
        and the extension's refusal carries no safety_decision, so testing
        for one was not enough on its own.
        """
        if not isinstance(result, dict):
            return False
        error = result.get('error')
        return isinstance(error, str) and 'not found' in error.lower()

    @staticmethod
    def _aborted(message: str) -> Dict[str, Any]:
        """A visible "this sequence stopped here" entry.

        A caller - often an LLM - reads the last entry of a returned list as
        the outcome, so a sequence that gave up halfway has to say so there
        rather than ending on whatever the last attempted step reported.
        """
        return {'success': False, 'aborted': True, 'error': message}

    @staticmethod
    def _safe_step(result: Dict[str, Any], sent: str) -> Dict[str, Any]:
        """A step's result with the value it typed scrubbed out of it.

        The two helpers that type credentials return a list of step results,
        and a caller prints that list. The extension's not-found error quotes
        the options it was handed, so the raw result carries the password in
        its 'error' string.
        """
        return _redact_result(result, _sensitive_values({'text': sent}))

    def fill_form(self, fields: Dict[str, str], submit: bool = False, submit_selector: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fill a form with the given field values.

        Args:
            fields: Dictionary mapping field selectors/names to values
            submit: Whether to submit the form after filling
            submit_selector: CSS selector for the submit button
        """
        results = []

        for field, value in fields.items():
            # Try different strategies to find the field
            if field.startswith('#') or field.startswith('.') or field.startswith('['):
                result = self.type_text(value, selector=field, clear=True)
            elif '=' in field:
                result = self.type_text(value, selector=f'[{field}]', clear=True)
            else:
                # Try by name, then by placeholder. The retry exists for a
                # wrong locator, so it must not run after a refusal: a
                # password-field refusal posted the secret to the server a
                # second time, and confirmation_required means stop and ask
                # the human rather than work around it with another locator.
                result = self.type_text(value, name=field, clear=True)
                if (not result.get('success')
                        and self._looks_like_a_wrong_locator(result)):
                    result = self.type_text(value, placeholder=field, clear=True)

            safe = self._safe_step(result, value)
            results.append(safe)

            if not result.get('success'):
                # Stop the sequence. The field is empty, so filling the rest
                # and then submitting posts a form with a missing value - with
                # a refused password field that is a real failed login
                # attempt against the real site, repeatable until the account
                # locks.
                results.append(self._aborted(
                    f'Form fill aborted: "{field}" could not be filled '
                    f'({safe.get("error") or "refused"}). The remaining '
                    'fields were skipped and the form was NOT submitted.'))
                return results

        if submit:
            if submit_selector:
                results.append(self.click(selector=submit_selector))
            else:
                # Try common submit selectors. Every failure used to append
                # nothing, so a form that was never submitted returned a
                # results list that read as a clean fill - the caller could
                # not tell. Report the last attempt, and stop early on a
                # refusal rather than trying the next selector.
                attempted = None
                for selector in ['button[type="submit"]', 'input[type="submit"]',
                                 'button:contains("Submit")', '.submit-btn']:
                    attempted = self.click(selector=selector)
                    if attempted.get('success') or self._refusal(attempted):
                        break
                results.append(attempted if attempted is not None else {
                    'success': False,
                    'error': 'No submit button was tried.'
                })

        return results

    def search(self, query: str, search_selector: str = 'input[type="search"], input[name="q"], #search') -> Dict[str, Any]:
        """Perform a search on the current page."""
        # The result of typing the query used to be discarded and only the
        # Enter keypress reported, so a refused query plus a successful Enter
        # read as a successful search of nothing.
        typed = self.type_text(query, selector=search_selector, clear=True)
        if not typed.get('success'):
            # Scrubbed, like the other helpers that return a step's result:
            # the not-found error quotes the text it was asked to type.
            return self._safe_step(typed, query)
        return self.type_text('', selector=search_selector, press_enter=True)

    def login(self, username: str, password: str,
              username_selector: str = '#username, input[name="username"], input[type="email"]',
              password_selector: str = '#password, input[name="password"], input[type="password"]',
              submit_selector: Optional[str] = None) -> List[Dict[str, Any]]:
        """Perform a login. Requires allow_password_typing in safety.json.

        Every step is checked before the next one runs. Submitting after a
        refused step is a real failed login attempt against the real site -
        an empty username with a real password, or a real username with an
        empty password - and repeated that way it locks the account out. An
        earlier version checked only the password step, so a refused username
        was still followed by typing the password and pressing Enter, and the
        returned list ended on a step that said success.
        """
        results = []
        for step, text, selector in (('username', username, username_selector),
                                     ('password', password, password_selector)):
            result = self.type_text(text, selector=selector, clear=True)
            safe = self._safe_step(result, text)
            results.append(safe)
            if not result.get('success'):
                results.append(self._aborted(
                    f'Login aborted: the {step} could not be entered '
                    f'({safe.get("error") or "refused"}). The form was NOT '
                    'submitted and no later step ran. Credentials are refused '
                    "by default - use the browser's own password manager, or "
                    'set "allow_password_typing": true in '
                    '~/.claudecodebrowser/safety.json.'))
                return results

        if submit_selector:
            results.append(self.click(selector=submit_selector))
        else:
            # Only reached when the password was accepted, so pressing Enter
            # submits a complete form rather than an empty credential.
            results.append(self.type_text('', selector=password_selector,
                                          press_enter=True))

        return results

    def extract_text(self, selector: str) -> Optional[str]:
        """Extract text content from elements matching selector."""
        # json.dumps, not interpolation: a selector containing a quote closed
        # the literal and the rest executed as code, and an ordinary escaped
        # selector like ".md\\:flex" lost its backslash and silently matched
        # nothing.
        result = self.execute_script(f"""
            const elements = document.querySelectorAll({json.dumps(selector)});
            return Array.from(elements).map(el => el.textContent.trim()).filter(t => t).join('\\n');
        """)
        if result.get('success'):
            return result.get('result')
        # A safety denial is not "no matching text". Returning None for both
        # let an agent report that content was absent when the guard refused
        # to look at it.
        decision = self._refusal(result)
        if decision:
            raise BrowserAgentDenied(
                f"extract_text refused by the safety guard ({decision}): "
                f"{result.get('error', 'no reason given')}")
        return None

    def extract_links(self, selector: str = 'a[href]') -> List[Dict[str, str]]:
        """Extract links from the page."""
        result = self.execute_script(f"""
            const links = document.querySelectorAll({json.dumps(selector)});
            return Array.from(links).map(a => ({{
                text: a.textContent.trim(),
                href: a.href
            }})).filter(l => l.href);
        """)
        if result.get('success') and result.get('result'):
            return result['result']
        # [] for a refusal told the caller the page had no links, when the
        # guard had refused to look. Same rule as extract_text.
        decision = self._refusal(result)
        if decision:
            raise BrowserAgentDenied(
                f"extract_links refused by the safety guard ({decision}): "
                f"{result.get('error', 'no reason given')}")
        return []


def interactive_mode(agent: BrowserAutomationAgent):
    """Run the agent in interactive mode."""
    print("""
╔══════════════════════════════════════════════════════════════╗
║          ClaudeCodeBrowser Interactive Agent                 ║
╠══════════════════════════════════════════════════════════════╣
║  Commands:                                                   ║
║    screenshot [filename]  - Take a screenshot                ║
║    navigate <url>         - Go to a URL                      ║
║    click <selector>       - Click an element                 ║
║    type <text>            - Type text                        ║
║    scroll <direction>     - Scroll the page                  ║
║    refresh                - Refresh current page             ║
║    hardrefresh            - Force refresh (bypass cache)     ║
║    reloadall [pattern]    - Reload all/matching tabs         ║
║    info                   - Get page information             ║
║    tabs                   - List browser tabs                ║
║    help                   - Show all commands                ║
║    exit                   - Exit interactive mode            ║
╚══════════════════════════════════════════════════════════════╝
    """)

    while True:
        try:
            cmd = input("\n> ").strip()
            if not cmd:
                continue

            parts = cmd.split(maxsplit=1)
            command = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ''

            if command == 'exit' or command == 'quit':
                print("Goodbye!")
                break

            elif command == 'screenshot':
                result = agent.screenshot(filename=args if args else None)
                print(json.dumps(result, indent=2))

            elif command == 'navigate' or command == 'goto' or command == 'go':
                if not args:
                    print("Usage: navigate <url>")
                    continue
                result = agent.navigate(args)
                print(json.dumps(result, indent=2))

            elif command == 'click':
                if not args:
                    print("Usage: click <selector>")
                    continue
                result = agent.click(selector=args)
                print(json.dumps(result, indent=2))

            elif command == 'type':
                if not args:
                    print("Usage: type <text>")
                    continue
                result = agent.type_text(args)
                # The not-found error quotes the text it was asked to type, so
                # printing the result raw echoes a typed credential into the
                # session transcript.
                print(json.dumps(
                    _redact_result(result, _sensitive_values({'text': args})),
                    indent=2))

            elif command == 'scroll':
                direction = args if args else 'down'
                result = agent.scroll(direction=direction)
                print(json.dumps(result, indent=2))

            elif command == 'refresh' or command == 'reload':
                result = agent.refresh()
                print(json.dumps(result, indent=2))

            elif command == 'hardrefresh' or command == 'hard-refresh' or command == 'force-refresh':
                result = agent.hard_refresh()
                print(json.dumps(result, indent=2))

            elif command == 'reloadall' or command == 'reload-all':
                result = agent.reload_all(url_pattern=args if args else None)
                print(json.dumps(result, indent=2))

            elif command == 'reloadlocal' or command == 'reload-localhost':
                port = int(args) if args and args.isdigit() else None
                result = agent.reload_localhost(port=port)
                print(json.dumps(result, indent=2))

            elif command == 'reloaddev' or command == 'reload-dev':
                result = agent.reload_dev_servers()
                print(json.dumps(result, indent=2))

            elif command == 'info' or command == 'pageinfo':
                result = agent.get_page_info()
                print(json.dumps(result, indent=2))

            elif command == 'tabs':
                result = agent.get_tabs()
                print(json.dumps(result, indent=2))

            elif command == 'elements':
                if not args:
                    print("Usage: elements <selector>")
                    continue
                result = agent.get_elements(args)
                print(json.dumps(result, indent=2))

            elif command == 'highlight':
                if not args:
                    print("Usage: highlight <selector>")
                    continue
                result = agent.highlight(args)
                print(json.dumps(result, indent=2))

            elif command == 'exec' or command == 'js':
                if not args:
                    print("Usage: exec <javascript>")
                    continue
                result = agent.execute_script(args)
                print(json.dumps(
                    _redact_result(result, _sensitive_values({'script': args})),
                    indent=2))

            elif command == 'help':
                print("""
Available commands:
  screenshot [filename]    - Take a screenshot
  navigate/goto <url>      - Navigate to a URL
  click <selector>         - Click on an element
  type <text>              - Type text into focused element
  scroll [up|down|left|right|top|bottom]  - Scroll the page
  refresh                  - Refresh current page
  hardrefresh              - Force refresh (bypass cache, like Ctrl+Shift+R)
  reloadall [pattern]      - Reload all tabs (optionally matching pattern)
  reloadlocal [port]       - Reload all localhost tabs
  reloaddev                - Reload all dev server tabs
  info                     - Get page information
  tabs                     - List browser tabs
  elements <selector>      - Find elements
  highlight <selector>     - Highlight an element
  exec <js>                - Execute JavaScript
  help                     - Show this help
  exit                     - Exit interactive mode
                """)

            else:
                print(f"Unknown command: {command}. Type 'help' for available commands.")

        except KeyboardInterrupt:
            print("\nInterrupted. Type 'exit' to quit.")
        except EOFError:
            # A piped or closed stdin raises EOFError from input(). The bare
            # `except Exception` below caught it, printed "Error: " and asked
            # for input again, so the process spun on a dead stdin instead of
            # exiting.
            print("\nEnd of input. Goodbye!")
            break
        except Exception as e:
            print(f"Error: {e}")


# The commands --command may reach. Anything not named here is not a command,
# however callable it happens to be. Every name must also be a method that
# exists: 'get_text' was listed here and is not one, so '--command get_text'
# answered with an AttributeError traceback.
COMMAND_METHODS = frozenset({
    'navigate', 'screenshot', 'click', 'type_text', 'get_page_info',
    'scroll', 'wait_for_element', 'extract_text', 'extract_links',
    'execute_script', 'refresh', 'hard_refresh', 'reload_all',
    'reload_localhost', 'reload_dev_servers', 'search', 'check_server',
})

# Commands whose single positional argument is sent to the page under a
# sensitive argument name, so the result may quote it back: the extension's
# not-found error embeds the options it was given. Printing the result raw
# puts that on stdout.
_COMMAND_SENSITIVE_ARG = {'type_text': 'text', 'search': 'text',
                          'execute_script': 'script'}


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='ClaudeCodeBrowser Agent')
    parser.add_argument('--interactive', '-i', action='store_true', help='Run in interactive mode')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    parser.add_argument('--check', action='store_true', help='Check if MCP server is running')
    parser.add_argument('--command', '-c', help='Execute a single command')
    parser.add_argument('--screenshot', '-s', nargs='?', const='screenshot.png', help='Take a screenshot')
    parser.add_argument('--navigate', '-n', help='Navigate to URL')
    parser.add_argument('--info', action='store_true', help='Get page info')
    parser.add_argument('--refresh', '-r', action='store_true', help='Refresh current page')
    parser.add_argument('--hard-refresh', '-R', action='store_true', help='Hard refresh (bypass cache)')
    parser.add_argument('--reload-all', action='store_true', help='Reload all browser tabs')
    parser.add_argument('--reload-localhost', nargs='?', const=True, help='Reload localhost tabs (optionally specify port)')
    parser.add_argument('--reload-dev', action='store_true', help='Reload all dev server tabs')
    parser.add_argument('--reload-url', help='Reload tabs matching URL pattern')

    args = parser.parse_args()

    agent = BrowserAutomationAgent(verbose=args.verbose)

    if args.check:
        if agent.check_server():
            print("MCP server is running")
            sys.exit(0)
        else:
            print("MCP server is not available")
            sys.exit(1)

    if args.screenshot:
        result = agent.screenshot(filename=args.screenshot)
        print(json.dumps(result, indent=2))

    elif args.navigate:
        result = agent.navigate(args.navigate)
        print(json.dumps(result, indent=2))

    elif args.info:
        result = agent.get_page_info()
        print(json.dumps(result, indent=2))

    elif args.refresh:
        result = agent.refresh()
        print(json.dumps(result, indent=2))

    elif args.hard_refresh:
        result = agent.hard_refresh()
        print(json.dumps(result, indent=2))

    elif args.reload_all:
        result = agent.reload_all()
        print(json.dumps(result, indent=2))

    elif args.reload_localhost:
        port = int(args.reload_localhost) if isinstance(args.reload_localhost, str) and args.reload_localhost.isdigit() else None
        result = agent.reload_localhost(port=port)
        print(json.dumps(result, indent=2))

    elif args.reload_dev:
        result = agent.reload_dev_servers()
        print(json.dumps(result, indent=2))

    elif args.reload_url:
        result = agent.reload_by_url(url_pattern=args.reload_url)
        print(json.dumps(result, indent=2))

    elif args.command:
        # Parse and execute a command string
        parts = args.command.split(maxsplit=1)
        cmd = parts[0]
        cmd_args = parts[1] if len(parts) > 1 else ''

        # An allowlist, not getattr on whatever arrives. getattr dispatched to
        # any attribute: '--command __init__' re-ran the constructor and wiped
        # the action history, printing "null" as though nothing had gone
        # wrong, and '--command log oops' reached an internal helper.
        if cmd not in COMMAND_METHODS:
            print(f"Unknown command: {cmd}")
            print(f"Try one of: {', '.join(sorted(COMMAND_METHODS))}")
        else:
            try:
                method = getattr(agent, cmd)
                result = method(cmd_args) if cmd_args else method()
            except TypeError as e:
                # login(username, password) called one argument short used to
                # escape main() as a traceback.
                print(f"Unknown command: {cmd} does not take those arguments "
                      f"({e}).")
            except BrowserAgentDenied as e:
                print(f"{cmd} was refused: {e}")
            except Exception as e:
                # TypeError alone was too narrow: 'extract_text div' raises
                # BrowserAgentDenied on a denial and 'reload_localhost <url>'
                # raises ValueError from int(), and both reached the user as a
                # traceback. Name the exception type, so a real bug in here is
                # still identifiable instead of silently swallowed.
                print(f"{cmd} failed: {type(e).__name__}: {e}")
            else:
                # Scrub what we typed out of the printed result, for the same
                # reason call_tool scrubs it out of its log.
                sensitive = _COMMAND_SENSITIVE_ARG.get(cmd)
                secrets = (_sensitive_values({sensitive: cmd_args})
                           if sensitive else ())
                print(json.dumps(_redact_result(result, secrets), indent=2))

    elif args.interactive:
        if not agent.check_server():
            print("Warning: MCP server is not available. Start the server first.")
        interactive_mode(agent)

    else:
        # Default: show help
        parser.print_help()


if __name__ == '__main__':
    main()

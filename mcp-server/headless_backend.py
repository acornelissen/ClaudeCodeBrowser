#!/usr/bin/env python3
"""
Headless browser backend for ClaudeCodeBrowser.

Uses Playwright to drive Firefox, Chromium, or WebKit without a display.
Activated when CLAUDE_BROWSER_HEADLESS=1 or --headless is passed.
Pick the engine with CLAUDE_BROWSER_ENGINE=firefox|chromium|webkit (default firefox).

Install: pip install playwright && playwright install firefox   (or chromium/webkit)

MIT License
Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.
Author: dre@ligandal.com
"""

import asyncio
import base64
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional

from safety import prune_screenshots, resolve_screenshots_dir

logger = logging.getLogger('ClaudeCodeBrowser.Headless')

SCREENSHOTS_DIR = resolve_screenshots_dir()

# Floor for waitAndAct's poll interval, in ms. See the comment there: 0 and
# negative values turned the poll loop into an infinite one.
MIN_POLL_INTERVAL_MS = 10

# Ceiling for waitAndAct's timeout, in ms. The poll floor above fixed half
# the wedge and left the other half: timeout_ms stayed unbounded and
# browser_wait_and_act's schema declares no maximum, so timeout_ms = 10**12
# (about 31 years) held HeadlessBrowser._lock for the life of the server.
# server.py abandons the future at 35s but does not cancel the coroutine, so
# the lock stayed held and every later headless tool blocked for ever. Kept
# under that 35s deadline so the call always finishes before the caller
# stops waiting for it.
MAX_WAIT_AND_ACT_TIMEOUT_MS = 30000

# Firefox vs Chromium vs WebKit: default Firefox to match the visible-mode extension
BROWSER_TYPE = os.environ.get('CLAUDE_BROWSER_ENGINE', 'firefox')

# Optional path to a browser executable. Lets headless mode use a system
# browser or a pre-installed Playwright build at a nonstandard revision,
# instead of requiring "playwright install".
EXECUTABLE_PATH = os.environ.get('CLAUDE_BROWSER_EXECUTABLE')


# Argument coercion, mirroring parseFlag() in extension/background.js.
#
# MCP clients hand-write JSON, so a boolean argument arrives as "false", "0"
# or 0 often enough to matter: a bare `if args.get('flag')` read the string
# "false" as true. That was a confirmed live bug on the extension side
# (capture_bodies: "false" started a capture anyway), and this file had the
# same class of bug in six places. Numbers arrive as strings for the same
# reason, and `"15000" / 1000` raised TypeError from inside a poll loop.
#
# The leniency is for FEATURE arguments only. allow_password stays a strict
# `is True` everywhere in this file: a fail-closed security switch must not be
# unlocked by anything that merely looks truthy.
_TRUE_WORDS = ('true', '1', 'yes', 'on')
_FALSE_WORDS = ('false', '0', 'no', 'off')


def parse_flag(value: Any, fallback: bool) -> bool:
    """Read a boolean argument that may have arrived as a string or number."""
    if value is None or value == '':
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return fallback


def parse_number(value: Any, fallback: float) -> float:
    """Read a numeric argument that may have arrived as a string.

    Anything unparseable, infinite or NaN falls back: these values end up as
    Playwright timeouts, loop increments and JS literals, where inf and nan
    are worse than a sane default.
    """
    if value is None or value == '' or isinstance(value, bool):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return number


def parse_int(value: Any, fallback: int) -> int:
    """parse_number for arguments that must be whole (lengths, limits, ms)."""
    return int(parse_number(value, fallback))


def screenshot_filename(requested: Any, generated: str) -> str:
    """The name a screenshot is written under: no directories, always .png.

    Both rules exist for a bug, and both paths need both of them.

    Path('..').name is '..' and Path('.').name is '', so a filename made of
    nothing but directory components survived the .name strip and resolved to
    the screenshots directory itself or its parent, where the write failed
    with an IsADirectoryError naming a path the caller never asked for.

    And the payload is always a PNG, while prune_screenshots and
    GET /screenshots both look for *.png - so filename "dashboard.jpg" was
    written, never listed and never pruned, leaving the longest-lived copy of
    the user's screen in the project. The suffix is corrected rather than the
    sweep widened: pruning must never consider a file this project did not
    write. _save_screenshot in server.py corrected it for the attended path
    and this branch was missed, so the retention fix the CHANGELOG describes
    as project-wide did not apply in headless mode at all.
    """
    filename = Path(requested or generated).name
    if filename in ('', '.', '..'):
        filename = generated
    if not filename.endswith('.png'):
        filename = Path(filename).with_suffix('.png').name
    return filename


class CredentialProbeFailed(RuntimeError):
    """The credential check could not be evaluated.

    Treated as a refusal rather than a pass: a guard that cannot tell must not
    let the call through. The message says so, because this surfaces to the
    agent as the reason its call did not happen.
    """

    def __init__(self, detail: str):
        super().__init__(
            'Refused: could not determine whether the target is a credential '
            f'field ({detail}). Refusing rather than risk handling a '
            'credential.')


class HeadlessBrowser:
    """Playwright-backed headless browser. One persistent context per server lifetime."""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        # Real tab management: id -> Page, mirroring the extension's tab ids
        self._tabs = {}
        self._next_tab_id = 1
        self._active_tab_id = None

    async def start(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "playwright not installed. Run: pip install playwright && playwright install firefox"
            )

        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, BROWSER_TYPE)
        launch_kwargs = {'headless': True}
        if EXECUTABLE_PATH:
            launch_kwargs['executable_path'] = EXECUTABLE_PATH
        self._browser = await launcher.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            viewport={'width': 1280, 'height': 800}
        )
        self._page = await self._context.new_page()
        self._register_tab(self._page)

        logger.info(f"Headless {BROWSER_TYPE} started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        # Forget the tabs as well. is_ready() still reported True after stop(),
        # so server.py went on dispatching commands onto a closed browser and
        # the caller got an error from deep inside Playwright instead of being
        # told the browser is gone.
        self._page = None
        self._tabs = {}
        self._active_tab_id = None
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("Headless browser stopped")

    def is_ready(self) -> bool:
        """True once start() has finished and a page is available for commands."""
        return self._page is not None

    def _register_tab(self, page) -> int:
        """Track a page under a stable tab id; untrack it when it closes."""
        tab_id = self._next_tab_id
        self._next_tab_id += 1
        self._tabs[tab_id] = page
        self._active_tab_id = tab_id
        page.on('close', lambda: self._forget_tab(tab_id))
        self._wire_logging(page)
        return tab_id

    @staticmethod
    def _wire_logging(page):
        """Mirror a page's console, errors and traffic to stderr.

        Wired per tab, next to the tab bookkeeping: start() used to attach
        these to the first page only, so a tab opened with createTab produced
        no diagnostics at all.
        """
        page.on('console', lambda m: logger.debug(f"[browser:console:{m.type}] {m.text}"))
        page.on('pageerror', lambda e: logger.warning(f"[browser:pageerror] {e}"))
        page.on('request', lambda r: logger.debug(f"[browser:request] {r.method} {r.url}"))
        page.on('response', lambda r: logger.debug(f"[browser:response] {r.status} {r.url}"))

    def _forget_tab(self, tab_id: int):
        self._tabs.pop(tab_id, None)
        if self._active_tab_id == tab_id:
            self._active_tab_id = next(iter(self._tabs), None)
            self._page = self._tabs.get(self._active_tab_id)

    async def _get_page(self, tab_id: Optional[int] = None):
        """Return the page for tab_id, or the active page when not specified."""
        if tab_id is not None:
            page = self._tabs.get(tab_id)
            if page is None:
                raise RuntimeError(f"No headless tab with id {tab_id}")
            return page
        if self._page is None:
            raise RuntimeError("Headless browser not started")
        return self._page

    # ONE definition, mirroring CREDENTIAL_AUTOCOMPLETE_TOKENS in
    # extension/content.js. There were previously three divergent copies in
    # this file - the selector guard, the focused-element guard and the read
    # mask - with different token lists, so whether a field counted as a
    # credential depended on which code path reached it: cc-exp-month and
    # cc-exp-year were missing everywhere, and cc-exp was missing from the
    # focused path, meaning `type` with a selector refused a card-expiry field
    # while `type` with no selector typed into the same focused field.
    # tests/test_headless_backend.py asserts this list matches the extension's.
    CREDENTIAL_AUTOCOMPLETE_TOKENS = (
        'current-password', 'new-password', 'one-time-code',
        'cc-number', 'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year',
    )

    # Mirrors CREDENTIAL_NAME_RE in extension/content.js, character for
    # character: it is a JavaScript pattern, interpolated into the probes
    # below and compiled by the page. Change one, change the other - the
    # tests lift the extension's copy and compare the two strings, the same
    # way background.js's SECRET_KEY_RE is pinned to it.
    #
    # On real pages the name/id is the signal that matters most, and this
    # file had none of it: it looked at type and autocomplete only, inside a
    # tagName === 'INPUT' test. So <input type="text" name="passwd"> was not
    # a credential here - headless read its value straight back to the agent
    # and typed into it - while attended mode refused both. Eight shapes
    # disagreed, every one of them in the leaking direction.
    CREDENTIAL_NAME_RE = (
        '(pass(?:word|wd|phrase|code|key)|userpass'
        '|(?:^|[^a-z])pass(?:[^a-z]|$)|pwd|secret|token|credential'
        '|one[-_]?time[-_]?code|[th]?otp(?:[^a-z]|$)|oauth|authorization'
        '|authenticat|auth(?:z|n)(?:[^a-z]|$)'
        '|auth[-_]?(?:token|key|code|header|secret|data)|auth(?:[^a-z]|$)'
        '|api[-_]?key|private[-_]?key'
        '|session[-_]?(?:id|token|key|secret|value)|sess[-_]?id'
        '|session(?:[^a-z]|$)|sessid|cvv|cvc|card[-_]?number|jwt|bearer'
        '|signature|ssn|(?:^|[^a-z])pin(?:[^a-z]|$))'
    )

    # How many nested credential fields a single getText will mask. See
    # _get_text_js for why the cap is on how many are masked, not on how many
    # are examined.
    MAX_SCRUBBED_FIELDS = 50

    @classmethod
    def _credential_defs_js(cls) -> str:
        """The extension's credential predicates, as JS to run in the page.

        A port of attributeOf / looksLikeCredentialName / holdsEnteredValue /
        isPasswordField / isConcealedValueField in extension/content.js, kept
        function for function so the two can be read side by side. The tests
        run this and the extension's own copy over one fixture table and fail
        if they disagree about any shape.

        Not restricted to <input>: Shoelace, Ionic and Vaadin wrap a real
        input in a shadow root, so <sl-input type="password"> is the only
        element an agent can target, and a contenteditable <div id="otp-code">
        is a credential with no type at all.
        """
        tokens = ', '.join(f"'{t}'" for t in cls.CREDENTIAL_AUTOCOMPLETE_TOKENS)
        return (
            f"const CREDENTIAL_AUTOCOMPLETE_TOKENS = [{tokens}];\n"
            f"const CREDENTIAL_NAME_RE = /{cls.CREDENTIAL_NAME_RE}/i;\n"
            "const attributeOf = (element, name) =>\n"
            "  (element && typeof element.getAttribute === 'function')\n"
            "    ? element.getAttribute(name) : null;\n"
            # camelCase is normalised first because the anchors in the
            # pattern only see a non-letter as a boundary: otpCode, apiKey
            # and privateKey do not match without it.
            "const looksLikeCredentialName = (name) => CREDENTIAL_NAME_RE.test(\n"
            "  String(name == null ? '' : name)"
            ".replace(/([a-z0-9])([A-Z])/g, '$1_$2'));\n"
            # The name/id rule is only for elements that hold a value
            # somebody entered. Applied to everything it would mask the text
            # of any <div id="user-session-banner"> on the page.
            "const holdsEnteredValue = (element) => {\n"
            "  const tag = element.tagName || '';\n"
            "  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT')"
            " return true;\n"
            "  if (element.isContentEditable === true) return true;\n"
            "  return tag.includes('-');\n"
            "};\n"
            "const isPasswordField = (element) => {\n"
            "  if (!element) return false;\n"
            "  if (element.type === 'password') return true;\n"
            "  if ((attributeOf(element, 'type') || '').toLowerCase()"
            " === 'password') return true;\n"
            "  const autocomplete = attributeOf(element, 'autocomplete');\n"
            "  if (autocomplete && autocomplete.toLowerCase().split(/\\s+/)\n"
            "        .some(t => CREDENTIAL_AUTOCOMPLETE_TOKENS.includes(t)))"
            " return true;\n"
            "  if (!holdsEnteredValue(element)) return false;\n"
            # name can be a form path like user[password], which still names
            # a credential, so this is a substring match, not an equality
            # test.
            "  const name = element.name || attributeOf(element, 'name') || '';\n"
            "  const id = element.id || attributeOf(element, 'id') || '';\n"
            "  return looksLikeCredentialName(name)"
            " || looksLikeCredentialName(id);\n"
            "};\n"
            # Hidden inputs routinely carry CSRF tokens, session ids and
            # order ids. They are never something the agent needs the value
            # of - but writing one is how a form carries state, so only the
            # read mask includes them.
            "const isConcealedValueField = (element) =>"
            " isPasswordField(element) ||\n"
            "  (!!element && element.tagName === 'INPUT'"
            " && element.type === 'hidden');\n"
        )

    @classmethod
    def _credential_js(cls, include_hidden: bool) -> str:
        """JS predicate for "is this element a credential field".

        include_hidden distinguishes the read mask (hidden inputs carry CSRF
        and session tokens, so their values are masked) from the write guard
        (writing to a plain hidden input is legitimate), matching
        isPasswordField vs isConcealedValueField in the extension.
        """
        name = 'isConcealedValueField' if include_hidden else 'isPasswordField'
        return (f"el => {{\n{cls._credential_defs_js()}"
                f"return {name}(el);\n}}")

    @classmethod
    def _get_text_js(cls, allow_password: bool) -> str:
        """JS for one getText read: mask, scrub, and report what it masked.

        The whole read happens in the page, including the decision, so the
        unscrubbed text never crosses into this process - the same reason the
        read mask asks `!!el.value` instead of pulling the value out. This
        used to be a bare page.inner_text() with no guard of any kind, so
        browser_get_text on a contenteditable credential returned the code in
        clear where attended mode returns '***'.

        The element the caller named is checked directly, but a credential
        can also sit INSIDE it - and 'body' is the default, so a whole-page
        read returned a <div contenteditable> PIN in the middle of the page
        dump. A port of withoutNestedCredentialText in content.js, including
        the two bugs its comment records: filter before capping, or a busy
        page with 50 ordinary editable cells never reaches the credential
        after them; and replace longest first, or masking a short secret that
        prefixes a longer one leaves the longer one's tail behind.

        Stated honestly, as there it is: an <input> contributes nothing to
        innerText whatever its value, so inputs cannot leak this way, and a
        credential that reached the page as ordinary prose is not something
        this can find.
        """
        allowed = 'true' if allow_password else 'false'
        return (
            "root => {\n"
            f"const ALLOW_PASSWORD = {allowed};\n"
            f"{cls._credential_defs_js()}"
            "if (!ALLOW_PASSWORD && isConcealedValueField(root))"
            " return {self: true};\n"
            # innerText is the visible text this tool promises; textContent
            # is the fallback for where innerText does not exist (an SVG
            # element, a detached node), and the result says which was read.
            "const visible = typeof root.innerText === 'string';\n"
            "const raw = (visible ? root.innerText : root.textContent) || '';\n"
            "const source = visible ? 'innerText' : 'textContent';\n"
            "if (ALLOW_PASSWORD || !raw) return {text: raw, masked: 0, source};\n"
            "let candidates = [];\n"
            "try {\n"
            "  candidates = root.querySelectorAll\n"
            "    ? Array.from(root.querySelectorAll('[contenteditable], textarea'))\n"
            "        .filter(el => el !== root && isPasswordField(el))\n"
            f"        .slice(0, {cls.MAX_SCRUBBED_FIELDS})\n"
            "    : [];\n"
            "} catch (e) { candidates = []; }\n"
            "const secrets = [];\n"
            "for (const field of candidates) {\n"
            "  const own = field.tagName === 'TEXTAREA'\n"
            "    ? (field.value || '')\n"
            "    : (typeof field.innerText === 'string'\n"
            "        ? field.innerText : (field.textContent || ''));\n"
            "  const secret = own.trim();\n"
            # A one- or two-character "secret" is not worth masking every
            # occurrence of across a whole page.
            "  if (secret.length >= 3) secrets.push(secret);\n"
            "}\n"
            "secrets.sort((a, b) => b.length - a.length);\n"
            "let out = raw;\n"
            "let masked = 0;\n"
            "for (const secret of secrets) {\n"
            "  if (!out.includes(secret)) continue;\n"
            "  out = out.split(secret).join('***');\n"
            "  masked++;\n"
            "}\n"
            "return {text: out, masked, source};\n"
            "}"
        )

    @classmethod
    def _element_info_js(cls, allow_password: bool) -> str:
        """JS for one getElements entry: tag plus text, masked if credential.

        getElements used to hand back el.inner_text() raw, so a contenteditable
        credential inside the match list came back in clear; attended mode
        routes the same text through safeElementText. textContent decides
        whether a masked field is empty, as it does there, because a field
        hidden from innerText still holds its value.
        """
        allowed = 'true' if allow_password else 'false'
        return (
            "el => {\n"
            f"const ALLOW_PASSWORD = {allowed};\n"
            f"{cls._credential_defs_js()}"
            "const tag = (el.tagName || '').toLowerCase();\n"
            "if (!ALLOW_PASSWORD && isConcealedValueField(el)) {\n"
            "  return {tag, text: el.textContent ? '***' : null, masked: true};\n"
            "}\n"
            "const visible = typeof el.innerText === 'string';\n"
            "const raw = (visible ? el.innerText : el.textContent) || '';\n"
            "return {tag, text: raw.slice(0, 100)};\n"
            "}"
        )

    async def _is_password_field(self, page, selector: str,
                                 include_hidden: bool = False) -> bool:
        """True when the selector resolves to a credential input.

        Raises CredentialProbeFailed when it cannot tell. Returning False on
        any exception made the guard fail OPEN: a probe that raised
        (TimeoutError, "Execution context was destroyed" mid-navigation, a
        detached frame) silently disabled the check while a following
        page.fill went on to succeed.
        """
        try:
            return bool(await page.eval_on_selector(
                selector, self._credential_js(include_hidden)))
        except Exception as e:
            message = str(e)
            # A selector that genuinely does not match is not a probe failure;
            # the caller reports its own "not found" error for that.
            if 'failed to find element' in message.lower() or \
                    'no element' in message.lower():
                return False
            raise CredentialProbeFailed(message) from e

    async def _assert_focused_not_password(self, page, args: Dict[str, Any]):
        """Refuse to type into a focused credential field.

        Uses the same single predicate as the selector path, and fails CLOSED:
        this previously swallowed any probe exception and returned, so a
        destroyed execution context mid-navigation let the credential through.
        """
        if args.get('allow_password') is True:
            return
        try:
            predicate = self._credential_js(include_hidden=False)
            is_password = await page.evaluate(
                "() => { const el = document.activeElement; "
                f"return ({predicate})(el); }}")
        except Exception as e:
            raise RuntimeError(
                'Refused: could not determine whether the focused element is a '
                f'credential field ({e}). Refusing rather than risk typing a '
                'credential into one.') from e
        if is_password:
            raise RuntimeError(
                'Refused: the focused element is a credential field. '
                'Credentials belong in a password manager, not automated '
                'typing. Set "allow_password_typing": true in '
                '~/.claudecodebrowser/safety.json to override.')

    async def _assert_not_password(self, page, selector: str, args: Dict[str, Any]):
        """Refuse to fill credential fields unless the safety config allows it.

        include_hidden=False: writing into a plain hidden input is legitimate
        (it is how a form carries state), while READING one back is masked
        because they carry CSRF and session tokens. Same split as
        isPasswordField vs isConcealedValueField in the extension.
        """
        if args.get('allow_password') is True:
            return
        if await self._is_password_field(page, selector, include_hidden=False):
            raise RuntimeError(
                'Refused: target is a password field. Credentials belong in a '
                'password manager, not automated typing. Set '
                '"allow_password_typing": true in ~/.claudecodebrowser/safety.json '
                'to override.'
            )

    # Keys that cannot put a character into a field, in Playwright's
    # keyboard.press naming: navigation, editing, function and modifier keys.
    #
    # An allowlist, because the predicate it replaces tried to spot the keys
    # that DO type and missed almost all of them: `len(key) == 1 and
    # key.isprintable()` saw 'a', but Playwright also accepts 'KeyA',
    # 'Digit1', 'Space', 'Minus', 'Numpad5' and 'Shift+a', every one of which
    # inserts a character and none of which is one character long. Anything
    # not listed here is assumed to type, so a key name Playwright adds later
    # is guarded by default instead of waved through.
    _KEYS_THAT_CANNOT_TYPE = frozenset((
        'arrowleft', 'arrowright', 'arrowup', 'arrowdown',
        'home', 'end', 'pageup', 'pagedown',
        'backspace', 'delete', 'escape', 'esc', 'tab', 'enter', 'insert',
        # The numpad's own Enter, which submits and moves like Enter does.
        # Listing Enter and not this one refused a key that cannot put a
        # character anywhere, which made the tool inconsistent for no gain.
        'numpadenter',
        'shift', 'control', 'alt', 'meta', 'controlormeta',
        'shiftleft', 'shiftright', 'controlleft', 'controlright',
        'altleft', 'altright', 'metaleft', 'metaright', 'altgraph',
        'capslock', 'numlock', 'scrolllock', 'pause', 'printscreen',
        'contextmenu', 'help', 'clear',
    )) | frozenset(f'f{n}' for n in range(1, 25))

    @classmethod
    def _key_can_type(cls, key: str) -> bool:
        """True when pressing this key could enter a character."""
        # 'Shift+a' and 'Control+KeyO' name modifiers before the key itself,
        # and only the last part can type. 'Control++' ends in a literal '+'.
        # A modifier in front does not make the key safe - Control+v pastes -
        # so the modifiers are dropped rather than trusted.
        actual = key.split('+')[-1].strip() or '+'
        return actual.lower() not in cls._KEYS_THAT_CANNOT_TYPE

    # The directions browser_scroll's schema allows.
    _RELATIVE_DIRECTIONS = {'up': (0, -1), 'down': (0, 1),
                            'left': (-1, 0), 'right': (1, 0)}
    _EDGE_DIRECTIONS = ('top', 'bottom')

    async def _scroll(self, page, args: Dict[str, Any]) -> Dict[str, Any]:
        """Scroll the page or one element, as browser_scroll documents it.

        This handler used to read x/y/deltaX/deltaY - none of which the schema
        defines - and ignore direction, amount, selector and to_element. So
        scroll(direction='up', amount=1000) wheeled 300px DOWN and returned
        success: true, telling the caller something happened that did not. The
        x/y and delta names still work for any caller that learned them here.

        A selector that matches nothing raises, which execute() reports:
        scrolling the window instead of the element the caller named and
        calling it success is worse than failing (the same rule the
        extension's performScroll follows).
        """
        selector = args.get('selector') or None
        to_element = args.get('to_element') or args.get('toElement') or None
        direction = str(args.get('direction') or '').strip().lower()

        if to_element:
            await page.eval_on_selector(
                to_element, "el => el.scrollIntoView({block: 'center'})")
            return {'success': True, 'scrolledTo': to_element}

        if direction in self._EDGE_DIRECTIONS:
            edge = '0' if direction == 'top' else 'target.scrollHeight'
            if selector:
                await page.eval_on_selector(
                    selector, f"target => target.scrollTo({{top: {edge}}})")
            else:
                await page.evaluate(
                    'window.scrollTo({top: 0})' if direction == 'top'
                    else 'window.scrollTo({top: document.body.scrollHeight})')
            return {'success': True, 'direction': direction}

        if direction and direction not in self._RELATIVE_DIRECTIONS:
            allowed = sorted(set(self._RELATIVE_DIRECTIONS) |
                             set(self._EDGE_DIRECTIONS))
            return {'success': False,
                    'error': f'Unknown scroll direction {direction!r}. '
                             f'Expected one of: {", ".join(allowed)}'}

        if direction:
            amount = parse_number(args.get('amount'), 300.0)
            step_x, step_y = self._RELATIVE_DIRECTIONS[direction]
            delta_x, delta_y = step_x * amount, step_y * amount
        elif args.get('deltaX') is not None or args.get('deltaY') is not None:
            delta_x = parse_number(args.get('deltaX'), 0.0)
            delta_y = parse_number(args.get('deltaY'), 0.0)
        elif args.get('x') is not None or args.get('y') is not None:
            # x/y are an absolute position, as the extension treats them.
            position = {'left': parse_number(args.get('x'), 0.0),
                        'top': parse_number(args.get('y'), 0.0)}
            if selector:
                await page.eval_on_selector(
                    selector, f"target => target.scrollTo({json.dumps(position)})")
            else:
                await page.evaluate(f"window.scrollTo({json.dumps(position)})")
            return {'success': True, 'position': position}
        else:
            # No arguments at all: the historical default, one wheel notch down.
            delta_x, delta_y = 0.0, 300.0

        if selector:
            offset = {'left': delta_x, 'top': delta_y}
            await page.eval_on_selector(
                selector, f"target => target.scrollBy({json.dumps(offset)})")
            return {'success': True, 'selector': selector, 'scrolledBy': offset}

        await page.mouse.wheel(float(delta_x), float(delta_y))
        return {'success': True, 'scrolledBy': {'x': delta_x, 'y': delta_y}}

    async def execute(self, action: str, tab_id: Optional[int], arguments: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            try:
                return await self._dispatch(action, tab_id, arguments)
            except Exception as e:
                logger.error(f"Headless {action} failed: {e}")
                return {'success': False, 'error': str(e)}

    async def _dispatch(self, action: str, tab_id, args: Dict[str, Any]) -> Dict[str, Any]:
        page = await self._get_page(tab_id)

        if action == 'navigate':
            url = args.get('url', '')
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            return {'success': True, 'url': page.url, 'title': await page.title()}

        elif action == 'screenshot':
            from datetime import datetime
            generated = f'screenshot_{datetime.now().strftime("%Y%m%d_%H%M%S")}.png'
            # Strips directories and forces the .png the retention sweep
            # looks for; see screenshot_filename for what each rule is for.
            filename = screenshot_filename(args.get('filename'), generated)
            # Captured into memory, not written by Playwright. Handing
            # Playwright a path= made it write the file with a plain
            # open(path, 'wb'): umask permissions (0644) and symlinks
            # followed, so with a shared CLAUDE_BROWSER_SCREENSHOTS_DIR
            # someone could pre-create a predictable name as a symlink and
            # have another of this user's files truncated. The attended path
            # (_save_screenshot in server.py) and the native host were both
            # hardened to O_NOFOLLOW at 0600; this branch was missed, and
            # server.py returns the headless result without passing it
            # through _save_screenshot, so nothing downstream covered for it.
            image = await page.screenshot(
                full_page=parse_flag(args.get('full_page'), False))

            # save_to_file is in browser_screenshot's schema and the attended
            # path honours it; this branch did not mention it at all, so a
            # caller that explicitly declined a disk copy got one anyway -
            # and no image either, because the bytes were never returned.
            # parse_flag for the same reason _save_screenshot uses it:
            # bool("false") is True.
            if not parse_flag(args.get('save_to_file'), True):
                return {
                    'success': True,
                    'saved': False,
                    'filename': filename,
                    'size': len(image),
                    'data': 'data:image/png;base64,'
                            + base64.b64encode(image).decode('ascii'),
                    'message': 'Screenshot not written to disk: save_to_file '
                               'was false. The image is in this response only.'
                }

            filepath = SCREENSHOTS_DIR / filename
            flags = (os.O_CREAT | os.O_WRONLY | os.O_TRUNC |
                     getattr(os, 'O_NOFOLLOW', 0))
            fd = os.open(str(filepath), flags, 0o600)
            with os.fdopen(fd, 'wb') as fh:
                fh.write(image)
            # The headless path does its own write rather than going through
            # server.py's _save_screenshot, so without this the retention
            # policy simply did not exist in headless mode.
            prune_screenshots(SCREENSHOTS_DIR)
            return {
                'success': True,
                'saved': True,
                'filepath': str(filepath),
                'filename': filename,
                'size': len(image),
                'message': f'Screenshot saved to {filepath}'
            }

        elif action == 'click':
            selector = args.get('selector')
            x, y = args.get('x'), args.get('y')
            if selector:
                await page.click(selector, timeout=10000)
            elif x is not None and y is not None:
                await page.mouse.click(float(x), float(y))
            else:
                return {'success': False, 'error': 'click requires selector or x+y coordinates'}
            return {'success': True}

        elif action == 'type':
            selector = args.get('selector')
            text = args.get('text', '')
            if selector:
                await self._assert_not_password(page, selector, args)
                await page.fill(selector, text)
            else:
                # Typing into the focused element bypassed the guard entirely:
                # focus a password field with any other call, then type with
                # no selector. Attended mode resolves document.activeElement
                # and still checks it, so this was a headless-only hole that
                # contradicted the documented "refused in both modes".
                await self._assert_focused_not_password(page, args)
                await page.keyboard.type(text)
            return {'success': True}

        elif action == 'scroll':
            return await self._scroll(page, args)

        elif action == 'getPageInfo':
            return {
                'success': True,
                'url': page.url,
                'title': await page.title(),
            }

        elif action == 'getElements':
            selector = args.get('selector', 'a, button, input, select, textarea')
            limit = parse_int(args.get('limit'), 50)
            if limit <= 0:
                limit = 50
            elements = await page.query_selector_all(selector)
            # One page call per element reads the tag and the text together,
            # so a credential field cannot be classified and then read in two
            # steps with a navigation in between.
            reader = self._element_info_js(args.get('allow_password') is True)
            results = []
            skipped = 0
            for el in elements[:limit]:
                try:
                    info = await el.evaluate(reader)
                    box = await el.bounding_box()
                    entry = {'tag': info['tag'], 'text': info['text'],
                             'box': box}
                    if info.get('masked'):
                        entry['masked'] = True
                    results.append(entry)
                except Exception:
                    # An element that detached between the query and the read
                    # is dropped - but it is counted. A caller reasoning about
                    # "all the buttons" was handed a list cut at 50, with any
                    # unreadable element silently missing, and nothing in the
                    # result said so.
                    skipped += 1
            return {
                'success': True,
                'elements': results,
                'totalMatched': len(elements),
                'returned': len(results),
                'truncated': len(elements) > limit,
                'skipped': skipped,
            }

        elif action == 'executeScript':
            script = args.get('script', '')
            result = await page.evaluate(script)
            return {'success': True, 'result': result}

        elif action == 'waitForElement':
            selector = args.get('selector', '')
            timeout = parse_number(args.get('timeout'), 10000)
            await page.wait_for_selector(selector, timeout=timeout)
            return {'success': True}

        elif action == 'waitForNetworkIdle':
            timeout = parse_number(args.get('timeout'), 10000)
            await page.wait_for_load_state('networkidle', timeout=timeout)
            return {'success': True}

        elif action == 'getTabs':
            tabs = []
            for tid, p in list(self._tabs.items()):
                try:
                    tabs.append({
                        'id': tid,
                        'url': p.url,
                        'title': await p.title(),
                        'active': tid == self._active_tab_id
                    })
                except Exception:
                    pass
            return {'success': True, 'tabs': tabs, 'totalTabs': len(tabs)}

        elif action == 'createTab':
            url = args.get('url', 'about:blank')
            new_page = await self._context.new_page()
            if url != 'about:blank':
                await new_page.goto(url)
            self._page = new_page
            new_id = self._register_tab(new_page)
            return {'success': True, 'tabId': new_id, 'url': new_page.url}

        elif action == 'closeTab':
            if tab_id is None or tab_id not in self._tabs:
                return {'success': False, 'error': f'No headless tab with id {tab_id}'}
            await self._tabs[tab_id].close()
            return {'success': True, 'closedTabId': tab_id}

        elif action == 'focusTab':
            if tab_id is None or tab_id not in self._tabs:
                return {'success': False, 'error': f'No headless tab with id {tab_id}'}
            self._active_tab_id = tab_id
            self._page = self._tabs[tab_id]
            await self._page.bring_to_front()
            return {'success': True, 'tabId': tab_id, 'url': self._page.url}

        elif action == 'getValue':
            selector = args.get('selector', '')
            # Reading a password field hands the credential to the AI just as
            # typing one would, so the same guard applies. Masked, not refused,
            # so the caller can still tell whether the field is filled.
            if args.get('allow_password') is not True and \
                    await self._is_password_field(page, selector,
                                                  include_hidden=True):
                # Masked rather than refused so the caller can still tell
                # whether the field is filled - which only works if an EMPTY
                # credential field reads back as null instead of '***'. The
                # extension's safeElementValue makes the same distinction. The
                # emptiness test runs in the page, so the credential itself
                # never crosses into this process.
                filled = bool(await page.eval_on_selector(
                    selector, 'el => !!el.value'))
                return {
                    'success': True,
                    'value': '***' if filled else None,
                    'masked': True,
                    'note': 'Credential field value withheld. Set '
                            '"allow_password_typing": true in '
                            '~/.claudecodebrowser/safety.json to read '
                            'credentials through the agent.'
                }
            value = await page.eval_on_selector(selector, 'el => el.value')
            return {'success': True, 'value': value}

        elif action == 'setValue':
            selector = args.get('selector', '')
            value = args.get('value', '')
            await self._assert_not_password(page, selector, args)
            await page.fill(selector, value)
            return {'success': True}

        elif action == 'requestApproval':
            return {'success': False, 'approved': False,
                    'error': 'No human is present in headless mode; use the '
                             'confirm_token flow for protected actions instead.'}

        elif action == 'solveCaptcha':
            # Detect what's there, but a captcha is by design a human check —
            # headless mode has no human and this project does not auto-solve.
            detection = await page.evaluate(_DETECT_CAPTCHA_JS)
            if parse_flag(args.get('detect_only'), False):
                return {'success': True, **detection}
            widgets = detection.get('widgets', [])

            if not detection.get('present'):
                # A step that found nothing to do is not a failed step. This
                # was reported as success: false, so a workflow could not tell
                # "no captcha here" from "a captcha blocked us". The extension
                # returns success here too.
                return {'success': True, 'present': False, 'widgets': widgets,
                        'message': 'No captcha detected on the page.'}

            # EVERY widget must report itself solved. The old test skipped
            # widgets whose state is unknowable (a generic image captcha,
            # solved: null) instead of counting them against the result, so
            # one solved reCAPTCHA next to an unsolved generic one reported
            # solved: true.
            if widgets and all(w.get('solved') is True for w in widgets):
                return {
                    'success': True, 'present': True, 'solved': True,
                    # The response token is written by the page, so a hostile
                    # page can set it. "Captcha already solved." read as proof
                    # that a human passed the check; it is not, and the
                    # extension says so in the same words.
                    'humanVerified': False,
                    'widgets': widgets,
                    'message': ('Captcha reports itself already solved. The '
                                'response token is page-writable, so this is '
                                'not proof a human solved it.'),
                }

            return {
                'success': False,
                'present': True,
                'widgets': widgets,
                'needs_human': True,
                'error': ('A captcha requires a human to solve and none is present '
                          'in headless mode. Re-run this step in attended mode '
                          '(the Firefox extension) so the person can complete it.'),
            }

        elif action == 'hover':
            selector = args.get('selector', '')
            await page.hover(selector)
            return {'success': True}

        elif action == 'selectOption':
            selector = args.get('selector', '')
            if args.get('value') is not None:
                await page.select_option(selector, value=args['value'])
            elif args.get('text') is not None:
                await page.select_option(selector, label=args['text'])
            elif args.get('index') is not None:
                await page.select_option(selector,
                                         index=parse_int(args['index'], 0))
            else:
                return {'success': False, 'error': 'selectOption requires value, text, or index'}
            return {'success': True}

        elif action == 'goBack':
            await page.go_back(wait_until='domcontentloaded', timeout=15000)
            return {'success': True, 'url': page.url, 'title': await page.title()}

        elif action == 'goForward':
            await page.go_forward(wait_until='domcontentloaded', timeout=15000)
            return {'success': True, 'url': page.url, 'title': await page.title()}

        elif action == 'pressKey':
            selector = args.get('selector')
            key = args.get('key', '')
            if not key:
                # Without this, an omitted key reached keyboard.press(''),
                # which Playwright rejects with an opaque parse error.
                return {'success': False, 'error': 'pressKey requires "key"'}

            # The extension's pressKey dispatches synthetic KeyboardEvents,
            # which are untrusted and have no default action, so it cannot
            # enter text. Playwright's keyboard.press REALLY types - so this
            # path could enter a credential one character at a time, which is
            # the historical bypass in a different costume. A key that could
            # type is refused on a credential field; navigation and editing
            # keys stay allowed. See _KEYS_THAT_CANNOT_TYPE for why this is
            # an allowlist.
            if self._key_can_type(key) and \
                    args.get('allow_password') is not True:
                if selector:
                    if await self._is_password_field(page, selector,
                                                     include_hidden=False):
                        raise RuntimeError(
                            'Refused: pressing a printable key into a '
                            'credential field would enter the credential one '
                            'character at a time. Set "allow_password_typing": '
                            'true in ~/.claudecodebrowser/safety.json to '
                            'override.')
                else:
                    await self._assert_focused_not_password(page, args)

            if selector:
                await page.focus(selector)
            modifiers = [m for m, on in (
                ('Control', parse_flag(args.get('ctrl'), False)),
                ('Shift', parse_flag(args.get('shift'), False)),
                ('Alt', parse_flag(args.get('alt'), False)),
                ('Meta', parse_flag(args.get('meta'), False))) if on]
            combination = '+'.join(modifiers + [key]) if modifiers else key
            await page.keyboard.press(combination)
            return {'success': True, 'key': combination}

        elif action == 'getText':
            selector = args.get('selector') or 'body'
            max_length = parse_int(args.get('max_length'), 20000)
            if max_length <= 0:
                max_length = 20000
            # state='attached', not the default 'visible': inner_text, which
            # this replaces, read a display:none element without complaint,
            # and waiting for visibility would turn that into a timeout.
            await page.wait_for_selector(selector, state='attached',
                                         timeout=10000)
            read = await page.eval_on_selector(
                selector,
                self._get_text_js(args.get('allow_password') is True))
            if read.get('self'):
                # Asking for the text of a credential field is asking for its
                # value, so this is the same refusal getValue gives.
                return {
                    'success': True,
                    'text': '***',
                    'masked': True,
                    'note': 'Credential field text withheld. Set '
                            '"allow_password_typing": true in '
                            '~/.claudecodebrowser/safety.json to read '
                            'credentials through the agent.',
                    'url': page.url
                }
            text = read.get('text') or ''
            masked_fields = read.get('masked') or 0
            result = {
                'success': True,
                'text': text[:max_length],
                'truncated': len(text) > max_length,
                'total_length': len(text),
                'source': read.get('source'),
                'url': page.url
            }
            if masked_fields:
                # Named as the extension names it, since a caller reads the
                # same key in both modes.
                result['maskedFields'] = masked_fields
                result['note'] = (
                    f'{masked_fields} credential field(s) inside this element '
                    'had their text replaced with ***.')
            return result

        elif action == 'refresh':
            await page.reload()
            return {'success': True}

        elif action == 'highlight':
            selector = args.get('selector', '')
            await page.eval_on_selector(
                selector,
                "el => { el.style.outline = '3px solid red'; setTimeout(() => el.style.outline = '', 2000); }"
            )
            return {'success': True}

        elif action == 'evalChain':
            steps = args.get('steps', [])
            results = []
            prev = None
            for i, step in enumerate(steps):
                script = step.get('script', '')
                label = step.get('label', f'step_{i}')
                capture = parse_flag(step.get('capture_console'), True)
                stop_on_error = parse_flag(step.get('stop_on_error'), True)

                console_msgs = []
                listener = None
                if capture:
                    # Hold the listener so it can actually be removed: the old
                    # code removed a fresh lambda, so every step leaked one and
                    # messages accumulated into earlier steps' arrays.
                    def listener(m, sink=console_msgs):
                        sink.append({'type': m.type, 'text': m.text})
                    page.on('console', listener)

                try:
                    # Inject $prev into execution context
                    wrapped = f"(function($prev) {{ return ({script}); }})({json.dumps(prev)})"
                    result = await page.evaluate(wrapped)
                    prev = result
                    results.append({'label': label, 'result': result, 'console': console_msgs, 'error': None})
                except Exception as e:
                    results.append({'label': label, 'result': None, 'console': console_msgs, 'error': str(e)})
                    # A failed step has no result, so the next step's $prev is
                    # null. It used to keep the value from the step before,
                    # handing the next script data from two steps back while
                    # the tool documents $prev as "the prior result".
                    prev = None
                    if stop_on_error:
                        break
                finally:
                    if listener is not None:
                        try:
                            page.remove_listener('console', listener)
                        except Exception:
                            pass

            # A chain whose first step threw used to report success: true with
            # the error buried in steps[0], so a caller checking the top-level
            # flag carried on from a false premise.
            return {
                'success': all(step.get('error') is None for step in results),
                'steps': results,
                'final': prev,
            }

        elif action == 'waitAndAct':
            condition = args.get('condition', 'true')
            action_script = args.get('action_script', '')
            # A non-positive poll interval never advanced `elapsed` (and a
            # negative one walked it backwards), so the loop never ended: the
            # call held the single headless event loop, and every other
            # browser tool with it, until the server was killed. The floor
            # also keeps a tiny interval from becoming a busy spin.
            poll_ms = max(parse_int(args.get('poll_interval_ms'), 200),
                          MIN_POLL_INTERVAL_MS)
            requested_ms = parse_int(args.get('timeout_ms'), 15000)
            timeout_ms = min(requested_ms, MAX_WAIT_AND_ACT_TIMEOUT_MS)
            # Reported back, because a call that waited 30s when it asked for
            # 31 years otherwise looks like the page never settled. Only when
            # the cap actually bit, so an honoured timeout stays quiet.
            capped = ({'timeout_ms': timeout_ms, 'timeout_capped': True}
                      if timeout_ms != requested_ms else {})
            elapsed = 0
            # Tested after the body, not before it: `while elapsed < timeout_ms`
            # skipped the body entirely on timeout_ms=0, so an already-true
            # condition was reported as "not met" without ever being evaluated.
            while True:
                try:
                    ready = await page.evaluate(condition)
                except Exception as e:
                    # Evaluating the condition failed (page navigating, syntax
                    # error). Report it rather than polling until timeout and
                    # blaming the condition.
                    return {'success': False,
                            'error': f'Condition evaluation failed: {e}',
                            'elapsed_ms': elapsed, **capped}
                if ready:
                    # Outside the try, and returned either way: the action is
                    # allowed to run exactly once. Previously a throwing action
                    # was swallowed and re-fired on every poll - up to 75 times
                    # at the default timeout, so a Submit click could repeat.
                    try:
                        result = await page.evaluate(action_script)
                    except Exception as e:
                        return {'success': False,
                                'error': f'Action failed: {e}',
                                'elapsed_ms': elapsed, **capped}
                    return {'success': True, 'result': result,
                            'elapsed_ms': elapsed, **capped}
                if elapsed + poll_ms >= timeout_ms:
                    break
                await asyncio.sleep(poll_ms / 1000)
                elapsed += poll_ms
            return {'success': False,
                    'error': f'Condition not met within {timeout_ms}ms',
                    'elapsed_ms': elapsed, **capped}

        elif action == 'injectObserver':
            selector = args.get('selector', 'body')
            observe_attrs = parse_flag(args.get('observe_attributes'), True)
            observe_children = parse_flag(args.get('observe_child_list'), True)
            observe_subtree = parse_flag(args.get('observe_subtree'), True)
            script = f"""
                (function() {{
                    if (window.__ccb_observer) window.__ccb_observer.disconnect();
                    window.__ccb_mutations = window.__ccb_mutations || [];
                    const target = document.querySelector({json.dumps(selector)}) || document.body;
                    window.__ccb_observer = new MutationObserver(mutations => {{
                        mutations.forEach(m => window.__ccb_mutations.push({{
                            type: m.type,
                            target: m.target.tagName + (m.target.id ? '#' + m.target.id : ''),
                            addedNodes: m.addedNodes.length,
                            removedNodes: m.removedNodes.length,
                            attributeName: m.attributeName,
                            ts: Date.now()
                        }}));
                    }});
                    window.__ccb_observer.observe(target, {{
                        attributes: {'true' if observe_attrs else 'false'},
                        childList: {'true' if observe_children else 'false'},
                        subtree: {'true' if observe_subtree else 'false'}
                    }});
                    return 'observer installed on ' + target.tagName;
                }})()
            """
            result = await page.evaluate(script)
            return {'success': True, 'message': result}

        else:
            return {'success': False, 'error': f'Unsupported headless action: {action}'}


# Captcha detection, mirroring the extension's content-script heuristics.
_DETECT_CAPTCHA_JS = r"""
(function() {
  const widgets = [];
  const tokened = (name) => {
    const el = document.querySelector('textarea[name="'+name+'"], input[name="'+name+'"]');
    return !!(el && el.value && el.value.length > 0);
  };
  if (document.querySelector('.g-recaptcha, iframe[src*="recaptcha"], #g-recaptcha-response'))
    widgets.push({type:'recaptcha', solved: tokened('g-recaptcha-response') || !!document.querySelector('.recaptcha-checkbox-checked')});
  if (document.querySelector('.h-captcha, iframe[src*="hcaptcha"], textarea[name="h-captcha-response"]'))
    widgets.push({type:'hcaptcha', solved: tokened('h-captcha-response')});
  if (document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]'))
    widgets.push({type:'turnstile', solved: tokened('cf-turnstile-response')});
  if (widgets.length === 0 &&
      document.querySelector('img[src*="captcha" i], input[name*="captcha" i], [id*="captcha" i], [class*="captcha" i]'))
    widgets.push({type:'generic', solved: null});
  return {present: widgets.length > 0, widgets: widgets};
})()
"""


# Module-level singleton
_headless_browser: Optional[HeadlessBrowser] = None


def get_headless_browser() -> Optional[HeadlessBrowser]:
    return _headless_browser


async def init_headless_browser() -> HeadlessBrowser:
    global _headless_browser
    _headless_browser = HeadlessBrowser()
    await _headless_browser.start()
    return _headless_browser

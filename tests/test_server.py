#!/usr/bin/env python3
"""
MCP server configuration and credential-guard plumbing tests.

Run: python3 -m unittest discover -s tests -v
"""

import asyncio
import base64
import inspect
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

# Point HOME at a throwaway directory before importing the server: it creates
# its API token, config and screenshot directories under the home directory at
# import time, and a test run must not touch the real installation.

from tests import REPO_ROOT, TEST_HOME  # noqa: F401  (redirects HOME on import)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'mcp-server'))

import safety  # noqa: E402
import server  # noqa: E402


class ScreenshotLocationTests(unittest.TestCase):
    """Screenshots hold whatever is on screen, so they belong in the user's
    own directory, not a world-readable shared /tmp."""

    def test_default_directory_is_under_the_home_directory(self):
        self.assertEqual(
            server.SCREENSHOTS_DIR,
            Path(TEST_HOME) / '.claudecodebrowser' / 'screenshots')

    def test_default_directory_is_not_in_shared_tmp(self):
        """The old default was a fixed directory in the shared /tmp. This
        used to assert the path did not start with /tmp/, which fails on
        Linux for the wrong reason: the suite's throwaway home lives there."""
        self.assertNotEqual(server.SCREENSHOTS_DIR,
                            Path('/tmp/claudecodebrowser/screenshots'))
        self.assertTrue(server.SCREENSHOTS_DIR.is_relative_to(Path.home()))

    def test_directory_is_not_world_readable(self):
        self.assertTrue(server.SCREENSHOTS_DIR.is_dir())
        mode = server.SCREENSHOTS_DIR.stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0,
                         f'screenshots directory is group/other accessible: {oct(mode)}')

    def test_headless_backend_agrees_with_the_server(self):
        import headless_backend
        self.assertEqual(headless_backend.SCREENSHOTS_DIR, server.SCREENSHOTS_DIR)


class CredentialGuardPlumbingTests(unittest.TestCase):
    """The password guard must cover reads as well as writes. Enforcement
    happens browser-side where the element type is visible, so the server's
    job is to send the flag with every tool that can surface a field value."""

    PASSWORD_AWARE_TOOLS = (
        'browser_type',
        'browser_set_value',
        'browser_get_value',
        'browser_get_elements',
        'browser_get_page_info',
    )

    # browser_get_attribute is deliberately absent: it is not in
    # tool_action_map, so it is not an exposed MCP tool and the agent cannot
    # call it. content.js still guards the handler as defence in depth for the
    # extension-internal message path.

    def _prepared_arguments(self, tool_name):
        """Run a tool through the server's argument preparation and return the
        arguments it would hand to the browser."""
        captured = {}

        def fake_dispatch(action, tab_id, arguments):
            captured['action'] = action
            captured['arguments'] = arguments
            return {'success': True}

        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._dispatch_action = fake_dispatch
        handler.execute_tool(tool_name, {'selector': '#pw'})
        return captured.get('arguments', {})

    def test_the_flag_survives_the_trip_to_the_extension(self):
        """The extension reads camelCase. A flag lost on the way fails
        closed, but silently: allow_password_typing: true just stops
        working, with nothing to say why."""
        sent = []

        async def fake_send(command, timeout=30.0):
            sent.append(command)
            return {'success': True}

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)
        guard = server.get_safety_guard()
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        with unittest.mock.patch.dict(guard.config,
                                      {'allow_password_typing': True}), \
                unittest.mock.patch.object(server, 'HEADLESS_MODE', False), \
                unittest.mock.patch.object(server, 'MAIN_EVENT_LOOP', loop), \
                unittest.mock.patch.object(
                    server.connection_manager, 'get_active_browser',
                    lambda: object()), \
                unittest.mock.patch.object(
                    server.connection_manager, 'send_command', fake_send):
            handler.execute_tool('browser_type',
                                 {'selector': '#pw', 'text': 'x'})

        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0].data.get('allowPassword'), True, sent[0].data)

    def test_the_agent_cannot_supply_the_password_flag_itself(self):
        """Only the safety config may open the guard. getText reads the flag
        in both modes but is not in the list the server overwrites, so an
        agent sending allow_password: true got credential text unmasked.
        content.js also accepts allowPassword, which camelize_args passes
        through untouched."""
        tools = self.PASSWORD_AWARE_TOOLS + (
            'browser_get_text', 'browser_press_key', 'browser_click',
            'browser_execute_script')
        for tool in tools:
            for spelling in ('allow_password', 'allowPassword', 'ALLOW_PASSWORD'):
                with self.subTest(tool=tool, key=spelling):
                    captured = {}

                    def fake_dispatch(action, tab_id, arguments):
                        captured.update(server.camelize_args(arguments))
                        captured.update(arguments)
                        return {'success': True}

                    handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
                    handler._dispatch_action = fake_dispatch
                    handler.execute_tool(tool, {'selector': '#pw', 'text': 'x',
                                                'key': 'a', 'script': '1',
                                                spelling: True})
                    opened = {k: v for k, v in captured.items()
                              if k.replace('_', '').lower() == 'allowpassword'
                              and v is not False}
                    self.assertEqual(opened, {},
                                     'the agent opened the credential guard')

    def test_read_and_write_tools_all_carry_the_password_flag(self):
        for tool in self.PASSWORD_AWARE_TOOLS:
            with self.subTest(tool=tool):
                args = self._prepared_arguments(tool)
                self.assertIn('allow_password', args,
                              f'{tool} must tell the browser whether credentials are allowed')
                self.assertFalse(args['allow_password'],
                                 'the default must be to refuse credentials')

    def test_flag_follows_the_safety_config(self):
        guard = safety.get_safety_guard()
        original = guard.config.get('allow_password_typing', False)
        guard.config['allow_password_typing'] = True
        try:
            for tool in self.PASSWORD_AWARE_TOOLS:
                with self.subTest(tool=tool):
                    self.assertTrue(self._prepared_arguments(tool)['allow_password'])
        finally:
            guard.config['allow_password_typing'] = original

    def test_every_value_returning_path_uses_the_shared_guard(self):
        """A grep for one branch's masking string is what let the
        getPageInfo leak through: forms[] masked, interactiveElements[] did
        not, and the test passed. Assert instead that no reader builds its own
        value expression -- behaviour is covered by the content-script suite,
        which drives the real code."""
        content_js = (ROOT / 'extension' / 'content.js').read_text()

        self.assertIn('function safeElementValue(', content_js,
                      'there should be one place that decides what a value looks like')

        # No path may slice a raw .value into a result any more.
        offenders = [line.strip() for line in content_js.splitlines()
                     if 'value:' in line and '.value?.substring' in line]
        self.assertEqual(offenders, [],
                         f'these read paths bypass safeElementValue: {offenders}')


class CommandQueueTests(unittest.TestCase):
    """A queued command runs whenever the browser next polls. If the caller
    has already timed out, running it is an action nobody asked for -
    observed live with a disconnected extension."""

    def _handler(self, pending):
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler.server = type('S', (), {'_pending_commands': pending})()
        handler.path = '/browser/poll'
        handler.headers = {'X-API-Key': server.API_TOKEN}
        sent = {}
        handler.send_json_response = lambda data, status=200: sent.update(data)
        handler._check_auth = lambda: True
        handler.do_GET()
        return sent

    def test_a_fresh_command_is_delivered(self):
        import time
        sent = self._handler([{'action': 'click', 'queuedAt': time.time()}])
        self.assertIsNotNone(sent['command'])
        self.assertEqual(sent['command']['action'], 'click')

    def test_a_stale_command_is_dropped_not_delivered(self):
        import time
        stale = time.time() - (server.COMMAND_QUEUE_TTL + 60)
        sent = self._handler([{'action': 'click', 'queuedAt': stale}])
        self.assertIsNone(sent['command'],
                          'a command the caller gave up on must not run later')

    def test_a_fresh_command_behind_a_stale_one_still_runs(self):
        import time
        now = time.time()
        pending = [
            {'action': 'stale', 'queuedAt': now - (server.COMMAND_QUEUE_TTL + 60)},
            {'action': 'fresh', 'queuedAt': now},
        ]
        sent = self._handler(pending)
        self.assertEqual(sent['command']['action'], 'fresh')

    def test_the_ttl_outlasts_the_longest_human_wait(self):
        """A captcha prompt blocks for 200s; dropping that command would make
        the guard's own flow unusable."""
        self.assertGreater(server.COMMAND_QUEUE_TTL, 200)


class ScreenshotFilenameSharingTests(unittest.TestCase):
    """Both rules were duplicated and the .png correction was added to the
    attended copy only, so headless kept writing files the retention sweep
    and GET /screenshots both skip."""

    def test_both_paths_use_the_same_helper(self):
        import headless_backend
        self.assertIs(server.screenshot_filename, safety.screenshot_filename)
        self.assertIs(headless_backend.screenshot_filename,
                      safety.screenshot_filename)

    def test_the_attended_path_writes_under_the_shared_name(self):
        """A second spelling is how the two drifted. This used to grep
        _save_screenshot for with_suffix('.png'), which any other spelling of
        the rule passed; check the file that is actually written instead."""
        png = base64.b64encode(b'\x89PNG\r\n\x1a\x0a').decode()
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        stem = f'ccb-share-{os.getpid()}-{time.time_ns()}'
        for requested in (f'{stem}.jpg', f'../{stem}', f'{stem}.PNG'):
            with self.subTest(requested=requested):
                out = handler._save_screenshot(
                    {'success': True, 'data': png, 'tab': {'id': 1}},
                    {'filename': requested})
                written = Path(out['filepath'])
                self.addCleanup(written.unlink, missing_ok=True)
                self.assertEqual(written.parent, server.SCREENSHOTS_DIR)
                self.assertEqual(
                    written.name,
                    safety.screenshot_filename(requested, 'unused.png'))


class HeadlessDeadlineTests(unittest.TestCase):
    """A headless call that outran its deadline was abandoned, not cancelled,
    so the coroutine kept HeadlessBrowser's lock and every later headless tool
    blocked for the life of the process - one slow call wedged the backend."""

    def test_an_overrunning_call_is_cancelled_not_just_abandoned(self):
        """This used to grep the source for future.cancel(), which stayed
        green with the call wrapped in "if False:". Run a real overrunning
        call instead and check the lock comes back."""
        import headless_backend

        class StuckBrowser:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.cancelled = threading.Event()

            def is_ready(self):
                return True

            async def execute(self, action, tab_id, arguments):
                async with self.lock:
                    try:
                        await asyncio.sleep(3600)
                    except asyncio.CancelledError:
                        self.cancelled.set()
                        raise

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)
        stuck = StuckBrowser()
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        with unittest.mock.patch.object(server, 'HEADLESS_MODE', True), \
                unittest.mock.patch.object(server, 'MAIN_EVENT_LOOP', loop), \
                unittest.mock.patch.object(server, 'HEADLESS_CALL_TIMEOUT', 0.2), \
                unittest.mock.patch.object(
                    server.connection_manager, 'get_active_browser',
                    lambda: None), \
                unittest.mock.patch.object(
                    headless_backend, 'get_headless_browser', lambda: stuck):
            result = handler._dispatch_action('click', None, {'selector': '#x'})

        self.assertFalse(result['success'])
        self.assertIn('cancelled', result['error'])
        self.assertTrue(stuck.cancelled.wait(2),
                        'the coroutine is still running after the deadline')
        locked = asyncio.run_coroutine_threadsafe(
            asyncio.sleep(0, result=stuck.lock.locked()), loop).result(2)
        self.assertFalse(locked, 'the headless lock is still held, so every '
                                 'later headless tool would block for ever')

    def test_the_deadline_outlasts_the_wait_and_act_cap(self):
        """A legitimately slow browser_wait_and_act must finish rather than be
        cut off by the dispatch deadline."""
        import headless_backend
        self.assertGreater(server.HEADLESS_CALL_TIMEOUT * 1000,
                           headless_backend.MAX_WAIT_AND_ACT_TIMEOUT_MS)

    def test_the_schema_states_the_cap_it_enforces(self):
        tool = next(t for t in server.MCP_TOOLS
                    if t.name == 'browser_wait_and_act')
        schema = tool.input_schema['properties']['timeout_ms']
        import headless_backend
        self.assertEqual(schema['maximum'],
                         headless_backend.MAX_WAIT_AND_ACT_TIMEOUT_MS,
                         'the schema must advertise the cap the backend applies')


class RetentionConfigTests(unittest.TestCase):
    """These two variables are read at import time and the native host runs
    the server with stderr=DEVNULL, so a bad value used to be a server that
    never starts with the traceback thrown away - the user saw only restart
    backoff."""

    def _read(self, raw, default, cast):
        with unittest.mock.patch.dict(os.environ, {'CCB_TEST_NUM': raw}):
            return safety._env_number('CCB_TEST_NUM', default, cast)

    def test_an_unparseable_value_falls_back_to_the_default(self):
        for raw in ('7d', 'forever', '', 'one week'):
            with self.subTest(value=raw):
                self.assertEqual(self._read(raw, 7.0, float), 7.0)

    def test_a_fractional_file_cap_falls_back(self):
        self.assertEqual(self._read('0.5', 500, int), 500)

    def test_a_negative_value_falls_back(self):
        self.assertEqual(self._read('-1', 500, int), 500)

    def test_a_valid_value_is_honoured(self):
        self.assertEqual(self._read('3', 500, int), 3)
        self.assertEqual(self._read('0', 500, int), 0,
                         'zero disables the cap and must be honoured')

    def test_an_unset_variable_uses_the_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CCB_TEST_NUM', None)
            self.assertEqual(safety._env_number('CCB_TEST_NUM', 7.0, float),
                             7.0)


class ScreenshotPruningTests(unittest.TestCase):
    """Screenshots hold whatever was on screen and nothing ever removed them,
    making the directory the longest-lived record of the user's browsing in
    the project."""

    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix='ccb-shots-'))
        # Pruning only touches a directory this project created, so the tests
        # that expect deletion have to say the directory is ours.
        (self.dir / safety.OWNED_DIR_MARKER).touch()
        # Both come from CLAUDE_BROWSER_SCREENSHOT_* at import time, so a
        # developer with either exported ran different tests from CI: with
        # RETENTION_DAYS=60 the month-old screenshot below survives and two
        # tests failed for a reason that had nothing to do with the code.
        for name, pinned in (('SCREENSHOT_RETENTION_DAYS', 7.0),
                             ('SCREENSHOT_MAX_FILES', 500)):
            self.addCleanup(setattr, safety, name, getattr(safety, name))
            setattr(safety, name, pinned)

    def _shot(self, name, age_days=0):
        import os, time
        path = self.dir / name
        path.write_bytes(b'\x89PNG\r\n\x1a\n')
        if age_days:
            old = time.time() - age_days * 86400
            os.utime(path, (old, old))
        return path

    def test_old_screenshots_are_removed(self):
        fresh = self._shot('fresh.png')
        stale = self._shot('stale.png', age_days=30)

        safety.prune_screenshots(self.dir)

        self.assertTrue(fresh.exists(), 'a recent screenshot must survive')
        self.assertFalse(stale.exists(), 'a month-old screenshot must not')

    def test_the_file_cap_removes_the_oldest_first(self):
        original = safety.SCREENSHOT_MAX_FILES
        safety.SCREENSHOT_MAX_FILES = 3
        try:
            paths = [self._shot(f's{i}.png', age_days=i * 0.001)
                     for i in range(6)]
            safety.prune_screenshots(self.dir)
            surviving = sorted(p.name for p in self.dir.glob('*.png'))
            self.assertEqual(len(surviving), 3)
            # s0 is the oldest by mtime, so it should be among those removed.
            self.assertNotIn('s5.png', surviving)
        finally:
            safety.SCREENSHOT_MAX_FILES = original

    def test_retention_can_be_disabled(self):
        age, cap = safety.SCREENSHOT_RETENTION_DAYS, safety.SCREENSHOT_MAX_FILES
        safety.SCREENSHOT_RETENTION_DAYS = 0
        safety.SCREENSHOT_MAX_FILES = 0
        try:
            stale = self._shot('ancient.png', age_days=400)
            safety.prune_screenshots(self.dir)
            self.assertTrue(stale.exists(),
                            'keeping an indefinite record must remain possible')
        finally:
            safety.SCREENSHOT_RETENTION_DAYS = age
            safety.SCREENSHOT_MAX_FILES = cap

    def test_a_missing_directory_does_not_raise(self):
        result = safety.prune_screenshots(self.dir / 'does-not-exist')
        self.assertEqual(result, {'removed_age': 0, 'removed_count': 0})

    def test_a_directory_we_did_not_create_is_never_pruned(self):
        """CLAUDE_BROWSER_SCREENSHOTS_DIR can point at ~/Pictures or a repo's
        docs/screenshots. Pruning every *.png older than the retention window
        there would delete the user's own files on the first screenshot, which
        is a worse failure than the retention it closes."""
        theirs = self.dir / 'not-ours'
        theirs.mkdir()
        holiday = theirs / 'holiday.png'
        holiday.write_bytes(b'\x89PNG\r\n\x1a\n')
        import os, time
        old = time.time() - 400 * 86400
        os.utime(holiday, (old, old))

        result = safety.prune_screenshots(theirs)

        self.assertTrue(holiday.exists(),
                        "a 400-day-old PNG in a directory we did not create "
                        "must survive")
        self.assertEqual(result, {'removed_age': 0, 'removed_count': 0})

    def test_the_marker_is_written_for_the_default_directory(self):
        with unittest.mock.patch.dict(
                os.environ, {'CLAUDE_BROWSER_SCREENSHOTS_DIR': ''},
                clear=False):
            os.environ.pop('CLAUDE_BROWSER_SCREENSHOTS_DIR')
            resolved = safety.resolve_screenshots_dir()
        self.assertTrue((resolved / safety.OWNED_DIR_MARKER).exists(),
                        'our own directory must be marked as prunable')

    def test_an_override_at_a_fresh_path_is_ours(self):
        fresh = self.dir / 'fresh-override'
        with unittest.mock.patch.dict(
                os.environ,
                {'CLAUDE_BROWSER_SCREENSHOTS_DIR': str(fresh)}):
            resolved = safety.resolve_screenshots_dir()
        self.assertTrue((resolved / safety.OWNED_DIR_MARKER).exists(),
                        'a directory we created is ours to prune')

    def test_an_override_at_an_existing_path_is_not_ours(self):
        theirs = self.dir / 'pictures'
        theirs.mkdir()
        with unittest.mock.patch.dict(
                os.environ,
                {'CLAUDE_BROWSER_SCREENSHOTS_DIR': str(theirs)}):
            resolved = safety.resolve_screenshots_dir()
        self.assertFalse((resolved / safety.OWNED_DIR_MARKER).exists(),
                         'a directory that already held the user\'s files '
                         'must not become prunable')

    def test_an_uppercase_extension_is_pruned_too(self):
        """The filename comes from the caller, so capture.PNG is reachable and
        was kept forever by a case-sensitive glob."""
        stale = self._shot('CAPTURE.PNG', age_days=30)
        safety.prune_screenshots(self.dir)
        self.assertFalse(stale.exists())

    def test_non_png_files_are_left_alone(self):
        note = self.dir / 'notes.txt'
        note.write_text('not a screenshot')
        safety.prune_screenshots(self.dir)
        self.assertTrue(note.exists())


class WebSocketOriginTests(unittest.TestCase):
    """WebSockets are exempt from CORS, so any page the user visits can open a
    connection to the loopback port. The token refuses it, but only after the
    handshake and a 10s wait for the first frame."""

    def test_only_non_browser_origins_are_accepted_by_default(self):
        self.assertEqual(server.ALLOWED_WS_ORIGINS, [None],
                         'a web page always sends an Origin; refuse it at the '
                         'handshake rather than after')

    def test_an_override_adds_to_the_default_instead_of_replacing_it(self):
        """None in the list means "no Origin header", which is the only shape
        a local non-browser client has. Replacing it gave every one of them a
        403 - so naming one custom origin killed the only client that works
        today."""
        self.assertEqual(server.parse_ws_origins('moz-extension://abc'),
                         [None, 'moz-extension://abc'])
        self.assertEqual(server.parse_ws_origins('one,two'),
                         [None, 'one', 'two'])

    def test_an_empty_override_means_unset_not_allow_nothing(self):
        """A trailing comma used to silently re-permit no-Origin clients, and
        an empty value was ignored. Both now mean the default."""
        for raw in ('', ',,', '   ', 'moz-extension://abc,'):
            with self.subTest(raw=raw):
                origins = server.parse_ws_origins(raw)
                self.assertIn(None, origins)

    def test_a_star_disables_the_check_explicitly(self):
        """There was no way to reach origins=None, and "*" was taken as a
        literal origin, so it refused everything instead of allowing it."""
        self.assertIsNone(server.parse_ws_origins('*'))

    @unittest.skipUnless(server.HAS_WEBSOCKETS, 'websockets is not installed')
    def test_the_serve_call_honours_the_origin_list(self):
        """This used to assert on inspect.getsource() text, which cannot tell
        whether origins= is actually passed and breaks on a rename. Drive the
        real call and look at the kwargs instead."""
        captured = {}

        class FakeServer:
            async def wait_closed(self):
                return None

        async def fake_serve(handler, host, port, **kwargs):
            captured.update(kwargs)
            return FakeServer()

        origins = [None, 'moz-extension://abc']
        with unittest.mock.patch.object(server, 'HAS_WEBSOCKETS', True), \
                unittest.mock.patch.object(server, 'ALLOWED_WS_ORIGINS', origins), \
                unittest.mock.patch.object(server, 'MAIN_EVENT_LOOP', None), \
                unittest.mock.patch.object(server.websockets, 'serve', fake_serve):
            asyncio.run(server.run_websocket_server())

        self.assertEqual(captured.get('origins'), origins,
                         'the handshake check is only real if serve() gets it')


def _wrong_keys():
    token = server.API_TOKEN
    return {
        'missing': None,
        'empty': '',
        'last character changed': token[:-1] + ('1' if token[-1] == '0' else '0'),
        'prefix': token[:10],
        'token plus a suffix': token + '0',
        # http.server decodes headers as latin-1, and compare_digest raises
        # TypeError on a non-ASCII str rather than returning False.
        'non-ascii': '\xff' * len(token),
    }


class HttpAuthTests(unittest.TestCase):
    """The token is full control of the user's browser. Every other HTTP test
    stubs _check_auth out, so a mutation run found the suite stayed green with
    authentication switched off, and with the token reduced to a prefix
    oracle. These drive the real handler over a real socket."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0),
                                               server.MCPHTTPHandler)
        cls.httpd.daemon_threads = True
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, method, path, key=None, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=body)
        if key is not None:
            req.add_header('X-API-Key', key)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_the_real_key_is_accepted(self):
        """Without this the refusals below could pass against a server that
        refuses everyone."""
        status, _, body = self.request('GET', '/mcp/tools', server.API_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn(b'"tools"', body)

    def test_a_wrong_key_is_refused_on_every_get_endpoint(self):
        for label, key in _wrong_keys().items():
            for path in ('/mcp/tools', '/screenshots', '/browser/poll'):
                with self.subTest(key=label, path=path):
                    status, _, body = self.request('GET', path, key)
                    self.assertEqual(status, 403)
                    self.assertNotIn(b'"tools"', body)
                    self.assertNotIn(b'"screenshots"', body)

    def test_a_refused_poll_does_not_take_a_queued_command(self):
        """A process that could poll without the token would read every
        command meant for the browser, and the browser would never run it."""
        command = {'action': 'click', 'queuedAt': time.time()}
        with server._PENDING_COMMANDS_LOCK:
            self.httpd._pending_commands = [command]
        try:
            for label, key in _wrong_keys().items():
                with self.subTest(key=label):
                    status, _, body = self.request('GET', '/browser/poll', key)
                    self.assertEqual(status, 403)
                    self.assertNotIn(b'click', body)
            self.assertEqual(self.httpd._pending_commands, [command])
        finally:
            with server._PENDING_COMMANDS_LOCK:
                self.httpd._pending_commands = []

    def test_a_wrong_key_cannot_run_a_tool_or_forge_a_response(self):
        executed, forged = [], []
        with unittest.mock.patch.object(
                server.MCPHTTPHandler, 'execute_tool',
                lambda self, name, args: executed.append(name) or {}), \
                unittest.mock.patch.object(
                    server.connection_manager, 'handle_response',
                    forged.append):
            for label, key in _wrong_keys().items():
                with self.subTest(key=label):
                    status, _, _ = self.request(
                        'POST', '/mcp/call', key,
                        b'{"tool": "browser_navigate", '
                        b'"arguments": {"url": "https://example.com"}}')
                    self.assertEqual(status, 403)
                    status, _, _ = self.request(
                        'POST', '/browser/response', key,
                        b'{"requestId": "1", "success": true}')
                    self.assertEqual(status, 403)
        self.assertEqual(executed, [])
        self.assertEqual(forged, [])

    def test_a_refused_post_still_delivers_its_error_body(self):
        """The 403 was sent before the request body was read, so closing
        with those bytes unread reset the connection - about four times in
        ten the client lost the error body, and a caller with a stale token
        saw "connection reset" rather than "Unauthorized"."""
        body = b'{"tool": "browser_navigate", "arguments": {}}'
        for attempt in range(50):
            req = urllib.request.Request(self.base + '/mcp/call',
                                         method='POST', data=body)
            req.add_header('X-API-Key', 'wrong')
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 403)
            self.assertIn(b'Unauthorized', ctx.exception.read(),
                          f'attempt {attempt}')

    def test_a_refused_post_never_waits_on_a_claimed_body(self):
        """The body is read before refusing only up to a small cap. A caller
        without the token that claims a huge or negative length and then
        sends almost nothing must still get its 403 at once: reading the
        claimed length (or to EOF, for -1) would park the worker thread."""
        for claimed in ('10000000', '-1'):
            with self.subTest(content_length=claimed):
                host, port = self.httpd.server_address
                with socket.create_connection((host, port), timeout=5) as sock:
                    sock.sendall(
                        b'POST /mcp/call HTTP/1.1\r\n'
                        b'Host: 127.0.0.1\r\nX-API-Key: wrong\r\n'
                        b'Content-Length: ' + claimed.encode() + b'\r\n\r\n'
                        b'{"a": 1}')
                    sock.settimeout(2)
                    try:
                        status = sock.recv(64)
                    except socket.timeout:
                        self.fail('no answer: the server is waiting on a '
                                  'body it was never going to accept')
                self.assertTrue(status.startswith(b'HTTP/1.0 403')
                                or status.startswith(b'HTTP/1.1 403'), status)

    def test_a_cors_preflight_is_refused(self):
        """With a preflight allowed, any web page the user visits could try
        keys against the API from their browser."""
        status, headers, _ = self.request('OPTIONS', '/mcp/call')
        self.assertEqual(status, 403)
        self.assertFalse([h for h in headers.keys()
                          if h.lower().startswith('access-control-')])

    def test_health_answers_without_a_key_and_reveals_no_token(self):
        """The one deliberate exception: the native host probes it before it
        has read the token."""
        status, _, body = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertNotIn(server.API_TOKEN.encode(), body)


@unittest.skipUnless(server.HAS_WEBSOCKETS, 'websockets is not installed')
class WebSocketAuthTests(unittest.IsolatedAsyncioTestCase):
    """Whoever passes the handshake is "the browser": it receives every
    automation command and can forge the results. The suite only covered the
    Origin list, never the token frame."""

    async def asyncSetUp(self):
        self.registered = []
        for name, fake in (
                ('register_browser',
                 lambda browser_id, ws: self.registered.append(browser_id)),
                ('unregister_browser', lambda browser_id: None)):
            patcher = unittest.mock.patch.object(
                server.connection_manager, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ws_server = await server.websockets.serve(
            server.websocket_handler, '127.0.0.1', 0)
        port = next(iter(self.ws_server.sockets)).getsockname()[1]
        self.url = f'ws://127.0.0.1:{port}'

    async def asyncTearDown(self):
        self.ws_server.close()
        await self.ws_server.wait_closed()

    async def close_code_after(self, first_frame):
        async with server.websockets.connect(self.url) as ws:
            await ws.send(first_frame)
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except server.websockets.exceptions.ConnectionClosed as e:
                return e.rcvd.code if e.rcvd else None
        self.fail('the server neither closed the connection nor stayed silent')

    async def test_a_bad_first_frame_is_closed_and_never_registered(self):
        token = server.API_TOKEN
        frames = {
            'wrong token': '{"token": "wrong"}',
            'prefix of the token': '{"token": "%s"}' % token[:10],
            'token plus a suffix': '{"token": "%s0"}' % token,
            'non-ascii token': '{"token": "\\u00ff\\u00ff"}',
            'no token': '{}',
            'null token': '{"token": null}',
            'not json': 'not json',
            'a list': '["%s"]' % token,
            'token at the wrong key': '{"key": "%s"}' % token,
        }
        for label, frame in frames.items():
            with self.subTest(frame=label):
                self.assertEqual(await self.close_code_after(frame), 1008)
        self.assertEqual(self.registered, [])

    async def test_the_real_token_is_registered(self):
        """Without this the refusals above could pass against a handler that
        refuses everyone."""
        async with server.websockets.connect(self.url) as ws:
            await ws.send('{"token": "%s"}' % server.API_TOKEN)
            for _ in range(100):
                if self.registered:
                    break
                await asyncio.sleep(0.02)
        self.assertEqual(len(self.registered), 1)


class DeprecatedEndpointTests(unittest.TestCase):

    def test_browser_command_no_longer_claims_success(self):
        """It returned {"success": true, "message": "Command queued"} while
        queueing nothing, so its only caller was told its work was accepted.
        Driven through the handler rather than grepped, so it asserts
        behaviour and not the shape of the source."""
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler.path = '/browser/command'
        handler.headers = {'Content-Length': '0'}
        handler._check_auth = lambda: True
        handler.rfile = None
        captured = {}

        def send(data, status=200, reason=None):
            captured['data'] = data
            captured['status'] = status

        handler.send_json_response = send
        handler.do_POST()

        self.assertEqual(captured['status'], 410,
                         'a dead endpoint should say so, not return 200')
        self.assertIs(captured['data']['success'], False)
        self.assertIn('queues nothing', captured['data']['error'])

    def test_the_410_says_what_to_do_in_the_status_line(self):
        """The body never reaches the caller: the native host uses
        urllib.request.urlopen, which raises HTTPError on 410 and throws the
        body away, so the operator saw "connection failed: HTTP Error 410:
        Gone". HTTPError does carry the status line's phrase."""
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler.path = '/browser/command'
        handler.headers = {'Content-Length': '0'}
        handler._check_auth = lambda: True
        handler.rfile = None
        captured = {}

        def send(data, status=200, reason=None):
            captured['status'] = status
            captured['reason'] = reason

        handler.send_json_response = send
        handler.do_POST()

        self.assertIsNotNone(captured['reason'],
                             'without a reason phrase the caller only gets "Gone"')
        self.assertIn('MCP tool', captured['reason'])
        self.assertNotIn('\n', captured['reason'])


class FlagParsingTests(unittest.TestCase):
    """bool("false") is True. Flags were read with arguments.get(name, True),
    so a caller that explicitly declined got the opposite - and two of these
    decide whether a PNG of the screen is written to disk. Nothing in the
    Python suite passed a string flag before (grep "'true'|'false'" tests/
    found nothing), although capture_bodies: "false" was confirmed live."""

    def test_string_flags_are_read_as_booleans(self):
        for raw in ('false', 'False', 'FALSE', ' off ', 'no', '0'):
            with self.subTest(raw=raw):
                self.assertFalse(server.parse_flag(raw, True))
        for raw in ('true', 'True', ' on ', 'yes', '1'):
            with self.subTest(raw=raw):
                self.assertTrue(server.parse_flag(raw, False))

    def test_real_booleans_and_numbers_still_work(self):
        self.assertTrue(server.parse_flag(True, False))
        self.assertFalse(server.parse_flag(False, True))
        self.assertFalse(server.parse_flag(0, True))
        self.assertTrue(server.parse_flag(1, False))

    def test_a_missing_or_unreadable_value_falls_back(self):
        """A typo must not silently mean the opposite of what it says."""
        for raw in (None, '', 'maybe', 'FALSEISH', [], {}):
            with self.subTest(raw=raw):
                self.assertTrue(server.parse_flag(raw, True))
                self.assertFalse(server.parse_flag(raw, False))


class NumericArgumentTests(unittest.TestCase):
    """parse_flag covered the booleans and the numbers were left on bare
    int(). "limit" is the one numeric the server reads itself rather than
    forwarding, and int() got it wrong twice: int("12.7") raises, so a caller
    asking for 12 tabs was given the 50-tab default instead, and
    int(float('inf')) raises OverflowError, which the except clause did not
    catch - json.loads accepts the literal Infinity, so a client can send
    it."""

    def test_numeric_strings_are_read_as_numbers(self):
        self.assertEqual(server.parse_int('12', 5), 12)
        self.assertEqual(server.parse_int(' 12 ', 5), 12)
        self.assertEqual(server.parse_int('12.7', 5), 12)
        self.assertEqual(server.parse_number('1.5', 5.0), 1.5)

    def test_unusable_values_fall_back(self):
        for raw in (None, '', 'lots', [], {}, True, False,
                    float('inf'), float('-inf'), float('nan')):
            with self.subTest(raw=raw):
                self.assertEqual(server.parse_int(raw, 5), 5)

    def test_a_string_limit_is_honoured_rather_than_replaced(self):
        self.assertEqual(server.clamp_tab_limit('12'), 12)
        self.assertEqual(server.clamp_tab_limit('12.7'), 12,
                         'a caller asking for fewer tabs must not be given '
                         'the larger default')

    def test_an_infinite_limit_does_not_crash_the_handler(self):
        self.assertEqual(server.clamp_tab_limit(float('inf')),
                         server.DEFAULT_TAB_LIMIT)

    def test_an_integer_too_large_for_a_float_falls_back(self):
        """json.loads turns a long run of digits into an exact int, and
        float() on one too large to represent raises OverflowError - not
        ValueError. It escaped execute_tool, which has no broad catch, and
        killed the connection with no JSON body and no audit entry."""
        huge = 10 ** 400
        self.assertEqual(server.parse_number(huge, 1.5), 1.5)
        self.assertEqual(server.parse_int(huge, 5), 5)
        self.assertEqual(server.parse_number(str(huge), 1.5), 1.5)
        self.assertEqual(server.clamp_tab_limit(huge), server.DEFAULT_TAB_LIMIT)

    def test_a_tab_id_too_large_for_a_float_is_a_plain_error(self):
        with self.assertRaises(ValueError):
            server.parse_tab_id(10 ** 400)

    def test_such_a_tab_id_gets_a_json_answer_not_a_dropped_socket(self):
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._dispatch_action = lambda action, tab_id, arguments: {'success': True}
        out = handler.execute_tool('browser_get_page_info',
                                   {'tab_id': 10 ** 400})
        self.assertFalse(out['success'])
        self.assertIn('tab_id', out['error'])


class ScreenshotSaveFlagTests(unittest.TestCase):
    """save_to_file is the privacy-relevant one: the PNG lands in
    ~/.claudecodebrowser/screenshots and is kept for the retention window.
    Driven through the real _save_screenshot - the handler stub used
    elsewhere never reaches it."""

    PNG = base64.b64encode(b'\x89PNG\r\n\x1a\x0a').decode()

    def setUp(self):
        self.handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        self.name = f'ccb-flag-test-{os.getpid()}-{time.time_ns()}.png'
        self.path = server.SCREENSHOTS_DIR / self.name

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def _save(self, arguments):
        result = {'success': True, 'data': self.PNG,
                  'tab': {'id': 1, 'url': 'https://example.com/x', 'title': 'x'}}
        return self.handler._save_screenshot(result, dict(arguments, filename=self.name))

    def test_a_string_false_still_declines_the_write(self):
        out = self._save({'save_to_file': 'false'})
        self.assertFalse(self.path.exists(),
                         'the caller declined; nothing may be written to disk')
        self.assertNotIn('filepath', out)

    def test_a_real_false_declines_the_write(self):
        self._save({'save_to_file': False})
        self.assertFalse(self.path.exists())

    def test_a_private_window_screenshot_is_not_written_to_disk(self):
        """The image still goes to the agent, but a copy kept for the
        retention window is the longer-lived record private windows exist to
        prevent - and startLogging already refuses a private tab."""
        result = {'success': True, 'data': self.PNG, 'privateWindow': True,
                  'tab': {'id': 1, 'url': 'https://example.com/x', 'title': 'x'}}
        out = self.handler._save_screenshot(
            result, {'filename': self.name})

        self.assertFalse(self.path.exists(),
                         'a private window was recorded on disk')
        self.assertEqual(out['saved'], False)
        self.assertIn('private', out['note'].lower())
        self.assertEqual(out['data'], self.PNG,
                         'the caller still gets the image it asked for')

    def test_an_ordinary_window_screenshot_is_written(self):
        self.handler._save_screenshot(
            {'success': True, 'data': self.PNG, 'tab': {'id': 1}},
            {'filename': self.name})
        self.assertTrue(self.path.exists())

    def test_a_written_screenshot_is_private_to_the_user(self):
        """The directory is 0700, but the file asks for 0600 as well, so a
        copy or a looser directory does not expose what was on screen. Set a
        permissive umask, or the umask alone would make this pass."""
        old_umask = os.umask(0o022)
        try:
            self.handler._save_screenshot(
                {'success': True, 'data': self.PNG, 'tab': {'id': 1}},
                {'filename': self.name})
        finally:
            os.umask(old_umask)
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_a_filename_of_only_directory_components_is_replaced(self):
        """Path('..').name is '..', so this resolved to the parent directory
        and O_NOFOLLOW|O_CREAT failed with EISDIR - reported as an unrelated
        error."""
        for hostile in ('..', '.', '/', '../'):
            with self.subTest(filename=hostile):
                out = self.handler._save_screenshot(
                    {'success': True, 'data': self.PNG, 'tab': {}},
                    {'filename': hostile})
                self.assertTrue(out.get('success'), out.get('error'))
                written = Path(out['filepath'])
                self.addCleanup(lambda p=written: p.unlink(missing_ok=True))
                self.assertEqual(written.parent, server.SCREENSHOTS_DIR)
                self.assertTrue(written.is_file())

    def test_a_filename_the_sweep_would_miss_is_given_a_png_suffix(self):
        """The payload is always a PNG, and both prune_screenshots and
        GET /screenshots look for *.png. filename: "dashboard.jpg" was
        written, never listed and never pruned - the longest-lived copy of
        the user's screen in the project."""
        for given, expected in (('dashboard.jpg', 'dashboard.png'),
                                ('report', 'report.png'),
                                ('shot.PNG', 'shot.png')):
            with self.subTest(filename=given):
                out = self.handler._save_screenshot(
                    {'success': True, 'data': self.PNG, 'tab': {}},
                    {'filename': given})
                self.assertTrue(out.get('success'), out.get('error'))
                written = Path(out['filepath'])
                self.addCleanup(lambda p=written: p.unlink(missing_ok=True))
                self.assertEqual(written.name, expected)
                self.assertTrue(written.is_file())

    def test_the_default_still_saves(self):
        out = self._save({})
        self.assertTrue(self.path.exists(), 'the documented default is to save')
        self.assertEqual(out['filepath'], str(self.path))

    def test_a_string_true_saves(self):
        self._save({'save_to_file': 'true'})
        self.assertTrue(self.path.exists())


class CurrentUrlTrackingTests(unittest.TestCase):
    """The guard judges the blocklist, the allowlist and protected-site
    confirmation against the page it believes the browser is on. The
    screenshot path returned before note_url, and a screenshot result is the
    one result that always carries a URL - so after a click landed on a bank,
    every one of those checks was still judging the previous page while the
    agent photographed the new one."""

    PNG = base64.b64encode(b'\x89PNG\r\n\x1a\x0a').decode()

    def setUp(self):
        self.guard = safety.get_safety_guard()
        self.original_url = self.guard._current_url
        self.guard._current_url = 'https://example.com/start'
        server.connection_manager.http_pending_requests.clear()

    def tearDown(self):
        self.guard._current_url = self.original_url
        server.connection_manager.http_pending_requests.clear()

    def _dispatch(self, action, arguments, response):
        """Drive the real _dispatch_action over the HTTP polling transport,
        answering its waiter the way /browser/response would."""
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler.server = type('S', (), {'_pending_commands': []})()
        pending = server.connection_manager.http_pending_requests

        def answer():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                for request_id, (event, holder) in list(pending.items()):
                    holder['response'] = dict(response)
                    event.set()
                    return
                time.sleep(0.005)

        responder = threading.Thread(target=answer)
        responder.start()
        try:
            return handler._dispatch_action(action, None, dict(arguments))
        finally:
            responder.join(timeout=11)

    def test_a_screenshot_result_updates_the_tracked_url(self):
        out = self._dispatch('screenshot', {'save_to_file': False}, {
            'success': True, 'data': self.PNG,
            'tab': {'id': 7, 'url': 'https://www.chase.com/transfer',
                    'title': 'Transfer'}})
        self.assertTrue(out.get('success'))
        self.assertEqual(self.guard._current_url,
                         'https://www.chase.com/transfer')

    def test_the_guard_then_protects_the_page_that_was_photographed(self):
        """The consequence, not just the bookkeeping: a click after the
        screenshot has to be confirmed."""
        self._dispatch('screenshot', {'save_to_file': False}, {
            'success': True, 'data': self.PNG,
            'tab': {'id': 7, 'url': 'https://www.chase.com/transfer',
                    'title': 'Transfer'}})
        denial = self.guard.check('browser_click', {'selector': '#send'})
        self.assertIsNotNone(denial, 'the bank page was on screen')
        self.assertEqual(denial['safety_decision'], 'confirmation_required')

    def test_a_navigate_result_updates_the_tracked_url(self):
        self._dispatch('navigate', {'url': 'https://example.org/next'},
                       {'success': True, 'url': 'https://example.org/next'})
        self.assertEqual(self.guard._current_url, 'https://example.org/next')

    def test_note_url_reads_both_result_shapes(self):
        """navigate reports {"url": ...}; screenshot reports
        {"tab": {"id", "url", "title"}}."""
        self.guard.note_url({'success': True, 'url': 'https://a.example/1'})
        self.assertEqual(self.guard._current_url, 'https://a.example/1')
        self.guard.note_url({'success': True, 'data': 'x',
                             'tab': {'id': 2, 'url': 'https://b.example/2',
                                     'title': 't'}})
        self.assertEqual(self.guard._current_url, 'https://b.example/2')

    def test_a_result_with_no_url_leaves_the_last_one_in_place(self):
        """A click that navigates returns {"success": true, ...contentResult}
        with no url and no tab, so the tracker cannot learn from it. It keeps
        the previous page rather than forgetting - an unknown URL would skip
        the protected-site check entirely."""
        self.guard.note_url({'success': True, 'clicked': True})
        self.assertEqual(self.guard._current_url, 'https://example.com/start')


class HumanApprovalBranchTests(unittest.TestCase):
    """The in-browser Approve/Deny prompt is the default enforcement in
    attended mode and had no Python test at all."""

    def setUp(self):
        if server.HEADLESS_MODE:
            self.skipTest('the approval branch only runs in attended mode')
        self.guard = safety.get_safety_guard()
        self.original_url = self.guard._current_url
        self.guard.note_url({'url': 'https://www.chase.com/transfer'})

    def tearDown(self):
        self.guard._current_url = self.original_url

    def _click(self, approval):
        dispatched = []
        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._request_human_approval = (
            lambda tool_name, arguments, denial, tab_id=None: approval)

        def fake_dispatch(action, tab_id, arguments):
            dispatched.append(action)
            return {'success': True}

        handler._dispatch_action = fake_dispatch
        result = handler.execute_tool('browser_click', {'selector': '#send'})
        return result, dispatched

    def test_an_approved_action_goes_through(self):
        result, dispatched = self._click({'success': True, 'approved': True})
        self.assertTrue(result.get('success'), result)
        self.assertEqual(dispatched, ['click'])

    def test_a_denied_action_is_refused_and_not_dispatched(self):
        result, dispatched = self._click({'success': True, 'approved': False})
        self.assertFalse(result['success'])
        self.assertEqual(result['safety_decision'], 'human_denied')
        self.assertEqual(dispatched, [])

    def test_an_undeliverable_prompt_is_a_refusal_not_a_token(self):
        """Falling through to the token denial would hand the agent a token
        and tell it to re-send the call itself, which is not a human in the
        loop at all."""
        result, dispatched = self._click({'success': False, 'error': 'no browser'})
        self.assertFalse(result['success'])
        self.assertEqual(result['safety_decision'], 'approval_undeliverable')
        self.assertNotIn('confirm_token', result)
        self.assertEqual(dispatched, [])

    def test_an_approved_action_is_checked_again_as_human_approved(self):
        """The approval satisfies only the protected-site confirmation. The
        second pass is what still applies the rest of the policy, and what
        records the call as allowed_by_human in the audit log."""
        with unittest.mock.patch.object(
                self.guard, 'check', wraps=self.guard.check) as spy:
            result, dispatched = self._click({'success': True, 'approved': True})
        self.assertTrue(result.get('success'), result)
        self.assertEqual(dispatched, ['click'])
        self.assertEqual([c.kwargs.get('human_approved', False)
                          for c in spy.call_args_list], [False, True])

    def test_an_approval_does_not_override_a_later_denial(self):
        """Whatever the second pass refuses stays refused: the person approved
        a protected action, not a rate limit or a blocklist hit."""
        later = {'success': False, 'safety_decision': 'rate_limited',
                 'error': 'too many calls'}
        real_check = self.guard.check

        def check(tool_name, arguments, human_approved=False):
            return later if human_approved else real_check(tool_name, arguments)

        with unittest.mock.patch.object(self.guard, 'check', check):
            result, dispatched = self._click({'success': True, 'approved': True})
        self.assertEqual(result, later)
        self.assertEqual(dispatched, [])

    def test_headless_mode_does_not_wait_for_a_human_who_is_not_there(self):
        """Headless has nobody at the browser, so it keeps the token flow
        rather than blocking on a prompt nobody will answer."""
        def refuse(*args, **kwargs):
            raise AssertionError('headless mode must not show a prompt')

        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._request_human_approval = refuse
        dispatched = []
        handler._dispatch_action = (
            lambda action, tab_id, arguments: dispatched.append(action) or {})
        with unittest.mock.patch.object(server, 'HEADLESS_MODE', True):
            result = handler.execute_tool('browser_click', {'selector': '#send'})
        self.assertFalse(result['success'])
        self.assertTrue(result.get('confirmation_required'), result)
        self.assertEqual(dispatched, [])

    def test_an_unprotected_page_is_not_prompted_about(self):
        self.guard.note_url({'url': 'https://example.com/page'})

        def refuse(*args, **kwargs):
            raise AssertionError('no prompt should be shown here')

        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._request_human_approval = refuse
        handler._dispatch_action = lambda action, tab_id, arguments: {'success': True}
        self.assertTrue(handler.execute_tool(
            'browser_click', {'selector': '#ok'})['success'])


class TabLimitTests(unittest.TestCase):
    """browser_find_tabs honoured limit as given and the extension caps
    nothing: {"url_pattern": "stub", "limit": 100000} returned 500 tabs."""

    def test_the_limit_is_clamped(self):
        self.assertEqual(server.clamp_tab_limit(100000), server.MAX_TAB_LIMIT)
        self.assertEqual(server.clamp_tab_limit(10), 10)

    def test_a_nonsense_limit_falls_back_to_the_default(self):
        for raw in (0, -5, 'all', None):
            with self.subTest(raw=raw):
                self.assertEqual(server.clamp_tab_limit(raw),
                                 server.DEFAULT_TAB_LIMIT)

    def test_the_clamp_is_applied_before_the_browser_sees_it(self):
        captured = {}

        def fake_dispatch(action, tab_id, arguments):
            captured['arguments'] = arguments
            return {'success': True}

        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._dispatch_action = fake_dispatch
        handler.execute_tool('browser_find_tabs',
                             {'url_pattern': 'stub', 'limit': 100000})
        self.assertEqual(captured['arguments']['limit'], server.MAX_TAB_LIMIT)

    def test_the_advertised_cap_is_the_one_the_extension_enforces(self):
        """The schema and both tool descriptions said 200 while the extension
        returned at most 50, so an agent planned around a number it could
        never get."""
        background = (REPO_ROOT / 'extension' / 'background.js').read_text()
        enforced = re.search(r'const MAX_TAB_RESULTS = (\d+);', background)
        self.assertIsNotNone(enforced, 'extension cap constant moved or renamed')
        self.assertEqual(server.MAX_TAB_LIMIT, int(enforced.group(1)))
        for name in ('browser_get_tabs', 'browser_find_tabs'):
            tool = next(t for t in server.MCP_TOOLS if t.name == name)
            text = tool.description + str(tool.input_schema)
            self.assertNotIn('200', text,
                             f'{name} still advertises a cap it cannot deliver')

    def test_the_schema_tells_the_agent_what_it_needs(self):
        """The filter requirement was added to the handler and never to the
        schema, so an agent reading the schema called it with {} and got a
        runtime error it had no way to anticipate."""
        tool = next(t for t in server.MCP_TOOLS if t.name == 'browser_find_tabs')
        required = [clause['required'][0] for clause in tool.input_schema['anyOf']]
        self.assertEqual(sorted(required),
                         ['active', 'audible', 'title', 'url', 'url_pattern'])
        self.assertIn('limit', tool.input_schema['properties'])
        self.assertIn('at least one filter', tool.description.lower())


class TabIdCoercionTests(unittest.TestCase):
    """A JSON client sends tab_id: "7". Every fixture here passed an int, so
    the string never reached the transports: the headless backend keys its
    tab table by int, so "1" answered "No headless tab with id 1" for a tab
    that was open, and background.js only coerces the id it is handed."""

    def _execute(self, tool, arguments):
        """Run the real execute_tool and report what it handed the transport."""
        captured = {}

        def fake_dispatch(action, tab_id, arguments):
            captured['action'] = action
            captured['tab_id'] = tab_id
            captured['arguments'] = arguments
            return {'success': True}

        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._dispatch_action = fake_dispatch
        result = handler.execute_tool(tool, dict(arguments))
        return result, captured

    def test_a_string_tab_id_reaches_the_transport_as_an_int(self):
        result, captured = self._execute('browser_get_page_info',
                                         {'tab_id': '7'})
        self.assertTrue(result.get('success'), result)
        self.assertEqual(captured['tab_id'], 7)
        self.assertIsInstance(captured['tab_id'], int)

    def test_tab_zero_is_still_a_tab(self):
        """0 is a real tab id, and it must not collapse to "the active tab"."""
        _, captured = self._execute('browser_get_page_info', {'tab_id': '0'})
        self.assertEqual(captured['tab_id'], 0)

    def test_no_tab_id_still_means_the_active_tab(self):
        _, captured = self._execute('browser_get_page_info', {})
        self.assertIsNone(captured['tab_id'])
        _, captured = self._execute('browser_get_page_info', {'tab_id': ''})
        self.assertIsNone(captured['tab_id'])

    def test_a_value_that_is_not_a_tab_id_is_refused_and_says_why(self):
        """Falling back to the active tab would act on a different page from
        the one the caller named."""
        for raw in ('active', 'last', '7.5', True, [7], -1, float('inf')):
            with self.subTest(raw=raw):
                result, captured = self._execute('browser_get_page_info',
                                                 {'tab_id': raw})
                self.assertFalse(result['success'])
                self.assertIn('tab_id', result['error'])
                self.assertIn('browser_get_tabs', result['error'],
                              'the message must say how to get a usable id')
                self.assertEqual(captured, {},
                                 'a refused call must not be dispatched')

    def test_the_tools_that_take_a_required_tab_id_coerce_it_too(self):
        for tool in ('browser_get_tab_info', 'browser_close_tab',
                     'browser_focus_tab'):
            with self.subTest(tool=tool):
                _, captured = self._execute(tool, {'tab_id': '7'})
                self.assertEqual(captured['tab_id'], 7)

    def test_the_audit_tool_coerces_before_dispatching_itself(self):
        """browser_audit_page bypasses the action map and dispatches
        executeScript directly."""
        result, captured = self._execute('browser_audit_page',
                                         {'tab_id': '3', 'screenshot': False})
        self.assertTrue(result.get('success'), result)
        self.assertEqual(captured['action'], 'executeScript')
        self.assertEqual(captured['tab_id'], 3)

    def test_a_workflow_step_coerces_its_own_tab_id(self):
        result, captured = self._execute('browser_run_workflow', {
            'steps': [{'tool': 'browser_get_page_info',
                       'arguments': {'tab_id': '7'}}],
            'screenshot_on_failure': False})
        self.assertTrue(result['success'], result)
        self.assertEqual(captured['tab_id'], 7)


class ObserverLifetimeSchemaTests(unittest.TestCase):
    """The observer expires after 5 minutes, and nothing in the schema or the
    description said so, so a 10-minute watch stopped silently and the agent
    only learned from expired: true afterwards."""

    def test_the_lifetime_is_in_the_schema_and_the_description(self):
        tool = next(t for t in server.MCP_TOOLS
                    if t.name == 'browser_observe_element')
        self.assertIn('max_lifetime_ms', tool.input_schema['properties'])
        self.assertIn('max_lifetime_ms', tool.description)

    def test_the_argument_reaches_the_browser_under_the_name_it_reads(self):
        """content.js reads options.maxLifetimeMs."""
        self.assertEqual(
            server.camelize_args({'max_lifetime_ms': 600000}),
            {'maxLifetimeMs': 600000})


class AuditToolBypassTests(unittest.TestCase):
    """browser_audit_page dispatches executeScript itself instead of going
    through the action map, so the toggle a user set to keep JavaScript out
    of their pages has to be enforced before that dispatch happens."""

    def setUp(self):
        self.guard = safety.get_safety_guard()
        self.before = dict(self.guard.config)
        self.addCleanup(self.guard.config.update, self.before)

    def _execute(self, tool, arguments):
        captured = []

        def fake_dispatch(action, tab_id, args):
            captured.append(action)
            return {'success': True, 'result': {}}

        handler = server.MCPHTTPHandler.__new__(server.MCPHTTPHandler)
        handler._dispatch_action = fake_dispatch
        return handler.execute_tool(tool, dict(arguments)), captured

    def test_no_script_is_dispatched_when_scripts_are_off(self):
        self.guard.config['allow_script_execution'] = False
        result, captured = self._execute('browser_audit_page',
                                         {'screenshot': False})
        self.assertFalse(result['success'])
        self.assertEqual(result['safety_decision'], 'scripts_disabled')
        self.assertEqual(captured, [], 'the script must not reach the browser')

    def test_the_audit_still_runs_when_scripts_are_allowed(self):
        self.guard.config['allow_script_execution'] = True
        result, captured = self._execute('browser_audit_page',
                                         {'screenshot': False})
        self.assertTrue(result['success'], result)
        self.assertEqual(captured, ['executeScript'])


class HeadlessConsoleDescriptionTests(unittest.TestCase):
    """browser_get_console_logs told an agent that headless "captures the
    page console in full". headless_backend._dispatch has no getConsoleLogs
    branch, so the call comes back "Unsupported headless action" - and an
    agent that believes the description reads that error as "the page logged
    nothing"."""

    def _description(self, name):
        return next(t for t in server.MCP_TOOLS if t.name == name).description

    def test_headless_really_has_no_console_action(self):
        """Asked of the backend rather than grepped from its source, which a
        renamed branch or a dispatch table would have fooled."""
        import headless_backend
        browser = headless_backend.HeadlessBrowser()
        page = object()
        browser._page, browser._tabs = page, {1: page}
        result = asyncio.run(browser._dispatch('getConsoleLogs', None, {}))
        self.assertEqual(result.get('error'),
                         'Unsupported headless action: getConsoleLogs',
                         'headless implements it now: say so in the tool '
                         'descriptions instead of removing this test')

    def test_the_console_tool_does_not_promise_a_headless_capture(self):
        description = self._description('browser_get_console_logs')
        self.assertNotIn('captures the page console in full', description)
        self.assertIn('Unsupported headless action', description,
                      'the agent has to be able to tell the error from an '
                      'empty console')

    def test_the_observer_tool_names_a_buffer_something_can_read(self):
        """Its buffer is window.__ccb_mutations, and browser_get_console_logs
        has never read it."""
        description = self._description('browser_inject_observer')
        self.assertNotIn('readable via browser_get_console_logs', description)
        self.assertIn('__ccb_mutations', description)
        self.assertIn('browser_execute_script', description)


class RedactionListTests(unittest.TestCase):
    """The application log and the audit log kept separate lists, with a
    comment here claiming they were in step. They were not: 'url' was in this
    one only, so a credential in a URL reached audit.jsonl in clear."""

    def test_both_logs_use_one_definition(self):
        self.assertIs(server.redact_for_log, safety.redact_arguments)

    def test_a_credential_in_a_url_does_not_reach_the_log(self):
        redacted = server.redact_for_log({
            'url': 'https://alice:hunter2@intranet.example.com/wiki/Home',
            'key': 'h',
            'text': 'secret',
        })
        self.assertEqual(redacted['url'],
                         'https://***@intranet.example.com/wiki/Home')
        self.assertEqual(redacted['key'], '***')
        self.assertEqual(redacted['text'], '***')


class SafetyGuardTests(unittest.TestCase):

    def test_read_only_mode_still_allows_observation(self):
        guard = safety.SafetyGuard()
        guard.config['read_only'] = True
        self.assertIsNone(guard.check('browser_get_value', {'selector': '#pw'}))
        denial = guard.check('browser_type', {'selector': '#pw', 'text': 'x'})
        self.assertIsNotNone(denial)
        self.assertEqual(denial['safety_decision'], 'read_only')

    def test_non_http_navigation_is_refused(self):
        guard = safety.SafetyGuard()
        for url in ('file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,x'):
            with self.subTest(url=url):
                denial = guard.check('browser_navigate', {'url': url})
                self.assertIsNotNone(denial)
                self.assertEqual(denial['safety_decision'], 'blocked_scheme')


if __name__ == '__main__':
    unittest.main()

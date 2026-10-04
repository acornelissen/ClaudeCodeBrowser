#!/usr/bin/env python3
"""
MCP server configuration and credential-guard plumbing tests.

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

# Point HOME at a throwaway directory before importing the server: it creates
# its API token, config and screenshot directories under the home directory at
# import time, and a test run must not touch the real installation.

from tests import TEST_HOME  # noqa: F401  (redirects HOME on import)

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
        self.assertFalse(str(server.SCREENSHOTS_DIR).startswith('/tmp/'))

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

    def test_the_serve_call_actually_passes_the_origin_list(self):
        """This was commented as intent and left unimplemented once."""
        import inspect
        source = inspect.getsource(server.run_websocket_server)
        self.assertIn('origins=ALLOWED_WS_ORIGINS', source)


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

        def send(data, status=200):
            captured['data'] = data
            captured['status'] = status

        handler.send_json_response = send
        handler.do_POST()

        self.assertEqual(captured['status'], 410,
                         'a dead endpoint should say so, not return 200')
        self.assertIs(captured['data']['success'], False)
        self.assertIn('queues nothing', captured['data']['error'])


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

#!/usr/bin/env python3
"""
MCP server configuration and credential-guard plumbing tests.

Run: python3 -m unittest discover -s tests -v
"""

import asyncio
import base64
import os
import sys
import tempfile
import threading
import time
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

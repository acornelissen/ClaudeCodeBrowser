#!/usr/bin/env python3
"""
Native host tests, covering which processes it is allowed to kill.

Firefox launches the native host automatically, and the host clears whatever
holds the MCP port before starting its own server. It must only ever kill its
own server: an unrelated service on port 8765 is not ours to terminate.

Also covered: the native messaging framing (fed a BytesIO rather than a real
pipe), what the host does and does not write to disk, and reaping the server
processes it spawns.

Run: python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests import TEST_HOME  # noqa: F401  (redirects HOME before the import below)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'native-host'))

import claudecodebrowser_host as host  # noqa: E402


class ProcessOwnershipTests(unittest.TestCase):

    def setUp(self):
        self._real_command = host._process_command
        self.addCleanup(setattr, host, '_process_command', self._real_command)

    def _with_command(self, command):
        host._process_command = lambda pid: command

    def test_our_own_server_is_ours_to_kill(self):
        for command in (
            '/usr/bin/python3 /Users/someone/.claudecodebrowser/mcp-server/server.py',
            'python3 /Users/someone/.claudecodebrowser/mcp-server/server.py',
            'python3.12 /Users/someone/.claudecodebrowser/mcp-server/server.py',
            '/opt/x/bin/python3 -u /Users/someone/.claudecodebrowser/mcp-server/server.py',
            'python3 /Users/someone/.claudecodebrowser/mcp-server/server.py --headless',
        ):
            with self.subTest(command=command):
                self._with_command(command)
                self.assertTrue(host._is_our_server(1234), command)

    def test_server_started_from_a_checkout_is_ours_to_kill(self):
        self._with_command('python3 /Users/someone/src/ClaudeCodeBrowser/mcp-server/server.py')
        self.assertTrue(host._is_our_server(1234))

    def test_unrelated_services_are_not_ours_to_kill(self):
        for command in (
            '/usr/local/bin/node /Users/someone/work/api/dist/index.js',
            'python3 -m http.server 8765',
            'docker-proxy -container-port 8765',
            '/opt/homebrew/bin/postgres -D /opt/homebrew/var/postgres',
            'python3 /Users/someone/other-project/server.py',
            # These hold our path open without being a server. A bare
            # substring test killed the first three; requiring the marker to
            # be an argument still killed them, because the path IS their
            # argument. Only an interpreter running it counts.
            'vim /Users/someone/src/ClaudeCodeBrowser/mcp-server/server.py',
            'tail -f /Users/someone/.claudecodebrowser/mcp-server/server.py',
            'node esbuild.js --watch src mcp-server/server.py',
            'grep -r pattern mcp-server/server.py',
            'python3 -m http.server 8765 # mcp-server/server.py',
            # An interpreter, but running some other script that merely
            # mentions ours as an argument. Only the script itself counts;
            # the two cases above that hold our path stop at the interpreter
            # check and never reach that rule.
            'python3 /opt/other/app.py --ref /x/mcp-server/server.py',
        ):
            with self.subTest(command=command):
                self._with_command(command)
                self.assertFalse(host._is_our_server(1234),
                                 f'must not claim ownership of: {command}')

    def test_unknown_commands_are_not_ours_to_kill(self):
        """If the command line cannot be read, assume it is not ours."""
        self._with_command(None)
        self.assertFalse(host._is_our_server(1234))

    def test_empty_command_is_not_ours_to_kill(self):
        self._with_command('')
        self.assertFalse(host._is_our_server(1234))


class KillTargetSelectionTests(unittest.TestCase):

    def setUp(self):
        self._real_command = host._process_command
        self._real_pids = host._pids_on_port
        self._real_kill = host._terminate
        self.addCleanup(setattr, host, '_process_command', self._real_command)
        self.addCleanup(setattr, host, '_pids_on_port', self._real_pids)
        self.addCleanup(setattr, host, '_terminate', self._real_kill)
        self.killed = []
        host._terminate = lambda pid: self.killed.append(pid)

    def test_only_our_server_processes_are_terminated(self):
        host._pids_on_port = lambda port: [111, 222]
        commands = {
            111: 'python3 /home/someone/.claudecodebrowser/mcp-server/server.py',
            222: '/usr/local/bin/node /home/someone/work/api/index.js',
        }
        host._process_command = lambda pid: commands[pid]

        host.kill_existing_server()

        self.assertEqual(self.killed, [111],
                         'only our own server should have been terminated')

    def test_foreign_process_alone_kills_nothing(self):
        host._pids_on_port = lambda port: [222]
        host._process_command = lambda pid: 'docker-proxy -container-port 8765'

        result = host.kill_existing_server()

        self.assertEqual(self.killed, [])
        self.assertFalse(result, 'the port was not freed, so it must report failure')

    def test_no_listener_kills_nothing(self):
        host._pids_on_port = lambda port: []
        host._process_command = lambda pid: None

        host.kill_existing_server()

        self.assertEqual(self.killed, [])


class FakeResponse:
    """The slice of an HTTP response that the identity probe reads."""

    def __init__(self, body):
        self.status = 200
        self._body = body

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


TOOL_LIST = json.dumps({'tools': [{'name': 'browser_screenshot'},
                                  {'name': 'browser_click'}]}).encode('utf-8')


class ServerIdentityTests(unittest.TestCase):
    """Whatever passes this probe is sent every browser command, typed
    credentials included, and whatever it hands back from /browser/poll runs
    in the browser. A process that got to port 8765 first must not pass.

    Every listener here is a fake urlopen: nothing touches the network."""

    TOKEN = 'a' * 64

    def setUp(self):
        token = mock.patch.object(host, '_read_api_token',
                                  return_value=self.TOKEN)
        token.start()
        self.addCleanup(token.stop)
        self.keys_sent = []

    def _listen(self, answer):
        """Install a listener: answer(api_key) returns a response or raises."""
        def fake_urlopen(request, timeout=None):
            api_key = request.get_header('X-api-key')
            self.keys_sent.append(api_key)
            return answer(api_key)

        patcher = mock.patch('urllib.request.urlopen', fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _forbidden():
        return urllib.error.HTTPError(host.MCP_SERVER_URL + '/mcp/tools', 403,
                                      'Forbidden', {}, None)

    def test_our_server_proves_itself(self):
        def ours(api_key):
            if api_key == self.TOKEN:
                return FakeResponse(TOOL_LIST)
            raise self._forbidden()

        self._listen(ours)
        self.assertTrue(host._server_proves_identity())
        self.assertEqual(self.keys_sent, [self.TOKEN, '0' * 64],
                         'both probes must have been made')

    def test_a_listener_that_accepts_any_key_is_foreign(self):
        """Echoing our tool list is easy; enforcing a token it cannot have
        read is not. A 200 for a wrong key means nothing is checked."""
        self._listen(lambda api_key: FakeResponse(TOOL_LIST))
        self.assertFalse(host._server_proves_identity())

    def test_a_listener_that_refuses_our_token_is_foreign(self):
        def squatter(api_key):
            raise self._forbidden()

        self._listen(squatter)
        self.assertFalse(host._server_proves_identity())

    def test_a_listener_without_our_tool_list_is_foreign(self):
        """200 for our token and 403 for the wrong one, but not our server's
        answer: some other service that happens to check an X-API-Key."""
        def other_service(api_key):
            if api_key == self.TOKEN:
                return FakeResponse(b'{"ok": true}')
            raise self._forbidden()

        self._listen(other_service)
        self.assertFalse(host._server_proves_identity())

    def test_an_unreachable_listener_is_not_ours(self):
        for error in (urllib.error.URLError(ConnectionRefusedError()),
                      TimeoutError('timed out')):
            with self.subTest(error=error):
                def unreachable(api_key, error=error):
                    raise error

                self._listen(unreachable)
                self.assertFalse(host._server_proves_identity())

    def test_without_a_token_nothing_is_asked_or_trusted(self):
        """No token file means no server of ours has run yet."""
        host._read_api_token.return_value = None
        self._listen(lambda api_key: FakeResponse(TOOL_LIST))
        self.assertFalse(host._server_proves_identity())
        self.assertEqual(self.keys_sent, [])


class DescribeMessageTests(unittest.TestCase):
    """Every message is logged both ways through describe_message, into a
    file under ~/.claudecodebrowser. A command carries typed text and a
    response carries page text or a screenshot, so the summary may say what
    a message is and how big, never what it holds."""

    SECRET = 'hunter2-SECRET'

    def assertNoSecret(self, summary):
        self.assertNotIn(self.SECRET, summary)
        self.assertNotIn('hunter2', summary)

    def test_typed_text_is_not_logged(self):
        summary = host.describe_message({
            'action': 'type', 'requestId': 'r1', 'tabId': 7,
            'data': {'text': self.SECRET, 'selector': '#password'}})
        self.assertNoSecret(summary)
        self.assertNotIn('#password', summary)
        # Still enough to follow a session in the log.
        self.assertIn("action='type'", summary)
        self.assertIn("requestId='r1'", summary)
        self.assertIn('tabId=7', summary)
        self.assertIn("'text'", summary)
        self.assertIn("'selector'", summary)

    def test_a_response_data_dict_is_logged_as_a_count(self):
        """A command's data keys are our own argument names. A response's
        come from the page - a script returning a copy of localStorage makes
        every key a stored name, and some of those are secrets."""
        summary = host.describe_message({
            'requestId': 'r1', 'success': True,
            'data': {self.SECRET: 'x', 'session_token_name': 'y'}})
        self.assertNoSecret(summary)
        self.assertNotIn('session_token_name', summary)
        self.assertIn('data_keys=2', summary)

    def test_page_text_is_logged_as_a_length(self):
        body = f'<page body with {self.SECRET}>'
        summary = host.describe_message(
            {'requestId': 'r1', 'success': True, 'text': body})
        self.assertNoSecret(summary)
        self.assertIn(f'text_len={len(body)}', summary)
        self.assertIn('success=True', summary)

    def test_a_screenshot_is_logged_as_a_length(self):
        image = 'data:image/png;base64,' + 'aHVudGVyMg' * 50 + self.SECRET
        summary = host.describe_message(
            {'requestId': 'r1', 'success': True, 'data': image})
        self.assertNoSecret(summary)
        self.assertNotIn('base64', summary)
        self.assertIn(f'data_len={len(image)}', summary)

    def test_string_payload_fields_are_logged_as_lengths(self):
        for key in ('result', 'logs', 'elements'):
            with self.subTest(key=key):
                summary = host.describe_message({key: self.SECRET})
                self.assertNoSecret(summary)
                self.assertIn(f'{key}_len={len(self.SECRET)}', summary)

    def test_structured_payloads_are_not_logged(self):
        summary = host.describe_message({
            'requestId': 'r1', 'success': True,
            'data': {'value': {'nested': [self.SECRET]}},
            'result': {'value': self.SECRET},
            'logs': [{'message': self.SECRET}],
            'elements': [{'text': self.SECRET}],
        })
        self.assertNoSecret(summary)

    def test_an_error_is_flagged_not_quoted(self):
        """An error message can echo what was being typed or read."""
        summary = host.describe_message({
            'requestId': 'r1', 'success': False,
            'error': f'could not type {self.SECRET} into #password'})
        self.assertNoSecret(summary)
        self.assertIn('error=yes', summary)
        self.assertIn('success=False', summary)

    def test_a_non_object_is_named_by_type_only(self):
        self.assertEqual(host.describe_message([self.SECRET]), '<list>')
        self.assertEqual(host.describe_message(self.SECRET), '<str>')

    def test_an_empty_message_still_says_something(self):
        self.assertEqual(host.describe_message({}), '<no action>')


class ScreenshotWriteTests(unittest.TestCase):
    """The host does not write screenshots at all.

    It used to write a .png for ANY successful response carrying a `data`
    field. The junk-payload half of that was fixed, but the half that
    mattered stayed: a real screenshot response IS a data:image/ string, so
    every screenshot was written twice - once by server.py's _save_screenshot
    under the caller's filename, with O_NOFOLLOW and mode 0600, and once here
    as screenshot_<epoch>.png through a plain open() at umask permissions,
    following symlinks, and consuming the 500-file retention cap at double
    rate. A response carries no action either, so getValue on an input
    holding an inline image was written out as a .png too."""

    def setUp(self):
        self.saved = []
        self._real = host.handle_local_command
        host.handle_local_command = self._capture
        self.addCleanup(setattr, host, 'handle_local_command', self._real)
        # Without the cleanup this left the module's real function replaced
        # for the rest of the process.
        self.addCleanup(setattr, host, 'forward_response_to_server',
                        host.forward_response_to_server)
        self.forwarded = []
        host.forward_response_to_server = self.forwarded.append

    def _capture(self, message):
        if message.get('action') == 'saveScreenshot':
            self.saved.append(message)
            return {'success': True, 'filepath': '/tmp/shot.png'}
        return self._real(message)

    def _process(self, message):
        return host.process_message(message)

    def test_an_image_data_url_is_not_written_a_second_time(self):
        self._process({'requestId': 1, 'success': True,
                       'data': 'data:image/png;base64,AAAA'})
        self.assertEqual(self.saved, [],
                         'server.py already saved this screenshot')

    def test_a_non_image_payload_is_not_written_as_a_png(self):
        self._process({'requestId': 1, 'success': True,
                       'data': 'just some page text, not an image'})
        self.assertEqual(self.saved, [],
                         'a text payload must not land on disk as a .png')

    def test_structured_data_is_not_written_as_a_png(self):
        self._process({'requestId': 1, 'success': True,
                       'data': {'elements': [1, 2, 3]}})
        self.assertEqual(self.saved, [])

    def test_a_failed_response_is_not_written(self):
        self._process({'requestId': 1, 'success': False,
                       'data': 'data:image/png;base64,AAAA'})
        self.assertEqual(self.saved, [])

    def test_the_response_reaches_the_server_and_is_not_echoed_back(self):
        """The server is the one waiting for a result. The whole result used
        to be returned here as well, and sent straight back to the extension,
        which ignored it - so a 3 MB screenshot tripped the 1 MB limit on its
        way back for nothing."""
        response = {'requestId': 1, 'success': True,
                    'data': 'data:image/png;base64,AAAA'}
        self.assertIsNone(self._process(response))
        self.assertEqual(self.forwarded, [response])


class SendDirectionTests(unittest.TestCase):
    """Firefox caps a message from the host to the extension at 1 MB, and
    that is the only direction with a cap that matters. What may cross it,
    and who hears about it when something cannot."""

    def setUp(self):
        self.sent, self.forwarded = [], []
        for name, fake in (('send_message', self.sent.append),
                           ('forward_response_to_server', self.forwarded.append),
                           ('check_mcp_server', lambda: True)):
            self.addCleanup(setattr, host, name, getattr(host, name))
            setattr(host, name, fake)

    def test_a_large_result_goes_to_the_server_only(self):
        result = {'requestId': 'r1', 'success': True,
                  'data': 'x' * (3 * host.MAX_OUTGOING_MESSAGE_BYTES)}
        host.handle_incoming(result)
        self.assertEqual(self.forwarded, [result])
        self.assertEqual(self.sent, [], 'nothing goes back to the extension')

    def test_a_request_from_the_extension_still_gets_its_reply(self):
        host.handle_incoming({'action': 'ping', 'requestId': 'r2'})
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0]['pong'])
        self.assertEqual(self.sent[0]['requestId'], 'r2')

    def test_an_oversized_command_fails_at_the_server_at_once(self):
        """A failure sent in its place went to the extension, which ignores
        a reply nobody asked for, so the caller waited out the full timeout
        and got no reason."""
        command = {'action': 'type', 'requestId': 'r3', 'tabId': 1,
                   'data': {'text': 'x' * (2 * host.MAX_OUTGOING_MESSAGE_BYTES)}}
        host.deliver_command(command)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.forwarded), 1)
        failure = self.forwarded[0]
        self.assertEqual(failure['requestId'], 'r3')
        self.assertIs(failure['success'], False)
        self.assertIn('1048576', failure['error'])
        self.assertNotIn('xxxx', json.dumps(failure))

    def test_an_ordinary_command_is_delivered(self):
        command = {'action': 'click', 'requestId': 'r4', 'data': {}}
        host.deliver_command(command)
        self.assertEqual(self.sent, [command])
        self.assertEqual(self.forwarded, [])


class ConcurrentSendTests(unittest.TestCase):
    """The polling thread and the main thread both write to the extension.
    The length prefix and the body went out as two writes with no lock, so
    two senders could interleave them, Firefox would read a corrupt frame,
    and it tears the port down."""

    def test_two_threads_never_interleave_a_frame(self):
        class SlowStream(io.BytesIO):
            def write(self, data):
                # Yield mid-message so the other sender gets its chance.
                time.sleep(0.0002)
                return super().write(data)

            def flush(self):
                pass

        stream = SlowStream()

        def sender(tag):
            for i in range(60):
                host.send_message({'tag': tag, 'i': i}, stream)

        threads = [threading.Thread(target=sender, args=(t,)) for t in 'ab']
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stream.seek(0)
        seen = []
        for _ in range(120):
            seen.append(host.read_message(stream))
        self.assertEqual(sorted((m['tag'], m['i']) for m in seen),
                         sorted((t, i) for t in 'ab' for i in range(60)))


class ExplicitScreenshotSaveTests(unittest.TestCase):
    """The extension can still ask for a save by name. That write must be as
    careful as server.py's: 0600, and no following a symlink someone
    pre-created under a predictable name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        previous = os.environ.get('CLAUDE_BROWSER_SCREENSHOTS_DIR')
        os.environ['CLAUDE_BROWSER_SCREENSHOTS_DIR'] = self.tmp.name
        self.addCleanup(self._restore_env, previous)

    def _restore_env(self, previous):
        if previous is None:
            os.environ.pop('CLAUDE_BROWSER_SCREENSHOTS_DIR', None)
        else:
            os.environ['CLAUDE_BROWSER_SCREENSHOTS_DIR'] = previous

    def _save(self, filename, data='data:image/png;base64,AAAA'):
        return host.handle_local_command({'action': 'saveScreenshot',
                                          'data': data,
                                          'filename': filename})

    def test_the_file_is_private_to_this_user(self):
        result = self._save('shot.png')
        self.assertTrue(result['success'], result)
        mode = os.stat(result['filepath']).st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_a_symlink_at_the_target_name_is_not_followed(self):
        victim = Path(self.tmp.name) / 'victim.txt'
        victim.write_text('do not truncate me')
        os.symlink(victim, Path(self.tmp.name) / 'shot.png')

        result = self._save('shot.png')

        self.assertFalse(result['success'])
        self.assertEqual(victim.read_text(), 'do not truncate me')

    def test_a_directory_only_filename_falls_back_to_a_generated_name(self):
        for hostile in ('..', '.', '../'):
            with self.subTest(filename=hostile):
                result = self._save(hostile)
                self.assertTrue(result['success'], result)
                written = Path(result['filepath'])
                self.assertEqual(written.parent.resolve(),
                                 Path(self.tmp.name).resolve())
                self.assertTrue(written.name.startswith('screenshot_'))

    def test_directories_are_stripped_from_the_filename(self):
        result = self._save('../../../../tmp/evil.png')
        self.assertTrue(result['success'], result)
        self.assertEqual(Path(result['filepath']).parent.resolve(),
                         Path(self.tmp.name).resolve())


class FakeChild:
    """The slice of subprocess.Popen that the reaper uses."""

    def __init__(self, returncode=None):
        self._returncode = returncode
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self._returncode

    def wait(self, timeout=None):
        raise AssertionError(
            'poll() already reaped this child; wait() was dead code')


class ChildReapingTests(unittest.TestCase):
    """Each crash-and-restart cycle left a zombie for the life of the host."""

    def setUp(self):
        self.addCleanup(host._spawned_children.clear)
        host._spawned_children.clear()

    def test_finished_children_are_dropped_and_running_ones_kept(self):
        finished, running = FakeChild(returncode=0), FakeChild()
        host._spawned_children[:] = [finished, running]

        host._reap_finished_children()

        # poll() IS the reap: it calls waitpid(WNOHANG) and sets returncode.
        # The old code followed it with wait(timeout=0) on an already-reaped
        # process, and the old test asserted that dead call happened.
        self.assertEqual(finished.polls, 1)
        self.assertEqual(host._spawned_children, [running],
                         'a running child must be kept')

    def test_a_child_that_cannot_be_polled_is_kept(self):
        class Unpollable:
            def poll(self):
                raise OSError('no such process')

        child = Unpollable()
        host._spawned_children[:] = [child]
        host._reap_finished_children()
        self.assertEqual(host._spawned_children, [child],
                         'a child we cannot ask about must not be forgotten')

    def test_reaping_does_not_drop_a_concurrently_added_child(self):
        """The list is rebuilt from the health-monitor thread while the main
        thread appends to it. Rebuilding it unlocked discarded the child that
        had just been added - the one it was meant to reap next."""
        late = FakeChild()
        appended = threading.Event()

        class Slow:
            """Appends from another thread while the reaper holds the lock."""

            def poll(self):
                def append():
                    with host._spawned_children_lock:
                        host._spawned_children.append(late)
                    appended.set()

                threading.Thread(target=append).start()
                # Long enough that an unlocked reaper would have rebuilt the
                # list from its stale copy by now.
                time.sleep(0.2)
                return 0

        host._spawned_children[:] = [Slow()]
        host._reap_finished_children()

        self.assertTrue(appended.wait(1), 'the appending thread never ran')
        self.assertEqual(host._spawned_children, [late])

    # Regression test. Was: claudecodebrowser_host.py:580 - the list is
    # guarded by a plain threading.Lock, and the comment above it says the
    # list is touched "from the signal handler (through shutdown)". Python
    # runs signal handlers on the main thread, so a SIGTERM landing while
    # that thread is already inside _reap_finished_children re-enters the
    # lock and self-deadlocks. The host then ignores SIGTERM and SIGINT and
    # only dies to SIGKILL - and Firefox SIGTERMs native hosts when the port
    # closes, so they pile up.
    def test_a_signal_arriving_mid_reap_can_still_shut_down(self):
        self.addCleanup(setattr, host, 'health_monitor_running',
                        host.health_monitor_running)
        host.health_monitor_running = True
        reentered = []

        class SignalDuringPoll:
            """Stands in for the handler, which runs on this same thread."""

            def __init__(self):
                self.signalled = False

            def poll(self):
                if not self.signalled:
                    self.signalled = True
                    # The lock is probed with a timeout before shutdown() is
                    # called for real: a non-reentrant lock would block here
                    # for the life of the process, hanging the whole test run
                    # instead of failing this one, and would stay held for
                    # every test after it.
                    taken = host._spawned_children_lock.acquire(timeout=1)
                    reentered.append(taken)
                    if taken:
                        host._spawned_children_lock.release()
                        host.shutdown()
                return 0

        host._spawned_children[:] = [SignalDuringPoll()]
        host._reap_finished_children()

        self.assertEqual(reentered, [True],
                         'the reaper lock must be reentrant, or a signal '
                         'landing mid-reap deadlocks the host')
        self.assertFalse(host.health_monitor_running,
                         'the handler has to get through shutdown()')
        self.assertEqual(host._spawned_children, [])

    def test_the_health_monitor_reaps_without_restarting(self):
        """Reaping used to happen only inside the next start_mcp_server(),
        which does not run during backoff and never runs again after
        MAX_RESTART_ATTEMPTS - so the last dead child stayed a zombie."""
        self.addCleanup(setattr, host, 'check_mcp_server',
                        host.check_mcp_server)
        self.addCleanup(setattr, host, 'start_mcp_server',
                        host.start_mcp_server)
        self.addCleanup(setattr, host, 'HEALTH_CHECK_INTERVAL',
                        host.HEALTH_CHECK_INTERVAL)
        self.addCleanup(setattr, host, 'health_monitor_running',
                        host.health_monitor_running)

        host.HEALTH_CHECK_INTERVAL = 0.01
        host.check_mcp_server = lambda: True        # healthy: no restart
        host.start_mcp_server = lambda: self.fail('must not restart')

        finished = FakeChild(returncode=1)
        host._spawned_children[:] = [finished]
        host.health_monitor_running = True

        monitor = threading.Thread(target=host.health_monitor_thread,
                                   daemon=True)
        monitor.start()
        deadline = time.time() + 2
        while host._spawned_children and time.time() < deadline:
            time.sleep(0.01)
        host.health_monitor_running = False
        monitor.join(timeout=2)

        self.assertEqual(host._spawned_children, [],
                         'the health monitor must reap a dead child')


class RecordingStream(io.BytesIO):
    """Records how many bytes each read asked for. A refused frame raises
    FramingError whether it was refused up front or ran out of bytes, so
    the requests are the only way to tell the two apart."""

    def __init__(self, data):
        super().__init__(data)
        self.requested = []

    def read(self, count=-1):
        self.requested.append(count)
        return super().read(count)


def framed(payload: bytes) -> bytes:
    """One native-messaging frame: native-order length prefix, then bytes."""
    return struct.pack(host.NATIVE_LENGTH_FORMAT, len(payload)) + payload


class FramingTests(unittest.TestCase):
    """Nothing exercised the native messaging framing, and it was the one
    place where a corrupt length prefix could take the host down."""

    def test_the_length_prefix_is_native_byte_order(self):
        """Firefox documents the prefix as native byte order, so '<I' would
        be wrong on a big-endian host rather than more portable.

        Asserted through the framing. This used to compare the format
        literal with the same literal, which cannot fail whatever the host
        does with it.
        """
        payload = b'{"action": "ping"}'
        self.assertEqual(
            host.read_message(io.BytesIO(
                struct.pack('=I', len(payload)) + payload)),
            {'action': 'ping'})
        # The same length written the other way round reads as hundreds of
        # megabytes, so it is refused rather than silently mis-framed. True
        # on either endianness: each order looks huge read as the other.
        # Checked through the reads: a short body raises FramingError too, so
        # expecting the error alone stayed green with the size bound removed.
        other_order = '>I' if sys.byteorder == 'little' else '<I'
        stream = RecordingStream(struct.pack(other_order, len(payload)) + payload)
        with self.assertRaises(host.FramingError):
            host.read_message(stream)
        self.assertEqual(stream.requested, [4])

    def test_a_valid_frame_round_trips(self):
        message = {'action': 'ping', 'requestId': 'abc'}
        stream = io.BytesIO(framed(json.dumps(message).encode('utf-8')))
        self.assertEqual(host.read_message(stream), message)

    def test_two_frames_are_read_in_order(self):
        stream = io.BytesIO(framed(b'{"action": "one"}')
                            + framed(b'{"action": "two"}'))
        self.assertEqual(host.read_message(stream), {'action': 'one'})
        self.assertEqual(host.read_message(stream), {'action': 'two'})
        self.assertIsNone(host.read_message(stream))

    def test_end_of_stream_is_none_not_an_error(self):
        """None means Firefox closed the pipe, and only that: it used to mean
        that OR a bad frame OR a decode error, so the caller could not tell
        a normal disconnect from a corrupt stream."""
        self.assertIsNone(host.read_message(io.BytesIO(b'')))

    def test_a_truncated_length_prefix_is_a_framing_error(self):
        with self.assertRaises(host.FramingError):
            host.read_message(io.BytesIO(b'\x01\x00'))

    def test_a_length_prefix_split_across_reads_still_works(self):
        class Dribble:
            """A pipe that hands back one byte at a time, as pipes may."""

            def __init__(self, data):
                self._data = data

            def read(self, count):
                chunk = self._data[:1]
                self._data = self._data[1:]
                return chunk

        message = {'action': 'ping'}
        payload = framed(json.dumps(message).encode('utf-8'))
        self.assertEqual(host.read_message(Dribble(payload)), message)

    def test_a_short_body_is_a_framing_error_not_a_truncated_message(self):
        """A single read() could return less than asked for, and the old code
        used whatever came back - silently truncating the message."""
        with self.assertRaises(host.FramingError):
            host.read_message(io.BytesIO(framed(b'{"action": "ping"}')[:-5]))

    def test_an_oversized_length_prefix_is_refused_before_reading(self):
        """A corrupt prefix claiming gigabytes must not be allocated.

        Asserted through what was asked of the stream. Expecting FramingError
        alone could not fail: with only the prefix to read, a short body
        raises FramingError too, so dropping the size bound kept it green.
        """
        stream = RecordingStream(struct.pack(
            host.NATIVE_LENGTH_FORMAT, host.MAX_INCOMING_MESSAGE_BYTES + 1))
        with self.assertRaises(host.FramingError):
            host.read_message(stream)
        self.assertEqual(stream.requested, [4],
                         'only the length prefix may be read before the '
                         'claimed size is checked')

    def test_a_two_megabyte_screenshot_frame_is_accepted(self):
        """Inbound is not capped at Firefox's 1 MB outbound limit: one
        screenshot data URL is routinely larger than that."""
        payload = json.dumps({'requestId': 'x',
                              'data': 'data:image/png;base64,'
                                      + 'A' * 2 * 1024 * 1024}).encode('utf-8')
        self.assertGreater(len(payload), 1024 * 1024)
        message = host.read_message(io.BytesIO(framed(payload)))
        self.assertEqual(message['requestId'], 'x')

    def test_a_zero_length_frame_is_a_framing_error(self):
        with self.assertRaises(host.FramingError):
            host.read_message(io.BytesIO(struct.pack(
                host.NATIVE_LENGTH_FORMAT, 0)))

    def test_undecodable_bytes_are_one_bad_message_not_a_dead_stream(self):
        """The frame was read in full, so the stream is still aligned: the
        caller drops this message and reads the next one."""
        stream = io.BytesIO(framed(b'not json at all')
                            + framed(b'{"action": "ping"}'))
        with self.assertRaises(host.MessageDecodeError):
            host.read_message(stream)
        self.assertEqual(host.read_message(stream), {'action': 'ping'})

    def test_invalid_utf8_is_also_one_bad_message(self):
        with self.assertRaises(host.MessageDecodeError):
            host.read_message(io.BytesIO(framed(b'\xff\xfe')))

    # Regression test. Was: claudecodebrowser_host.py:234 - read_message
    # returned whatever json.loads produced without checking it is an
    # object. A frame of `5`, `"hi"` or `[1,2,3]` is valid JSON, so it came
    # straight back and reached message.get() in main() as an
    # AttributeError, which the top-level handler logs as "Fatal error"
    # before sys.exit(1): one malformed frame shut the host down.
    def test_a_frame_that_is_not_an_object_is_one_bad_message(self):
        for payload in (b'5', b'"hi"', b'[1,2,3]', b'true', b'3.5'):
            with self.subTest(payload=payload):
                stream = io.BytesIO(framed(payload)
                                    + framed(b'{"action": "ping"}'))
                with self.assertRaises(host.MessageDecodeError):
                    host.read_message(stream)
                self.assertEqual(host.read_message(stream),
                                 {'action': 'ping'},
                                 'the frame was read in full, so the stream '
                                 'is still aligned')

    def test_a_null_frame_is_not_mistaken_for_end_of_stream(self):
        """`null` is valid JSON and came back as None - indistinguishable
        from Firefox closing the pipe, so a frame of `null` made main() log
        a clean disconnect and exit. That defeated the three-outcome
        distinction the rest of this class pins."""
        stream = io.BytesIO(framed(b'null') + framed(b'{"action": "ping"}'))
        with self.assertRaises(host.MessageDecodeError):
            host.read_message(stream)
        self.assertEqual(host.read_message(stream), {'action': 'ping'})


class SendMessageTests(unittest.TestCase):

    def test_a_message_is_framed_so_read_message_can_read_it_back(self):
        out = io.BytesIO()
        host.send_message({'success': True, 'pong': True}, out)
        self.assertEqual(host.read_message(io.BytesIO(out.getvalue())),
                         {'success': True, 'pong': True})

    def test_an_oversized_message_is_replaced_with_a_failure(self):
        """Firefox drops a message over 1 MB and tears the port down, so
        sending it anyway loses the message and the connection, and whoever
        was waiting on the requestId waits out its timeout."""
        out = io.BytesIO()
        host.send_message({'requestId': 'abc', 'success': True,
                           'data': 'x' * (host.MAX_OUTGOING_MESSAGE_BYTES + 1)},
                          out)

        self.assertLessEqual(len(out.getvalue()),
                             host.MAX_OUTGOING_MESSAGE_BYTES)
        sent = host.read_message(io.BytesIO(out.getvalue()))
        self.assertFalse(sent['success'])
        self.assertEqual(sent['requestId'], 'abc',
                         'the waiter must be able to match the failure')
        self.assertIn('limit', sent['error'])
        self.assertNotIn('xxxx', sent['error'],
                         'the dropped payload must not be echoed back')

    # Regression test. Was: claudecodebrowser_host.py:246-266 - the
    # replacement copies requestId verbatim and is never re-measured, so an
    # oversized requestId produced an oversized replacement. Firefox drops
    # that one too AND tears the port down, which is the exact outcome the
    # replacement exists to prevent. The test above asserts the bound but
    # its fixture uses requestId 'abc', so the bound was never exercised.
    def test_an_oversized_request_id_does_not_make_an_oversized_failure(self):
        out = io.BytesIO()
        huge_id = 'r' * (host.MAX_OUTGOING_MESSAGE_BYTES + 1)
        host.send_message({'requestId': huge_id, 'success': True,
                           'data': 'small'}, out)

        self.assertLessEqual(len(out.getvalue()),
                             host.MAX_OUTGOING_MESSAGE_BYTES)
        sent = host.read_message(io.BytesIO(out.getvalue()))
        self.assertFalse(sent['success'])
        self.assertIn('limit', sent['error'])
        self.assertNotIn('rrrr', json.dumps(sent),
                         'an id that does not fit must be dropped, not '
                         'echoed back')

    def test_a_non_dict_message_is_still_framed_within_the_limit(self):
        """send_message is also reachable with a non-dict (a list response
        forwarded from the MCP server), and the oversize path reads
        requestId off it."""
        out = io.BytesIO()
        host.send_message(['x' * (host.MAX_OUTGOING_MESSAGE_BYTES + 1)], out)
        self.assertLessEqual(len(out.getvalue()),
                             host.MAX_OUTGOING_MESSAGE_BYTES)
        self.assertFalse(host.read_message(
            io.BytesIO(out.getvalue()))['success'])


if __name__ == '__main__':
    unittest.main()

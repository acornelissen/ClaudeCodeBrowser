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
from pathlib import Path

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

    def test_the_response_still_reaches_the_server(self):
        response = {'requestId': 1, 'success': True,
                    'data': 'data:image/png;base64,AAAA'}
        self.assertIs(self._process(response), response)
        self.assertEqual(self.forwarded, [response])


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
        other_order = '>I' if sys.byteorder == 'little' else '<I'
        with self.assertRaises(host.FramingError):
            host.read_message(io.BytesIO(
                struct.pack(other_order, len(payload)) + payload))

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
        """A corrupt prefix claiming gigabytes must not be allocated."""
        huge = struct.pack(host.NATIVE_LENGTH_FORMAT,
                           host.MAX_INCOMING_MESSAGE_BYTES + 1)
        with self.assertRaises(host.FramingError):
            host.read_message(io.BytesIO(huge))

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

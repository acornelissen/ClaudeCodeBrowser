#!/usr/bin/env python3
"""
Native host tests, covering which processes it is allowed to kill.

Firefox launches the native host automatically, and the host clears whatever
holds the MCP port before starting its own server. It must only ever kill its
own server: an unrelated service on port 8765 is not ours to terminate.

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
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


if __name__ == '__main__':
    unittest.main()

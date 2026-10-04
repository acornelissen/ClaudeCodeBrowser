#!/usr/bin/env python3
"""
MCP server configuration and credential-guard plumbing tests.

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Point HOME at a throwaway directory before importing the server: it creates
# its API token, config and screenshot directories under the home directory at
# import time, and a test run must not touch the real installation.
_TMP_HOME = tempfile.mkdtemp(prefix='ccb-test-home-')
os.environ['HOME'] = _TMP_HOME
os.environ.pop('CLAUDE_BROWSER_SCREENSHOTS_DIR', None)
os.environ.pop('CLAUDE_BROWSER_SAFETY_CONFIG', None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'mcp-server'))

import safety  # noqa: E402
import server  # noqa: E402


class ScreenshotLocationTests(unittest.TestCase):
    """Screenshots hold whatever is on screen, so they belong in the user's
    own directory, not a world-readable shared /tmp."""

    def test_default_directory_is_under_the_home_directory(self):
        self.assertEqual(
            server.SCREENSHOTS_DIR,
            Path(_TMP_HOME) / '.claudecodebrowser' / 'screenshots')

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
    )

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

    def test_page_info_masks_password_values(self):
        """getPageInfo's own masking is the precedent the read guard follows."""
        content_js = (Path(__file__).resolve().parent.parent
                      / 'extension' / 'content.js').read_text()
        self.assertIn("el.type === 'password' ? '***'", content_js)


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

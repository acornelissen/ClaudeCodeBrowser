#!/usr/bin/env python3
"""
Safety guard tests.

The guard is the security core of the project and was the least covered part
of it: read-only mode and the scheme allowlist were tested, and nothing else.
These cover the enforcement layers an audit found holes in.

Run: python3 -m unittest discover -s tests -t . -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path


from tests import TEST_HOME  # noqa: F401  (redirects HOME on import)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'mcp-server'))

import safety  # noqa: E402


def guard(**config):
    g = safety.SafetyGuard()
    g.config.update(config)
    g._compile_patterns()
    return g


class ConfirmTokenBindingTests(unittest.TestCase):
    """The denial says "repeat the exact same call". That has to be true: a
    token bound only to the tool name authorised any call of that tool, on any
    URL, for two minutes."""

    def _token_for(self, g, tool, arguments):
        denial = g.check(tool, dict(arguments))
        self.assertIsNotNone(denial, 'expected a confirmation request')
        self.assertEqual(denial['safety_decision'], 'confirmation_required')
        return denial['confirm_token']

    def test_the_same_call_is_allowed_through(self):
        g = guard()
        g.note_url({'url': 'https://www.chase.com/transfer'})
        args = {'selector': '#help'}
        token = self._token_for(g, 'browser_click', args)
        self.assertIsNone(g.check('browser_click', {**args, 'confirm_token': token}))

    def test_a_token_does_not_authorise_different_arguments(self):
        g = guard()
        g.note_url({'url': 'https://www.chase.com/transfer'})
        token = self._token_for(g, 'browser_click', {'selector': '#help'})
        denial = g.check('browser_click',
                         {'selector': '#transfer-submit', 'confirm_token': token})
        self.assertIsNotNone(denial, 'a token must not travel to another selector')
        self.assertEqual(denial['safety_decision'], 'confirmation_required')

    def test_a_token_does_not_authorise_a_different_url(self):
        g = guard()
        g.note_url({'url': 'https://www.chase.com/help'})
        token = self._token_for(g, 'browser_click', {'selector': '#ok'})
        g.note_url({'url': 'https://paypal.com/myaccount/transfer'})
        denial = g.check('browser_click', {'selector': '#ok', 'confirm_token': token})
        self.assertIsNotNone(denial, 'a token must not travel to another site')

    def test_a_token_does_not_authorise_a_different_tool(self):
        g = guard()
        g.note_url({'url': 'https://www.chase.com/x'})
        token = self._token_for(g, 'browser_click', {'selector': '#ok'})
        denial = g.check('browser_type',
                         {'selector': '#ok', 'text': 'x', 'confirm_token': token})
        self.assertIsNotNone(denial)

    def test_a_token_is_single_use(self):
        g = guard()
        g.note_url({'url': 'https://www.chase.com/x'})
        args = {'selector': '#ok'}
        token = self._token_for(g, 'browser_click', args)
        self.assertIsNone(g.check('browser_click', {**args, 'confirm_token': token}))
        self.assertIsNotNone(g.check('browser_click', {**args, 'confirm_token': token}),
                             'a spent token must not work twice')

    def test_a_forged_token_is_refused(self):
        g = guard()
        g.note_url({'url': 'https://www.chase.com/x'})
        self._token_for(g, 'browser_click', {'selector': '#ok'})
        denial = g.check('browser_click', {'selector': '#ok', 'confirm_token': 'deadbeef'})
        self.assertIsNotNone(denial)


class ProtectedPatternTests(unittest.TestCase):

    def test_a_query_string_does_not_switch_the_gov_pattern_off(self):
        """\.gov(/|$) required a slash or end-of-string, so appending ?x=1
        disarmed the guard while loading the same page."""
        g = guard()
        for url in ('https://www.irs.gov', 'https://www.irs.gov/',
                    'https://www.irs.gov?x=1', 'https://www.irs.gov#x',
                    'https://irs.gov:443/'):
            with self.subTest(url=url):
                g.note_url({'url': url})
                denial = g.check('browser_click', {'selector': '#x'})
                self.assertIsNotNone(denial, f'{url} should be protected')

    def test_an_unprotected_site_needs_no_confirmation(self):
        g = guard()
        g.note_url({'url': 'https://example.com/page'})
        self.assertIsNone(g.check('browser_click', {'selector': '#x'}))


class UrlPolicyTests(unittest.TestCase):
    """The blocklist only ever looked at a url argument. No read tool takes
    one, so blocking a domain stopped navigation there and left every read
    tool free on an already-open tab."""

    def test_blocklist_covers_reads_on_the_current_page(self):
        g = guard(blocked_url_patterns=[r'secret\.internal'])
        g.note_url({'url': 'https://secret.internal/dashboard'})
        denial = g.check('browser_get_text', {})
        self.assertIsNotNone(denial, 'reading a blocked page must be refused')
        self.assertEqual(denial['safety_decision'], 'blocked_url')

    def test_blocklist_still_covers_navigation(self):
        g = guard(blocked_url_patterns=[r'secret\.internal'])
        denial = g.check('browser_navigate', {'url': 'https://secret.internal/x'})
        self.assertEqual(denial['safety_decision'], 'blocked_url')

    def test_allowlist_confines_reads_too(self):
        g = guard(allowed_url_patterns=[r'^https://localhost'])
        g.note_url({'url': 'https://evil.example/x'})
        denial = g.check('browser_screenshot', {})
        self.assertIsNotNone(denial)
        self.assertEqual(denial['safety_decision'], 'not_allowlisted')

    def test_an_allowlisted_page_is_readable(self):
        g = guard(allowed_url_patterns=[r'^https://localhost'])
        g.note_url({'url': 'https://localhost:3000/app'})
        self.assertIsNone(g.check('browser_screenshot', {}))


class ToolClassificationTests(unittest.TestCase):

    def test_read_only_blocks_low_risk_acts(self):
        """read_only says it blocks every state-changing action. Scroll,
        hover, highlight and focus_tab change state and were classified as
        observation, so they ran."""
        g = guard(read_only=True)
        for tool in ('browser_scroll', 'browser_hover', 'browser_highlight',
                     'browser_focus_tab'):
            with self.subTest(tool=tool):
                denial = g.check(tool, {})
                self.assertIsNotNone(denial, f'{tool} changes state')
                self.assertEqual(denial['safety_decision'], 'read_only')

    def test_low_risk_acts_do_not_prompt_on_protected_sites(self):
        """Prompting on every scroll trains people to click Approve."""
        g = guard()
        g.note_url({'url': 'https://www.chase.com/x'})
        self.assertIsNone(g.check('browser_scroll', {'direction': 'down'}))

    def test_screenshotting_every_tab_is_not_observation(self):
        """It activates and photographs every tab in every window."""
        g = guard(read_only=True)
        denial = g.check('browser_screenshot_all_tabs', {})
        self.assertIsNotNone(denial)
        self.assertEqual(denial['safety_decision'], 'read_only')

    def test_reading_the_working_page_is_still_observation(self):
        g = guard(read_only=True)
        for tool in ('browser_screenshot', 'browser_get_text',
                     'browser_get_page_info', 'browser_get_value'):
            with self.subTest(tool=tool):
                self.assertIsNone(g.check(tool, {}))


class ScriptToggleTests(unittest.TestCase):

    def test_scripts_can_be_turned_off(self):
        g = guard(allow_script_execution=False)
        denial = g.check('browser_execute_script', {'script': 'x'})
        self.assertEqual(denial['safety_decision'], 'scripts_disabled')

    def test_scripts_are_allowed_by_default(self):
        self.assertIsNone(guard().check('browser_execute_script', {'script': 'x'}))


class RateLimitTests(unittest.TestCase):

    def test_the_cap_is_enforced(self):
        g = guard(max_actions_per_minute=3)
        decisions = [g.check('browser_get_text', {}) for _ in range(5)]
        denied = [d for d in decisions if d is not None]
        self.assertTrue(denied, 'the limit must bite')
        self.assertEqual(denied[0]['safety_decision'], 'rate_limited')

    def test_zero_disables_the_limit(self):
        g = guard(max_actions_per_minute=0)
        for _ in range(50):
            self.assertIsNone(g.check('browser_get_text', {}))


class DisabledGuardTests(unittest.TestCase):

    def test_disabling_policy_does_not_re_enable_dangerous_schemes(self):
        """One config key should not turn file:// and javascript: back on."""
        g = guard(enabled=False)
        for url in ('file:///etc/passwd', 'javascript:alert(1)'):
            with self.subTest(url=url):
                denial = g.check('browser_navigate', {'url': url})
                self.assertIsNotNone(denial)
                self.assertEqual(denial['safety_decision'], 'blocked_scheme')

    def test_disabling_policy_does_allow_ordinary_calls(self):
        g = guard(enabled=False, read_only=True)
        self.assertIsNone(g.check('browser_click', {'selector': '#x'}))


class AuditLogTests(unittest.TestCase):

    def test_sensitive_values_are_not_written(self):
        g = guard()
        g.check('browser_type', {'selector': '#x', 'text': 'hunter2'})
        written = safety._AUDIT_FILE.read_text()
        self.assertNotIn('hunter2', written)
        self.assertIn('browser_type', written)

    def test_the_audit_file_is_not_world_readable(self):
        g = guard()
        g.check('browser_get_text', {})
        mode = safety._AUDIT_FILE.stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0,
                         f'audit log is a browsing history: {oct(mode)}')


if __name__ == '__main__':
    unittest.main()

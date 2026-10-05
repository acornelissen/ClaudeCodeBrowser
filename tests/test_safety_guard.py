#!/usr/bin/env python3
"""
Safety guard tests.

The guard is the security core of the project and was the least covered part
of it: read-only mode and the scheme allowlist were tested, and nothing else.
These cover the enforcement layers an audit found holes in.

Run: python3 -m unittest discover -s tests -t . -v
"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
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
        r"""\.gov(/|$) required a slash or end-of-string, so appending ?x=1
        disarmed the guard while loading the same page."""
        g = guard()
        for url in ('https://www.irs.gov', 'https://www.irs.gov/',
                    'https://www.irs.gov?x=1', 'https://www.irs.gov#x',
                    'https://irs.gov:443/',
                    # Firefox parses tabs.update({url}) the WHATWG way, where
                    # a backslash is a slash, control characters are dropped
                    # and surrounding whitespace is trimmed. Matching the raw
                    # string instead meant one backslash landed on the same
                    # page with the delimiter class unmatched.
                    'https://www.irs.gov\\payments',
                    'https://www.irs.gov\\\\payments',
                    'https://www.irs.gov\t/payments',
                    'https://www.irs.gov\n/payments',
                    '  https://www.irs.gov/payments  ',
                    'https:/\\www.irs.gov/payments'):
            with self.subTest(url=url):
                g.note_url({'url': url})
                denial = g.check('browser_click', {'selector': '#x'})
                self.assertIsNotNone(denial, f'{url} should be protected')

    def test_a_backslash_url_cannot_be_navigated_to_unconfirmed(self):
        """The exploit the delimiter gap bought: browser_navigate to a .gov
        page with no human approval and no confirm token."""
        g = guard()
        denial = g.check('browser_navigate',
                         {'url': 'https://www.irs.gov\\payments'})
        self.assertIsNotNone(denial, 'a backslash must not disarm the guard')
        self.assertEqual(denial['safety_decision'], 'confirmation_required')

    def test_normalising_does_not_protect_a_lookalike_host(self):
        """The host here is evil.com, not irs.gov, and over-matching would
        train people to approve prompts that do not matter."""
        g = guard()
        for url in ('https://www.irs.gov.evil.com/x',
                    'https://www.irs.gov@evil.com/x'):
            with self.subTest(url=url):
                g.note_url({'url': url})
                self.assertIsNone(g.check('browser_click', {'selector': '#x'}))

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

    def test_a_name_that_merely_starts_with_an_allowed_one_is_refused(self):
        """The allowlist is the strictest setting on offer, and re.search let
        any attacker-registrable name that extended an allowed one through -
        which also freed every read tool on that page."""
        g = guard(allowed_url_patterns=[r'^https://localhost'])
        for url in ('https://localhost.evil.com/x', 'https://localhostile.io/',
                    'https://localhost@evil.com/', 'https://localhost-evil.io/'):
            with self.subTest(url=url):
                g.note_url({'url': url})
                denial = g.check('browser_screenshot', {})
                self.assertIsNotNone(denial, f'{url} is not localhost')
                self.assertEqual(denial['safety_decision'], 'not_allowlisted')

    def test_an_allowlist_pattern_may_name_a_host_on_its_own(self):
        g = guard(allowed_url_patterns=[r'localhost'])
        g.note_url({'url': 'http://localhost:8080/x'})
        self.assertIsNone(g.check('browser_screenshot', {}))
        g.note_url({'url': 'https://localhost.evil.com/x'})
        self.assertIsNotNone(g.check('browser_screenshot', {}))

    def test_an_allowlisted_navigation_is_still_allowed(self):
        g = guard(allowed_url_patterns=[r'^https://localhost'])
        self.assertIsNone(g.check('browser_navigate',
                                  {'url': 'https://localhost:3000/app'}))
        denial = g.check('browser_navigate',
                         {'url': 'https://localhost.evil.com/app'})
        self.assertEqual(denial['safety_decision'], 'not_allowlisted')

    def test_a_trusted_pattern_is_confined_the_same_way(self):
        """trusted_url_patterns grants the same thing in the other direction:
        it exempts a site from confirmation, so a prefix match there buys an
        attacker's subdomain a pass on protected-site checks."""
        g = guard(unlisted_domains='confirm',
                  trusted_url_patterns=[r'^https://localhost'])
        g.note_url({'url': 'https://localhost.evil.com/x'})
        denial = g.check('browser_click', {'selector': '#x'})
        self.assertIsNotNone(denial, 'only localhost itself is trusted')
        self.assertEqual(denial['safety_decision'], 'confirmation_required')

    def test_a_pattern_with_an_inline_flag_still_restricts(self):
        """The delimiter test used to be a (?=...) wrapped round the pattern,
        which moved a leading "(?i)" off position 0. Python refuses a global
        flag anywhere else, so every such pattern was dropped - and an
        allowlist with nothing left in it was treated as no allowlist."""
        g = guard(allowed_url_patterns=['(?i)^https://localhost'])
        self.assertEqual(g.status()['pattern_errors'], [],
                         'this pattern compiled before the wrapper existed')
        denial = g.check('browser_navigate', {'url': 'https://evil.example/x'})
        self.assertIsNotNone(denial, 'allowlist mode must still be on')
        self.assertEqual(denial['safety_decision'], 'not_allowlisted')
        self.assertIsNone(g.check('browser_navigate',
                                  {'url': 'https://localhost:3000/app'}))

    def test_an_allowlist_that_cannot_compile_permits_nothing(self):
        """Fail closed. allowed_url_patterns is only consulted when it is
        non-empty, so dropping every pattern switched the strictest setting on
        offer off entirely."""
        g = guard(allowed_url_patterns=['(unterminated'])
        denial = g.check('browser_navigate', {'url': 'https://localhost/x'})
        self.assertIsNotNone(denial, 'a broken allowlist must not open up')
        self.assertEqual(denial['safety_decision'], 'not_allowlisted')
        self.assertTrue(any('no pattern compiled' in e
                            for e in g.status()['pattern_errors']),
                        'browser_safety_status has to say why nothing works')

    def test_userinfo_does_not_satisfy_a_permit_pattern(self):
        """':' is this guard's delimiter and also the userinfo password
        separator, so the match stopped at the ':' and never looked at the
        authority: https://localhost:3000@evil.com/ loads evil.com.

        The IPv6 cases are the ones the first fix missed. urlsplit hands back
        an IPv6 literal without its brackets, so the host always holds ':',
        the forbidden-host test rejected it and _normalise_url returned the
        URL untouched - taking the userinfo drop down with it.
        """
        hostile_urls = (
            'https://localhost:3000@evil.com/steal',
            'https://localhost:3000@[2606:4700::1]/steal',
            'https://localhost:3000@[2606:4700::1]:8443/steal',
            'https://localhost:3000@[::ffff:93.184.216.34]/steal',
        )
        for hostile in hostile_urls:
            with self.subTest(url=hostile):
                g = guard(allowed_url_patterns=[r'^https://localhost'])
                denial = g.check('browser_navigate', {'url': hostile})
                self.assertIsNotNone(denial, 'this URL loads another host')
                self.assertEqual(denial['safety_decision'], 'not_allowlisted')

                t = guard(unlisted_domains='confirm',
                          trusted_url_patterns=[r'^https://localhost'])
                t.note_url({'url': hostile})
                denial = t.check('browser_click', {'selector': '#x'})
                self.assertIsNotNone(denial, 'the real host is not trusted')
                self.assertEqual(denial['safety_decision'],
                                 'confirmation_required')

    def test_an_encoded_host_does_not_evade_the_blocklist(self):
        """The browser percent-decodes the host, so chase%2Ecom loads
        chase.com; the guard matched the text as written."""
        g = guard(blocked_url_patterns=[r'chase\.com'])
        for url in ('https://chase%2Ecom/x', 'https://%63hase.com/x',
                    'https://chase.com./x'):
            with self.subTest(url=url):
                denial = g.check('browser_navigate', {'url': url})
                self.assertIsNotNone(denial, f'{url} loads chase.com')
                self.assertEqual(denial['safety_decision'], 'blocked_url')

    def test_a_permit_pattern_is_not_satisfied_by_the_query_string(self):
        r"""A pattern with a leading wildcard could match into the query, which
        is text the page author chooses: README's own ".*\.stripe\.com"
        granted https://evil.com/?x=a.stripe.com."""
        g = guard(allowed_url_patterns=[r'.*\.stripe\.com'])
        g.note_url({'url': 'https://evil.com/?x=a.stripe.com'})
        denial = g.check('browser_screenshot', {})
        self.assertIsNotNone(denial, 'the host here is evil.com')
        self.assertEqual(denial['safety_decision'], 'not_allowlisted')
        g.note_url({'url': 'https://js.stripe.com/v3'})
        self.assertIsNone(g.check('browser_screenshot', {}),
                          'the pattern still names stripe.com subdomains')

    def test_a_delimiter_inside_the_query_or_fragment_grants_nothing(self):
        r"""Cutting the URL at '?' is not enough on its own. A '/' after the
        query starts is still a delimiter character, and a prefix ending
        there lets ".*\.stripe\.com" cover text the page author chose. The
        fragment is the same text by another name, so '#' has to end the
        granting part as well as '?'."""
        g = guard(allowed_url_patterns=[r'.*\.stripe\.com'])
        for url in ('https://evil.com/?x=a.stripe.com/',
                    'https://evil.com/#x.stripe.com'):
            with self.subTest(url=url):
                g.note_url({'url': url})
                denial = g.check('browser_screenshot', {})
                self.assertIsNotNone(denial, 'the host here is evil.com')
                self.assertEqual(denial['safety_decision'], 'not_allowlisted')

    def test_a_pattern_whose_greedy_match_overshoots_still_permits(self):
        """The delimiter test is applied to a prefix that ends at a delimiter,
        not to whichever match the engine returns first. With
        "^https://example\.com(/foo)?" the engine's first match of
        https://example.com/foobar ends inside "foobar", so the URL was
        refused while https://example.com/bar was permitted - the prefix the
        rule describes exists, it just was not the match handed back."""
        g = guard(allowed_url_patterns=[r'^https://example\.com(/foo)?'])
        for url in ('https://example.com/bar', 'https://example.com/foobar',
                    'https://example.com/foo/deep', 'https://example.com/foo'):
            with self.subTest(url=url):
                self.assertIsNone(g.check('browser_navigate', {'url': url}),
                                  'every one of these is under example.com')
        for hostile in ('https://example.com.evil.test/x',
                        'https://example.community/x'):
            with self.subTest(url=hostile):
                denial = g.check('browser_navigate', {'url': hostile})
                self.assertIsNotNone(denial, 'another host entirely')
                self.assertEqual(denial['safety_decision'], 'not_allowlisted')

    def test_a_bare_host_pattern_covers_that_host_and_no_other(self):
        r"""A permit pattern naming a host matches the whole of one host.
        r"example\.com" does not cover www.example.com - write
        r"(.+\.)?example\.com" for the tree."""
        g = guard(allowed_url_patterns=[r'example\.com'])
        g.note_url({'url': 'https://example.com/x'})
        self.assertIsNone(g.check('browser_screenshot', {}))
        g.note_url({'url': 'https://www.example.com/x'})
        self.assertIsNotNone(g.check('browser_screenshot', {}),
                             'a bare host pattern names one host')

        tree = guard(allowed_url_patterns=[r'(.+\.)?example\.com'])
        for url in ('https://example.com/x', 'https://www.example.com/x'):
            with self.subTest(url=url):
                tree.note_url({'url': url})
                self.assertIsNone(tree.check('browser_screenshot', {}))
        for url in ('https://evilexample.com/x',
                    'https://example.com.evil.com/x'):
            with self.subTest(url=url):
                tree.note_url({'url': url})
                self.assertIsNotNone(tree.check('browser_screenshot', {}),
                                     f'{url} is not in the example.com tree')


class ProtectedDomainNormalisationTests(unittest.TestCase):
    """protected_url_patterns is matched against the URL the browser will
    load, not the text the agent typed. The browser percent-decodes the host
    and keeps no trailing dot, so https://%63hase.com/transfer and
    https://www.irs.gov./payments both reached a protected site unchallenged."""

    def test_a_percent_encoded_host_is_still_protected(self):
        for url in ('https://%63hase.com/transfer',
                    'https://www.irs.%67ov/payments',
                    'https://CHASE%2Ecom/transfer'):
            with self.subTest(url=url):
                denial = guard().check('browser_navigate', {'url': url})
                self.assertIsNotNone(denial, f'{url} is a protected site')
                self.assertEqual(denial['safety_decision'],
                                 'confirmation_required')

    def test_a_trailing_dot_is_still_protected(self):
        for url in ('https://www.irs.gov./payments',
                    'https://www.chase.com../transfer'):
            with self.subTest(url=url):
                denial = guard().check('browser_navigate', {'url': url})
                self.assertIsNotNone(denial, f'{url} is a protected site')
                self.assertEqual(denial['safety_decision'],
                                 'confirmation_required')

    def test_the_url_sent_to_the_browser_is_not_rewritten(self):
        """The guard judges a normalised copy. Dropping userinfo or a trailing
        dot from the arguments would change the page the browser loads."""
        g = guard()
        arguments = {'url': 'https://localhost:3000@example.com/x'}
        g.check('browser_navigate', arguments)
        self.assertEqual(arguments['url'],
                         'https://localhost:3000@example.com/x')

    def test_an_ordinary_url_is_left_alone(self):
        for url in ('about:blank', 'http://[::1]:8080/x',
                    'https://host:abc/x', 'https://localhost:3000/app?a=b#c'):
            with self.subTest(url=url):
                self.assertIsNone(guard().check('browser_navigate',
                                                {'url': url}))
                # The IPv6 case passed even while normalisation bailed out of
                # it entirely, which is how it hid the userinfo hole. Pin the
                # rewrite, not just the decision.
                self.assertEqual(safety._normalise_url(url), url)

    def test_userinfo_is_dropped_from_an_ipv6_target(self):
        """The one rewrite that must not depend on the host being
        normalisable. An IPv6 literal is not, and the early exit handed the
        URL back with 'localhost:3000@' still on the front of it."""
        cases = {
            'https://localhost:3000@[::1]:9/admin': 'https://[::1]:9/admin',
            'https://alice:pw@[2606:4700::1]/x': 'https://[2606:4700::1]/x',
            'https://localhost@[::ffff:93.184.216.34]/x':
                'https://[::ffff:93.184.216.34]/x',
            'https://a:b@[2606:4700::1]:8443/x?q=1#f':
                'https://[2606:4700::1]:8443/x?q=1#f',
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(safety._normalise_url(url), expected)

    def test_the_last_at_sign_ends_the_userinfo(self):
        """The browser reads everything up to the authority's last '@' as
        userinfo. Splitting at the first one instead leaves
        'localhost:3000@' in front of the host, and a permit pattern
        anchored on 'https://localhost' reads that while the browser loads
        2606:4700::1."""
        hostile = 'https://a@localhost:3000@[2606:4700::1]/x'
        self.assertEqual(safety._normalise_url(hostile),
                         'https://[2606:4700::1]/x')
        g = guard(allowed_url_patterns=[r'^https://localhost'])
        denial = g.check('browser_navigate', {'url': hostile})
        self.assertIsNotNone(denial, 'this URL loads another host')
        self.assertEqual(denial['safety_decision'], 'not_allowlisted')

    def test_userinfo_is_dropped_even_from_a_host_we_cannot_normalise(self):
        """A host still holding a forbidden character once decoded is a URL
        the browser refuses, so there is nothing to normalise it towards - but
        handing the text back left the userinfo in place, and that is what a
        permit pattern reads."""
        self.assertEqual(
            safety._normalise_url('https://localhost:3000@ev%2Fil.com/x'),
            'https://ev%2Fil.com/x')
        self.assertEqual(
            safety._normalise_url('https://localhost:3000@[not:an:address%2F]/x'),
            'https://[not:an:address%2F]/x')


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


class AuditToolScriptTests(unittest.TestCase):
    """browser_audit_page runs a fixed inspection script. It was an observe
    tool, so the guard returned early and the server then dispatched
    executeScript itself - no executeScript ever reached the guard, and a
    user who set allow_script_execution: false still had JavaScript run in
    their pages, on a protected site, in read-only mode."""

    def test_the_script_toggle_covers_the_audit_tool(self):
        g = guard(allow_script_execution=False)
        denial = g.check('browser_audit_page', {})
        self.assertIsNotNone(denial, 'the toggle says no JavaScript at all')
        self.assertEqual(denial['safety_decision'], 'scripts_disabled')

    def test_the_audit_tool_is_refused_on_a_protected_site(self):
        g = guard()
        g.note_url({'url': 'https://www.chase.com/transfer'})
        denial = g.check('browser_audit_page', {})
        self.assertIsNotNone(denial)
        self.assertEqual(denial['safety_decision'],
                         'scripts_denied_on_protected_url')

    def test_it_is_still_observation_in_read_only_mode(self):
        """read-only is about not changing the page, and the audit script
        changes nothing. Blocking a read tool there buys no safety."""
        self.assertIsNone(guard(read_only=True).check('browser_audit_page', {}))

    def test_it_needs_no_confirmation_on_an_ordinary_page(self):
        g = guard()
        g.note_url({'url': 'https://example.com/page'})
        self.assertIsNone(g.check('browser_audit_page', {}))

    def test_status_names_every_tool_the_toggle_covers(self):
        """The toggle's reach is what a user inspects this tool for."""
        listed = guard().status()['script_tools']
        self.assertIn('browser_audit_page', listed)
        self.assertIn('browser_execute_script', listed)


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


class UnlistedDomainPolicyTests(unittest.TestCase):
    """protected_url_patterns is a denylist of ~16 finance/health patterns, so
    mail, cloud consoles and admin panels are unprotected by default. The
    inverted mode closes that, opt-in."""

    def test_default_leaves_unlisted_domains_alone(self):
        g = guard()
        g.note_url({'url': 'https://mail.example.com/inbox'})
        self.assertIsNone(g.check('browser_click', {'selector': '#x'}))

    def test_confirm_mode_protects_an_unlisted_domain(self):
        g = guard(unlisted_domains='confirm')
        g.note_url({'url': 'https://mail.example.com/inbox'})
        denial = g.check('browser_click', {'selector': '#x'})
        self.assertIsNotNone(denial)
        self.assertEqual(denial['safety_decision'], 'confirmation_required')

    def test_confirm_mode_exempts_trusted_patterns(self):
        g = guard(unlisted_domains='confirm',
                  trusted_url_patterns=[r'^https?://localhost'])
        g.note_url({'url': 'http://localhost:3000/app'})
        self.assertIsNone(g.check('browser_click', {'selector': '#x'}))

    def test_confirm_mode_still_allows_observation(self):
        """Inverting the policy must not make reading the page a prompt."""
        g = guard(unlisted_domains='confirm')
        g.note_url({'url': 'https://mail.example.com/inbox'})
        self.assertIsNone(g.check('browser_get_text', {}))

    def test_a_built_in_protected_pattern_still_wins(self):
        g = guard(unlisted_domains='confirm',
                  trusted_url_patterns=[r'.'])  # trust everything
        g.note_url({'url': 'https://www.chase.com/transfer'})
        denial = g.check('browser_click', {'selector': '#x'})
        self.assertIsNotNone(denial,
                             'an explicit protected pattern must not be '
                             'overridden by a broad trusted pattern')


class ScriptsOnProtectedSitesTests(unittest.TestCase):
    """browser_execute_script can read any field, so the credential guard does
    not constrain it. A confirmation the agent can satisfy itself is no
    control over arbitrary JavaScript."""

    def test_scripts_are_refused_on_a_protected_site(self):
        g = guard()
        g.note_url({'url': 'https://www.chase.com/transfer'})
        denial = g.check('browser_execute_script', {'script': 'document.title'})
        self.assertIsNotNone(denial)
        self.assertEqual(denial['safety_decision'],
                         'scripts_denied_on_protected_url')

    def test_a_confirm_token_cannot_buy_a_script_on_a_protected_site(self):
        """The refusal must not be a confirmation: there is no token to get."""
        g = guard()
        g.note_url({'url': 'https://www.chase.com/transfer'})
        denial = g.check('browser_execute_script', {'script': 'x'})
        self.assertNotIn('confirm_token', denial)

    def test_scripts_still_work_off_protected_sites(self):
        g = guard()
        g.note_url({'url': 'https://example.com/page'})
        self.assertIsNone(g.check('browser_execute_script', {'script': 'x'}))

    def test_the_restriction_can_be_turned_off(self):
        g = guard(deny_scripts_on_protected_urls=False)
        g.note_url({'url': 'https://www.chase.com/transfer'})
        denial = g.check('browser_execute_script', {'script': 'x'})
        # Falls back to the ordinary protected-domain confirmation.
        self.assertEqual(denial['safety_decision'], 'confirmation_required')

    def test_status_says_whether_the_credential_guard_is_advisory(self):
        enforced = guard(allow_script_execution=False).status()
        self.assertEqual(enforced['credential_guard'], 'enforced')

        partial = guard(allow_script_execution=True,
                        deny_scripts_on_protected_urls=True).status()
        self.assertEqual(partial['credential_guard'], 'enforced_except_scripts')

        advisory = guard(allow_script_execution=True,
                         deny_scripts_on_protected_urls=False).status()
        self.assertEqual(advisory['credential_guard'], 'advisory')


class PolicyChoiceTests(unittest.TestCase):
    """protected_approval and unlisted_domains were compared with ==, so
    "Human" or " human" matched nothing and fell through to the weaker
    branch - for protected_approval that is the agent-side token flow."""

    def test_an_approval_mode_is_read_case_insensitively(self):
        g = guard(protected_approval='Human')
        self.assertEqual(g.status()['protected_approval'], 'human')
        g.note_url({'url': 'https://www.chase.com/transfer'})
        denial = g.check('browser_click', {'selector': '#x'})
        self.assertEqual(denial['approval_mode'], 'human',
                         'the server routes on this value; "Human" must not '
                         'silently become the token flow')

    def test_surrounding_space_is_tolerated(self):
        g = guard(protected_approval=' token ')
        self.assertEqual(g.status()['protected_approval'], 'token')

    def test_an_unknown_mode_falls_back_to_the_default(self):
        g = guard(protected_approval='yes please')
        self.assertEqual(g.status()['protected_approval'], 'auto')

    def test_unlisted_domains_is_read_the_same_way(self):
        g = guard(unlisted_domains=' Confirm ')
        self.assertEqual(g.status()['unlisted_domains'], 'confirm')
        g.note_url({'url': 'https://mail.example.com/inbox'})
        denial = g.check('browser_click', {'selector': '#x'})
        self.assertIsNotNone(denial, '"Confirm" must invert the policy as '
                                     'the person asked')
        self.assertEqual(denial['safety_decision'], 'confirmation_required')


class AuditLogTests(unittest.TestCase):

    def _last_entry(self):
        return json.loads(safety._AUDIT_FILE.read_text().strip().splitlines()[-1])

    def test_sensitive_values_are_not_written(self):
        g = guard()
        g.check('browser_type', {'selector': '#x', 'text': 'hunter2'})
        written = safety._AUDIT_FILE.read_text()
        self.assertNotIn('hunter2', written)
        self.assertIn('browser_type', written)

    def test_a_credential_in_a_url_is_not_written(self):
        """userinfo is a password, and a reset or SSO link carries a token in
        the query. Both used to land in audit.jsonl in clear: the url
        argument was in no redaction list, and the entry's own url field is
        normalised, which drops userinfo but keeps the query."""
        g = guard()
        g.check('browser_navigate',
                {'url': 'https://alice:hunter2@intranet.example.com/'})
        g.check('browser_navigate',
                {'url': 'https://example.com/reset?token=S3CRET-RESET-TOKEN'})
        g.check('browser_create_tab',
                {'url': 'https://sso.example.com/cb#id_token=eyJhbGciOi'})
        written = safety._AUDIT_FILE.read_text()
        for secret in ('hunter2', 'S3CRET-RESET-TOKEN', 'eyJhbGciOi'):
            self.assertNotIn(secret, written)

    def test_a_url_keeps_the_part_that_makes_the_entry_useful(self):
        """Blanking the URL would cost the log its point - it exists to say
        what was done - so the normalised host and path stay."""
        g = guard()
        g.check('browser_navigate',
                {'url': 'https://alice:hunter2@intranet.example.com/wiki/Home'})
        entry = self._last_entry()
        self.assertEqual(entry['args']['url'],
                         'https://***@intranet.example.com/wiki/Home')
        # The entry's own url field is the normalised URL, which has had the
        # userinfo taken off it already, so there is nothing to mark there.
        self.assertEqual(entry['url'],
                         'https://intranet.example.com/wiki/Home')

    def test_the_tracked_page_is_redacted_when_the_call_names_no_url(self):
        """Most calls (click, type, read) carry no url argument, so the
        entry's url field is the tracked current page. That is a URL the
        browser reported, reset token and all, and it needs the same
        reduction as one the agent sent."""
        g = guard()
        g.note_url({'url': 'https://x.test/reset'
                           '?token=TRACKED-RESET-T0KEN#frag-T0KEN'})
        g.check('browser_click', {'selector': '#a'})
        self.assertEqual(self._last_entry()['url'],
                         'https://x.test/reset?***#***')
        written = safety._AUDIT_FILE.read_text()
        self.assertNotIn('TRACKED-RESET-T0KEN', written)
        self.assertNotIn('frag-T0KEN', written)

    def test_a_query_and_a_fragment_are_reduced_to_a_marker(self):
        g = guard()
        g.check('browser_navigate',
                {'url': 'https://example.com/reset?token=abc#t=1'})
        entry = self._last_entry()
        self.assertEqual(entry['args']['url'], 'https://example.com/reset?***#***')

    def test_an_ordinary_url_is_recorded_as_it_is(self):
        g = guard()
        g.check('browser_navigate', {'url': 'https://example.com/docs/page'})
        self.assertEqual(self._last_entry()['args']['url'],
                         'https://example.com/docs/page')

    def test_a_refused_scheme_is_recorded_without_its_payload(self):
        """A javascript: or data: URL is a payload, not a location. The
        decision is what the log needs; the body of the script is not."""
        g = guard()
        g.check('browser_navigate', {'url': 'javascript:alert(document.cookie)'})
        entry = self._last_entry()
        self.assertEqual(entry['args']['url'], 'javascript:***')
        self.assertEqual(entry['decision'], 'blocked_scheme')
        self.assertNotIn('cookie', safety._AUDIT_FILE.read_text())

    def test_about_blank_is_left_alone(self):
        g = guard()
        g.check('browser_navigate', {'url': 'about:blank'})
        self.assertEqual(self._last_entry()['args']['url'], 'about:blank')

    def test_a_typed_key_sequence_is_not_reconstructible(self):
        """browser_press_key logged its key, one entry per press, in order.
        Synthetic KeyboardEvents cannot type in attended Firefox, but
        headless keyboard.press does, so a password typed key by key was
        sitting in the log."""
        g = guard()
        for char in 'hunter2':
            g.check('browser_press_key', {'key': char})
        entries = [json.loads(line) for line
                   in safety._AUDIT_FILE.read_text().strip().splitlines()
                   if '"browser_press_key"' in line]
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(entry['args']['key'], '***')

    def test_every_sensitive_argument_is_masked(self):
        """Only 'text' and 'key' were pinned, so any other entry could fall
        out of SENSITIVE_ARGS with the suite still green - and each one
        carries something the log must not: a typed or set value, a
        password, or a script that may hold one. Each key goes through a
        tool that takes it, because the audit file is where it would leak."""
        cases = {
            'text': 'browser_type',
            'value': 'browser_set_value',
            # No tool declares 'password', but the guard sees whatever the
            # agent sends, and this is the one name that is never safe.
            'password': 'browser_type',
            'script': 'browser_execute_script',
            'steps': 'browser_run_workflow',
            'action_script': 'browser_wait_and_act',
            'condition': 'browser_wait_and_act',
            'key': 'browser_press_key',
        }
        # One direction only: a key removed from SENSITIVE_ARGS has to fail
        # on what reaches the log below, not on this bookkeeping.
        self.assertLessEqual(set(safety.SENSITIVE_ARGS), set(cases),
                             'a new sensitive key needs a case here')
        g = guard()
        g.note_url({'url': 'https://example.com/page'})
        for key, tool in cases.items():
            secret = f'S3CRET-{key}-value'
            # steps is a list of step objects, and a list is logged as-is.
            value = [{'action': 'type', 'text': secret}] \
                if key == 'steps' else secret
            with self.subTest(key=key):
                g.check(tool, {'selector': '#x', key: value})
                written = safety._AUDIT_FILE.read_text()
                self.assertEqual(self._last_entry()['tool'], tool,
                                 'the call has to reach the audit log')
                self.assertNotIn(secret, written)
                self.assertEqual(safety.redact_arguments({key: value}),
                                 {key: '***'})

    def test_the_audit_file_is_not_world_readable(self):
        g = guard()
        g.check('browser_get_text', {})
        mode = safety._AUDIT_FILE.stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0,
                         f'audit log is a browsing history: {oct(mode)}')

    def test_the_audit_file_rotates_at_its_cap(self):
        """It had no cap of any kind, and every tool call writes a line."""
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / 'audit.jsonl'
            with unittest.mock.patch.object(safety, '_AUDIT_FILE', audit), \
                    unittest.mock.patch.object(safety, '_AUDIT_MAX_BYTES', 2000):
                g = guard()
                for _ in range(100):
                    g.check('browser_get_text', {})
                backup = audit.with_suffix('.jsonl.1')
                self.assertTrue(backup.exists(), 'nothing was rotated')
                for path in (audit, backup):
                    with self.subTest(file=path.name):
                        # One entry may land after the size check.
                        self.assertLess(path.stat().st_size, 2000 + 1000)
                        self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_the_audit_cap_is_a_few_megabytes(self):
        """The test above shrinks the cap to see it work, so it cannot tell
        whether the real one is a cap at all."""
        self.assertLessEqual(safety._AUDIT_MAX_BYTES, 10 * 1024 * 1024)


class SlashRunTests(unittest.TestCase):
    """The browser reads any run of slashes or backslashes after http: or
    https: as "//" (the URL standard's "special authority ignore slashes"),
    so https:///evil.com/ loads evil.com. The guard read it as a URL with
    no host, so anchored patterns never matched it."""

    VARIANTS = ('https:///{}/', 'https:////{}/', 'https:/\\/{}/',
                'HTTPS:///{}/', 'https:{}/')

    def test_an_anchored_blocklist_still_blocks(self):
        g = guard(blocked_url_patterns=[r'^https?://(www\.)?evil\.com'])
        for shape in self.VARIANTS:
            url = shape.format('evil.com')
            with self.subTest(url=url):
                denial = g.check('browser_navigate', {'url': url})
                self.assertIsNotNone(denial, 'navigated to a blocked site')
                self.assertEqual(denial['safety_decision'], 'blocked_url')

    def test_an_anchored_protected_site_still_asks(self):
        g = guard(protected_url_patterns=[r'^https://([^/]*\.)?mybank\.com'])
        for shape in self.VARIANTS:
            url = shape.format('mybank.com') + 'transfer'
            with self.subTest(url=url):
                denial = g.check('browser_click',
                                 {'url': url, 'selector': '#send'})
                self.assertIsNotNone(denial, 'no confirmation asked')
                self.assertEqual(denial['safety_decision'],
                                 'confirmation_required')

    def test_userinfo_after_extra_slashes_is_not_logged(self):
        logged = safety.redact_url('https:///alice:hunter2@intranet.example/')
        self.assertNotIn('hunter2', logged)
        self.assertNotIn('alice', logged)


class PendingTokenCapTests(unittest.TestCase):
    """Every protected call the agent makes issues a token. Without a cap the
    table grows for as long as the agent keeps asking."""

    def test_the_table_never_holds_more_than_the_cap(self):
        # A fixed count, not one derived from the cap: a cap raised to a
        # billion should fail this test, not make it loop a billion times.
        g = guard()
        tokens = [g._issue_token('browser_click', f'fp{i}')
                  for i in range(40)]
        self.assertEqual(len(g._pending_tokens), 32)
        self.assertFalse(g._consume_token(tokens[0], 'fp0'),
                         'the oldest token should have been dropped')
        last = len(tokens) - 1
        self.assertTrue(g._consume_token(tokens[last], f'fp{last}'),
                        'the newest token must still work')


if __name__ == '__main__':
    unittest.main()

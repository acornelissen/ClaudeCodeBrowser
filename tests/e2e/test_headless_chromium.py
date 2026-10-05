#!/usr/bin/env python3
"""
End-to-end tests: HeadlessBrowser driving real Chromium.

tests/test_headless_backend.py drives _dispatch() with a fake page, so it
proves what the backend ASKS of Playwright, not what a browser then does. The
credential guards are only as good as the JavaScript probes they run in the
page, and a fake cannot run those: a web component wrapping a password field
had headless type 'hunter2a' into it while every fake-page test passed. These
tests run the same actions against real pages, served from fixtures/ on
127.0.0.1, and check the outcome in the page itself.

Chromium, not the backend's Firefox default: Playwright's Firefox does not
start from a terminal on current macOS, and Chromium is the cheaper install
in CI.

Skipped, with the reason, when playwright or its Chromium is not installed,
so `mise run test` passes on a machine without them. Set CCB_E2E_REQUIRE=1
(CI does) to turn that skip into a failure, so a broken install cannot pass
as green.

Run: python3 -m unittest tests.e2e.test_headless_chromium -v
"""

import asyncio
import functools
import http.server
import logging
import os
import sys
import threading
import unittest
import unittest.mock
from pathlib import Path

from tests import REPO_ROOT, TEST_HOME  # redirects HOME on import

# Before HOME moved, Playwright found its browsers under it: ~/.cache on
# Linux, ~/Library/Caches on macOS. Point it back at the real cache unless
# the caller chose one. Windows keeps it under LOCALAPPDATA, which HOME does
# not move.
if 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ and os.name == 'posix':
    import pwd
    _real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if sys.platform == 'darwin':
        _cache = _real_home / 'Library' / 'Caches' / 'ms-playwright'
    else:
        _xdg = os.environ.get('XDG_CACHE_HOME')
        _cache = (Path(_xdg) if _xdg else _real_home / '.cache') / 'ms-playwright'
    if _cache.is_dir():
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(_cache)

# Chromium on macOS expects these under HOME.
for _sub in ('Library/Application Support', 'Library/Caches',
             'Library/Preferences'):
    os.makedirs(os.path.join(TEST_HOME, _sub), exist_ok=True)

sys.path.insert(0, str(REPO_ROOT / 'mcp-server'))

import headless_backend  # noqa: E402
from headless_backend import HeadlessBrowser  # noqa: E402

try:
    import playwright  # noqa: F401
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

REQUIRED = os.environ.get('CCB_E2E_REQUIRE') == '1'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
INSTALL_HINT = ('python3 -m pip install playwright==1.63.0 && '
                'python3 -m playwright install chromium')

# Refusals are logged at error level and these tests provoke them on purpose.
logging.getLogger('ClaudeCodeBrowserX.Headless').setLevel(logging.CRITICAL)


def serve_fixtures():
    """Serve fixtures/ on 127.0.0.1 at an ephemeral port. Returns the server."""
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@unittest.skipUnless(
    HAS_PLAYWRIGHT or REQUIRED,
    f'E2E SKIPPED: playwright is not installed. Install it with: {INSTALL_HINT}')
class HeadlessChromiumTest(unittest.TestCase):
    """One browser for the class; each test loads its page fresh."""

    @classmethod
    def setUpClass(cls):
        cls.engine = unittest.mock.patch.object(
            headless_backend, 'BROWSER_TYPE', 'chromium')
        cls.engine.start()
        cls.loop = asyncio.new_event_loop()
        cls.browser = HeadlessBrowser()
        try:
            cls.loop.run_until_complete(cls.browser.start())
        except Exception as e:
            cls._close_loop()
            if "Executable doesn't exist" in str(e) and not REQUIRED:
                raise unittest.SkipTest(
                    'E2E SKIPPED: Playwright\'s Chromium is not installed. '
                    f'Install it with: {INSTALL_HINT}')
            raise
        # Two origins: the page on localhost, the "other site" frame on
        # 127.0.0.1, each on its own port.
        cls.site = serve_fixtures()
        cls.other_site = serve_fixtures()
        cls.origin = f'http://localhost:{cls.site.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        try:
            cls.loop.run_until_complete(cls.browser.stop())
        finally:
            cls._close_loop()
            for server in (cls.site, cls.other_site):
                server.shutdown()
                server.server_close()

    @classmethod
    def _close_loop(cls):
        cls.loop.close()
        cls.engine.stop()

    # -- helpers -------------------------------------------------------------

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    @property
    def page(self):
        return self.browser._page

    def act(self, action, **args):
        return self.run_async(self.browser.execute(action, None, args))

    def open(self, name, query=''):
        result = self.act('navigate', url=f'{self.origin}/{name}{query}')
        self.assertTrue(result.get('success'), result)

    def open_form(self):
        cross = self.other_site.server_address[1]
        self.open('form.html', f'?cross={cross}')
        for frame in ('#same', '#cross'):
            self.run_async(self.page.frame_locator(frame)
                           .locator('#ftext').wait_for(state='attached'))

    def value(self, selector, frame=None):
        root = self.page.frame_locator(frame) if frame else self.page
        return self.run_async(root.locator(selector).input_value())

    def focus(self, selector, frame=None):
        root = self.page.frame_locator(frame) if frame else self.page
        self.run_async(root.locator(selector).focus())

    def evaluate(self, script):
        return self.run_async(self.page.evaluate(script))

    def assertRefused(self, result, contains='Refused'):
        self.assertFalse(result.get('success'), result)
        self.assertIn(contains, result.get('error', ''), result)

    # -- browser_type locators and options -----------------------------------

    def test_type_by_name_fills_the_named_field(self):
        self.open_form()
        result = self.act('type', name='q', text='hello')
        self.assertTrue(result.get('success'), result)
        self.assertEqual(self.value('#q'), 'hello')
        self.assertEqual(self.value('#mfa'), '')

    def test_type_by_placeholder_appends_by_default(self):
        self.open_form()
        self.act('type', name='q', text='hello')
        result = self.act('type', placeholder='Search', text=' world')
        self.assertTrue(result.get('success'), result)
        self.assertEqual(self.value('#q'), 'hello world')

    def test_type_by_id_with_clear_replaces(self):
        self.open_form()
        self.act('type', name='q', text='hello')
        result = self.act('type', id='q', text='fresh', clear=True)
        self.assertTrue(result.get('success'), result)
        self.assertEqual(self.value('#q'), 'fresh')

    def test_type_press_enter_sends_enter_and_submits(self):
        self.open_form()
        result = self.act('type', id='q', text='go', press_enter=True)
        self.assertTrue(result.get('success'), result)
        self.assertEqual(self.value('#q'), 'go')
        self.assertEqual(self.evaluate('window.__enter || 0'), 1)
        self.assertEqual(self.evaluate('window.__submitted || 0'), 1)

    def test_type_without_press_enter_does_not_submit(self):
        self.open_form()
        self.act('type', id='q', text='stay')
        self.assertEqual(self.evaluate('window.__enter || 0'), 0)
        self.assertEqual(self.evaluate('window.__submitted || 0'), 0)

    # -- credential guards: type and pressKey --------------------------------

    def test_type_into_password_field_is_refused(self):
        self.open_form()
        self.assertRefused(self.act('type', selector='#pw', text='hunter2'))
        self.assertEqual(self.value('#pw'), '')

    def test_type_into_mfa_code_field_is_refused(self):
        self.open_form()
        self.assertRefused(self.act('type', name='mfaCode', text='999999'))
        self.assertEqual(self.value('#mfa'), '')

    def test_shift_insert_on_password_field_is_refused(self):
        self.open_form()
        self.assertRefused(self.act('pressKey', selector='#pw', key='Insert',
                                    shift=True))
        self.assertEqual(self.value('#pw'), '')

    def test_tab_on_password_field_is_allowed(self):
        self.open_form()
        result = self.act('pressKey', selector='#pw', key='Tab')
        self.assertTrue(result.get('success'), result)

    # -- focus inside frames -------------------------------------------------

    def test_presskey_into_password_in_same_origin_iframe_is_refused(self):
        self.open_form()
        self.focus('#fpw', frame='#same')
        self.assertRefused(self.act('pressKey', key='a'))
        self.assertEqual(self.value('#fpw', frame='#same'), '')

    def test_presskey_into_ordinary_field_in_same_origin_iframe_is_allowed(self):
        self.open_form()
        self.focus('#ftext', frame='#same')
        result = self.act('pressKey', key='a')
        self.assertTrue(result.get('success'), result)
        self.assertEqual(self.value('#ftext', frame='#same'), 'a')

    def test_presskey_with_focus_in_cross_origin_iframe_is_refused(self):
        self.open_form()
        self.focus('#ftext', frame='#cross')
        self.assertRefused(self.act('pressKey', key='a'), 'another site')
        self.assertEqual(self.value('#ftext', frame='#cross'), '')

    # -- focus inside an open shadow root ------------------------------------

    def focus_shadow_password(self):
        self.open('shadow.html')
        self.evaluate("document.querySelector('my-login').shadowRoot"
                      ".getElementById('inner').focus()")

    def shadow_password_value(self):
        return self.evaluate("document.querySelector('my-login').shadowRoot"
                             ".getElementById('inner').value")

    def test_type_into_password_inside_shadow_root_is_refused(self):
        self.focus_shadow_password()
        self.assertRefused(self.act('type', text='hunter2'))
        self.assertEqual(self.shadow_password_value(), '')

    def test_presskey_into_password_inside_shadow_root_is_refused(self):
        self.focus_shadow_password()
        self.assertRefused(self.act('pressKey', key='a'))
        self.assertEqual(self.shadow_password_value(), '')

    # -- reads: getText and getValue -----------------------------------------

    def read_page_text(self):
        self.open('text.html')
        result = self.act('getText')
        self.assertTrue(result.get('success'), result)
        text = result.get('text', '')
        # Guards against passing on an empty read.
        self.assertIn('VISIBLE-MARKER', text)
        return result, text

    def test_get_text_masks_one_time_code_everywhere(self):
        result, text = self.read_page_text()
        self.assertNotIn('482915', text)
        self.assertIn('Your code is ***', text)
        self.assertIn('enter *** to continue', text)
        self.assertGreaterEqual(result.get('maskedFields', 0), 1, result)

    def test_get_text_masks_three_digit_cvv_textarea(self):
        _, text = self.read_page_text()
        self.assertNotIn('731', text)
        self.assertIn('Card check digits: ***', text)

    def test_get_text_omits_script_and_hidden_text(self):
        _, text = self.read_page_text()
        self.assertNotIn('SCRIPT-SECRET-9', text)
        self.assertNotIn('HIDDEN-TEXT-7', text)

    def test_get_value_on_filled_password_is_masked(self):
        self.open('text.html')
        self.run_async(self.page.fill('#pw-filled', 'hunter2'))
        result = self.act('getValue', selector='#pw-filled')
        self.assertTrue(result.get('success'), result)
        self.assertEqual(result.get('value'), '***')
        self.assertIs(result.get('masked'), True)

    def test_get_value_on_empty_password_is_null(self):
        self.open('text.html')
        result = self.act('getValue', selector='#pw-empty')
        self.assertTrue(result.get('success'), result)
        self.assertIsNone(result.get('value'))
        self.assertIs(result.get('masked'), True)


if __name__ == '__main__':
    unittest.main()

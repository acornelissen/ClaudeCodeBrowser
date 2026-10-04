#!/usr/bin/env python3
"""
Extension identity tests.

Firefox only connects an extension to its native messaging host when the
host manifest's allowed_extensions contains the extension's ID exactly. The
ID therefore appears in more than one file, and a copy that drifts out of
sync breaks the bridge with no useful error. These tests pin them together.

Run: python3 -m unittest discover -s tests -t . -v
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def extension_id():
    manifest = json.loads((ROOT / 'extension' / 'manifest.json').read_text())
    return manifest['browser_specific_settings']['gecko']['id']


class ExtensionIdTests(unittest.TestCase):

    def test_id_is_well_formed(self):
        ext_id = extension_id()
        guid = re.fullmatch(
            r'\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
            r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}', ext_id)
        email = re.fullmatch(r'[^@\s]+@[^@\s]+', ext_id)
        self.assertTrue(guid or email,
                        f'{ext_id!r} is neither a GUID nor name@domain; '
                        'Firefox requires one of those forms')

    def test_id_is_not_the_upstream_id(self):
        """Upstream owns claudecodebrowser@ligandal.com on AMO, so signing a
        fork under it is rejected with 403 Forbidden."""
        self.assertNotEqual(extension_id(), 'claudecodebrowser@ligandal.com')

    def test_native_host_template_allows_exactly_this_extension(self):
        host = json.loads((ROOT / 'native-host' / 'claudecodebrowser.json').read_text())
        self.assertEqual(host['allowed_extensions'], [extension_id()])

    def test_installers_do_not_hardcode_an_id(self):
        """Both installers must read the ID from the manifest, so there is one
        source of truth."""
        for script in ('scripts/install.sh', 'scripts/install.ps1'):
            with self.subTest(script=script):
                body = (ROOT / script).read_text()
                self.assertNotIn(extension_id(), body,
                                 f'{script} should derive the ID from '
                                 'extension/manifest.json, not repeat it')
                self.assertIn('manifest.json', body,
                              f'{script} should read extension/manifest.json')

    def test_update_url_points_at_this_fork(self):
        manifest = json.loads((ROOT / 'extension' / 'manifest.json').read_text())
        update_url = manifest['browser_specific_settings']['gecko']['update_url']
        self.assertIn('acornelissen/ClaudeCodeBrowser', update_url,
                      'auto-update must not point at the upstream repository')

    def test_update_manifest_is_keyed_by_the_extension_id(self):
        """package-extension.sh writes updates.json keyed by the ID; a stale
        key means Firefox never sees the update."""
        packager = (ROOT / 'scripts' / 'package-extension.sh').read_text()
        self.assertIn('${EXT_ID}', packager)
        self.assertIn("['browser_specific_settings']['gecko']['id']", packager)


class PermissionTests(unittest.TestCase):

    def test_webrequest_permissions_are_declared(self):
        """Network logging needs webRequest, and filterResponseData (response
        bodies) additionally needs webRequestBlocking."""
        manifest = json.loads((ROOT / 'extension' / 'manifest.json').read_text())
        for permission in ('webRequest', 'webRequestBlocking'):
            self.assertIn(permission, manifest['permissions'])

    def test_content_script_does_not_touch_page_network_globals(self):
        """Capture belongs in the background script. A content script cannot
        override window.fetch in Firefox's sandbox anyway, and reaching into
        the page is what the webRequest move was meant to stop."""
        source = (ROOT / 'extension' / 'content.js').read_text()
        self.assertNotIn('window.fetch =', source)
        self.assertNotIn('XMLHttpRequest.prototype.open =', source)
        self.assertNotIn('XMLHttpRequest.prototype.send =', source)


if __name__ == '__main__':
    unittest.main()

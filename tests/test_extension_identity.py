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


class ReleaseVersionTests(unittest.TestCase):
    """The CHANGELOG states the MCP server, extension and docs are versioned
    together, and AMO refuses a repeat upload of a version that already
    exists — so a release where these disagree is a release that fails."""

    def version(self):
        manifest = json.loads((ROOT / 'extension' / 'manifest.json').read_text())
        return manifest['version']

    def test_server_reports_the_manifest_version(self):
        server = (ROOT / 'mcp-server' / 'server.py').read_text()
        self.assertIn(f"'version': '{self.version()}'", server,
                      "the /health endpoint must report the manifest version")

    def test_changelog_has_an_entry_for_this_version(self):
        changelog = (ROOT / 'CHANGELOG.md').read_text()
        self.assertIn(f'## [{self.version()}]', changelog)

    def test_readme_badge_matches(self):
        readme = (ROOT / 'README.md').read_text()
        self.assertIn(f'version-{self.version()}-blue', readme)


class AttributionTests(unittest.TestCase):
    """This repository is a fork. Upstream authorship must stay visible: it is
    an MIT condition for the copyright line, and the right thing to do for the
    rest. Rebranding a fork is exactly when it gets dropped by accident."""

    UPSTREAM_AUTHOR = 'Andre Watson'
    UPSTREAM_REPO = 'nanogenomic/ClaudeCodeBrowser'

    def test_license_keeps_the_original_copyright(self):
        license_text = (ROOT / 'LICENSE').read_text()
        self.assertIn('MIT License', license_text)
        self.assertIn(self.UPSTREAM_AUTHOR, license_text)
        self.assertIn('Ligandal', license_text)

    def test_readme_credits_the_original_author(self):
        readme = (ROOT / 'README.md').read_text()
        self.assertIn(self.UPSTREAM_AUTHOR, readme)
        self.assertIn(self.UPSTREAM_REPO, readme,
                      'the README should link upstream, not just name the author')
        self.assertIn('fork', readme.lower(),
                      'the README should state that this is a fork')

    def test_changelog_credits_the_original_author(self):
        changelog = (ROOT / 'CHANGELOG.md').read_text()
        self.assertIn(self.UPSTREAM_AUTHOR, changelog)

    SOURCE_FILES = (
        'extension/background.js',
        'extension/content.js',
        'extension/popup/popup.js',
        'mcp-server/server.py',
        'mcp-server/safety.py',
        'mcp-server/headless_backend.py',
        'mcp-server/stdio_wrapper.py',
        'native-host/claudecodebrowser_host.py',
        'agent/browser_agent.py',
    )

    def test_source_headers_credit_the_original_author(self):
        """Every source file carries the attribution in its header comment.
        Checked against the header rather than the whole file, so a mention
        further down does not satisfy it."""
        for path in self.SOURCE_FILES:
            with self.subTest(path=path):
                header = '\n'.join((ROOT / path).read_text().splitlines()[:45])
                self.assertIn('Copyright', header,
                              f'{path} has no copyright line in its header')
                self.assertIn(self.UPSTREAM_AUTHOR, header,
                              f'{path} does not credit the original author')
                self.assertIn('MIT License', header,
                              f'{path} does not state its license')


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

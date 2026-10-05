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
import os
import re
import subprocess
import sys
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
        further down does not satisfy it.

        20 lines, not 45: safety.py's header docstring grew past 45 and
        pushed the attribution out of the checked window, which this test
        then reported as missing attribution. A tight window makes the
        convention "attribution at the very top", which cannot drift."""
        for path in self.SOURCE_FILES:
            with self.subTest(path=path):
                header = '\n'.join((ROOT / path).read_text().splitlines()[:20])
                self.assertIn('Copyright', header,
                              f'{path} has no copyright line in its header')
                self.assertIn(self.UPSTREAM_AUTHOR, header,
                              f'{path} does not credit the original author')
                self.assertIn('MIT License', header,
                              f'{path} does not state its license')


class PackagingGuardTests(unittest.TestCase):
    """The release path can break auto-update for every installed copy
    silently, so its guards are worth pinning."""

    def test_both_packagers_refuse_to_clobber_a_signed_build(self):
        for script in ('scripts/package-extension.sh', 'scripts/package-extension.ps1'):
            with self.subTest(script=script):
                body = (ROOT / script).read_text()
                self.assertIn('META-INF/mozilla.rsa', body,
                              f'{script} must detect a signed archive before '
                              'overwriting it; publish-release.sh uploads that path')

    def test_publish_verifies_the_signature_and_the_version(self):
        body = (ROOT / 'scripts' / 'publish-release.sh').read_text()
        self.assertIn('META-INF/mozilla.rsa', body,
                      'publishing an unsigned xpi breaks every install')
        self.assertIn('--fail-with-body', body,
                      'curl exits 0 on HTTP 422, so a failed upload looked like success')

    def test_the_unsigned_zip_excludes_top_level_dotfiles(self):
        body = (ROOT / 'scripts' / 'package-extension.sh').read_text()
        self.assertIn("-x '.*'", body,
                      "-x '*/.*' only covers nested dotfiles, so a stray "
                      "extension/.env shipped")

    def test_the_update_manifest_names_only_the_current_id(self):
        """Firefox refuses an update whose id differs from the installed one,
        so an entry for a retired id rescued nobody: those installs
        downloaded the .xpi and failed every check. Listing upstream's id
        would also offer this fork's build to their users. Runs the
        manifest writer the script uses, so a re-added id shows up here."""
        script = (ROOT / 'scripts' / 'package-extension.sh').read_text()
        start = script.index("python3 > \"$DIST/updates.json\" <<'PYEOF'\n")
        body = script[start:].split("<<'PYEOF'\n", 1)[1].split('\nPYEOF', 1)[0]
        env = dict(os.environ, CCB_EXT_ID=extension_id(), CCB_VERSION='9.9.9',
                   CCB_XPI_URL='https://example.test/x.xpi',
                   CCB_LEGACY_EXT_IDS='{31c66d81-5dcd-4210-97ab-098400466392}')
        out = subprocess.run([sys.executable, '-c', body], env=env,
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(list(json.loads(out)['addons']), [extension_id()])

    def test_signing_credentials_are_not_passed_in_argv(self):
        for script in ('scripts/package-extension.sh', 'scripts/package-extension.ps1'):
            with self.subTest(script=script):
                body = (ROOT / script).read_text()
                self.assertNotIn('--api-secret', body,
                                 'argv is readable from any process listing')


class AttributionIdentityTests(unittest.TestCase):

    def test_the_manifest_credits_upstream_where_users_see_it(self):
        """author/homepage_url name the fork, because this build is signed by
        and supported from the fork - but about:addons should still say where
        the work came from."""
        manifest = json.loads((ROOT / 'extension' / 'manifest.json').read_text())
        self.assertIn('Andre Watson', manifest['description'])
        self.assertIn('acornelissen', manifest['homepage_url'])


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

"""Test package setup.

HOME is redirected once, here, before any test module imports the production
code. The modules compute paths from Path.home() at import time, so each test
file setting its own temp HOME meant whichever imported last won and a lazily
imported module disagreed with an already-imported one. Doing it in the
package init makes every module agree, and keeps the real
~/.claudecodebrowser untouched.
"""

import os
import tempfile
from pathlib import Path

TEST_HOME = tempfile.mkdtemp(prefix='ccb-test-home-')
os.environ['HOME'] = TEST_HOME
os.environ.pop('CLAUDE_BROWSER_SCREENSHOTS_DIR', None)
os.environ.pop('CLAUDE_BROWSER_SAFETY_CONFIG', None)

REPO_ROOT = Path(__file__).resolve().parent.parent

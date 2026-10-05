"""Test package setup.

HOME is redirected once, here, before any test module imports the production
code. The modules compute paths from Path.home() at import time, so each test
file setting its own temp HOME meant whichever imported last won and a lazily
imported module disagreed with an already-imported one. Doing it in the
package init makes every module agree, and keeps the real
~/.claudecodebrowserx untouched.

Every CLAUDE_BROWSERX_* variable is cleared for the same reason. Only two were,
so the suite gave a different answer depending on what the developer happened
to have exported - and the failures were not all loud:

  CLAUDE_BROWSERX_HEADLESS           hangs the run. _dispatch_action waits out
                                    HEADLESS_STARTUP_TIMEOUT for a Playwright
                                    browser that is never coming, so the suite
                                    never finishes and has to be killed. This
                                    is what made `mise run test` appear to
                                    hang.
  CLAUDE_BROWSERX_READ_ONLY          7 failures and 2 errors: the shared
                                    SafetyGuard denies the write tools those
                                    tests drive.
  CLAUDE_BROWSERX_WS_ORIGINS         the origin default is no longer the
                                    default.
  CLAUDE_BROWSERX_COMMAND_TTL        the queue-TTL test compares against it.
  CLAUDE_BROWSERX_SCREENSHOT_*       read at import time by the retention
                                    policy.

The rest are cleared because they are read at import time too and the next
test to touch one should not have to rediscover this. A test that wants one
of these set does so itself, with the original restored.
"""

import os
import tempfile
from pathlib import Path

TEST_HOME = tempfile.mkdtemp(prefix='ccb-test-home-')
os.environ['HOME'] = TEST_HOME
# Both prefixes: the pre-rename CLAUDE_BROWSER_* names are still honoured.
for _name in [name for name in os.environ
              if name.startswith(('CLAUDE_BROWSERX_', 'CLAUDE_BROWSER' + '_'))]:
    os.environ.pop(_name, None)

REPO_ROOT = Path(__file__).resolve().parent.parent

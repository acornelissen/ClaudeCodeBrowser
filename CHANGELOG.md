# Changelog

All notable changes to ClaudeCodeBrowser are documented here. Versions follow
[semantic versioning](https://semver.org/). The MCP server, extension, and
docs are versioned together.

## [1.5.0]

First release of this fork (`acornelissen/ClaudeCodeBrowser`). The extension is
renamed **ClaudeCodeBrowserX** and carries a fork-owned extension ID, so it is a
separate add-on from upstream's: remove the old one before installing this.

### Security
- **Credential reads are guarded.** `browser_type` and `browser_set_value`
  already refused password fields, but `browser_get_value` and
  `browser_get_elements` returned the plaintext — and both are observation
  tools, so they worked even in read-only mode. Reads are now masked as
  `***`, as `browser_get_page_info` already did. Same gap closed in the
  headless backend.
- **Page interception is opt-in.** The content script used to wrap
  `window.fetch`, the XHR prototype and all five `console` methods on every
  page in every frame at load, whether or not logging was on. Capture now
  starts with `browser_start_logging` and stops with
  `browser_stop_logging`.
- **Credential-bearing headers are redacted** in captured network logs
  (`Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key` and similar), so
  enabling logging no longer puts bearer tokens into the agent's context.
- **The native host only kills its own server.** It ran `lsof` on port 8765
  and `SIGTERM`/`SIGKILL`'d whatever answered, with a `fuser -k` fallback that
  killed unconditionally — and Firefox launches the host automatically, so an
  unrelated service on that port died unprompted. It now terminates only
  processes running our own server script, and reports failure rather than
  starting a server that cannot bind.
- **Screenshots moved out of shared `/tmp`** to
  `~/.claudecodebrowser/screenshots` at `0700`.
  `CLAUDE_BROWSER_SCREENSHOTS_DIR` is still honoured.

### Changed
- **Network logging uses `webRequest`** instead of wrapping page globals.
  Firefox's content-script sandbox refuses a `window.fetch` override, so the
  old implementation silently captured XHR but never `fetch` — which is what
  modern apps use. Capture now happens in the background script and sees
  `fetch`, XHR, WebSocket handshakes and beacons, is unaffected by a page's
  CSP, and touches nothing in the page. Response bodies use a read-only
  stream filter for textual content types; `capture_bodies: false` turns them
  off and `include_all_types: true` opts into assets.
  - Adds the `webRequest` and `webRequestBlocking` permissions
    (`filterResponseData` requires the latter).
  - The experimental Chrome build captures metadata and headers only:
    `filterResponseData` is Firefox-only and MV3 withholds
    `webRequestBlocking`.
- **`browser_wait_for_network_idle`** counts at the network layer too. It was
  the last place that hooked the page's `fetch`, and it had the same blind
  spot. It now also counts subresources, so a page still loading images is
  correctly not idle.
- Auto-update points at this fork's releases.
- Both installers read the extension ID from `extension/manifest.json`
  instead of repeating it, since Firefox fails silently when the native
  host's `allowed_extensions` does not match exactly.

### Fixed
- `native-host/claudecodebrowser.json` shipped the original author's absolute
  path. The README tells you to copy that file into place, so it was a live
  bug on the manual install route.
- `scripts/install.sh` suggested `xpinstall.signatures.required=false`, which
  does nothing on release Firefox.
- The unsigned packaging path refuses to overwrite a signed `.xpi`. Both land
  on the same filename and `publish-release.sh` uploads it, so a stray
  unsigned rebuild could have shipped a build nobody can install.

### Added
- Test suites with no external dependencies: `unittest` for the Python
  components, and Node harnesses that evaluate `content.js` and
  `background.js` against stubs and drive them through their real message
  entry points. `mise run test` runs all of them.
- `mise` manages `web-ext` and the task list (`test`, `package`, `sign`,
  `release`); AMO credentials live in a gitignored `mise.local.toml`.

## [1.4.0]

### Added
- **`browser_solve_captcha`** — detects reCAPTCHA, hCaptcha, Cloudflare
  Turnstile, and generic image/text captchas and hands them to the human to
  solve (OS notification + in-page banner), then continues. Token-based
  widgets auto-detect completion; otherwise the human clicks Done.
  `detect_only` reports presence without waiting. **Never auto-solves** — no
  OCR, no solver services. Headless mode reports detection and that a human
  is required.
- **Extension auto-update wiring** — manifest `update_url` points at
  `releases/latest/download/updates.json`; `scripts/package-extension.sh` and
  `scripts/package-extension.ps1` generate that `updates.json` alongside the
  versioned `.xpi`.
- **`scripts/package-extension.ps1`** — Windows extension packager/signer.
- **`scripts/publish-release.sh`** — one-command GitHub release: creates the
  `v<version>` release and uploads the `.xpi` + `updates.json` (gh CLI, or
  curl with `GITHUB_TOKEN`).

## [1.3.0]

### Added
- **Human approval (Duo-style, for Claude's actions)** —
  `browser_request_approval` shows an Approve/Deny banner plus an OS
  notification and waits for the human's decision. Protected-site actions
  default to asking the human directly (`protected_approval: auto|human|token`
  in `safety.json`).
- **Credential guard** — typing into password fields is refused by default in
  both attended and headless modes (`allow_password_typing` overrides).
  Credentials stay in the browser's own password manager. 2FA approvals are
  never automated (explicit non-goal).
- **`browser_run_workflow`** — declarative multi-step test runner with
  per-step assertions (`url_contains`, `text_contains`, `selector_exists`),
  safety checks per step, and automatic screenshots on failure.
- **`browser_audit_page`** — one-call structural/accessibility audit (heading
  hierarchy, missing alt text, unlabeled inputs, empty links/buttons,
  meta/viewport info) plus screenshot, for visual critique.
- **`scripts/package-extension.sh`** — builds a versioned `.xpi`, optionally
  signs it via Mozilla's web-ext + AMO API.

## [1.2.0]

### Added
- **Cross-browser headless** — Firefox, Chromium, or WebKit via
  `CLAUDE_BROWSER_ENGINE`; `CLAUDE_BROWSER_EXECUTABLE` to use a system browser.
- **Headless multi-tab management** — `browser_create_tab` returns a `tabId`;
  create/list/focus/close and `tab_id` routing work against live pages.
- **`scripts/build-chrome.sh`** — experimental Chrome/Chromium Manifest V3
  build (API shim + MV3 manifest + native-messaging template).
- **`scripts/install.ps1`** — Windows installer (native-host `.bat` wrapper +
  registry registration).
- README badges, browser support matrix, mermaid architecture diagram,
  headless-mode docs.

### Security
- WebSocket control channel now requires the API token as its first frame.
- `websocket_handler` accepts websockets >= 14 (optional `path` argument).
- Extension refuses `runtime.onMessageExternal` instead of forwarding
  arbitrary co-installed-extension messages to `handleCommand`.
- Content-script fetch/XHR/console interceptors are individually guarded so a
  read-only `window.fetch` in Firefox's sandbox no longer aborts the whole
  script; log getters report `interceptionAvailable`.

### Fixed
- `install.sh` now copies `safety.py` and `headless_backend.py`.

## [1.1.0]

### Added
- **Safety guard** on every tool call (`mcp-server/safety.py`,
  `~/.claudecodebrowser/safety.json`): URL scheme guard, blocklist/allowlist,
  protected-site confirmation tokens, read-only mode, script-execution kill
  switch, sliding-window rate limit, redacted JSONL audit log, and
  `browser_safety_status`.
- **New tools** — `browser_go_back`, `browser_go_forward`,
  `browser_press_key`, `browser_get_text`.

### Fixed
- **Headless mode on Python 3.12+** (issue #9): the main-thread event loop is
  published so HTTP worker threads dispatch onto it via
  `run_coroutine_threadsafe`; readiness wait avoids failing the session's
  first call. Same fix applied to the WebSocket branch.
- `background.js` now forwards `selectOption`, `hover`, `getValue`,
  `setValue`, `waitForChange`, `waitForNetworkIdle`, `observeElement`,
  `stopObserving`, `scrollAndCapture`, and `clickAndWait` to the content
  script (previously "Unknown action").
- snake_case MCP arguments (`full_page`, `url_pattern`, …) are camelized at
  the extension boundary so multi-word options reach the extension.

## Merged community contributions
- **#5** — macOS installer support (native-messaging path, launcher script).
- **#6** — cap `browser_get_tabs` response size for users with many tabs.

[1.4.0]: https://github.com/nanogenomic/ClaudeCodeBrowser/releases/tag/v1.4.0
[1.3.0]: https://github.com/nanogenomic/ClaudeCodeBrowser/releases/tag/v1.3.0
[1.2.0]: https://github.com/nanogenomic/ClaudeCodeBrowser/releases/tag/v1.2.0
[1.1.0]: https://github.com/nanogenomic/ClaudeCodeBrowser/releases/tag/v1.1.0

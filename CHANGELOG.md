# Changelog

All notable changes to ClaudeCodeBrowser are documented here. Versions follow
[semantic versioning](https://semver.org/). The MCP server, extension, and
docs are versioned together.

ClaudeCodeBrowser was created by Andre Watson
([@nanogenomic](https://github.com/nanogenomic), Ligandal Inc.); 1.1.0–1.4.0
are his releases. 1.5.0 onwards are from the fork at
<https://github.com/acornelissen/ClaudeCodeBrowser>.

## [1.7.1]

### Fixed
- **Boolean tool options could silently do nothing.** Found by live testing:
  the MCP client dispatched `include_all_types` as the string `"true"`, and a
  strict `=== true` comparison rejected it, so the option had no effect. The
  same shape made `capture_bodies: "false"` fail *open* — bodies captured in
  full for a caller who asked for none. Feature flags now accept booleans,
  numbers and the usual string spellings, and fall back to the documented
  default for anything unparseable rather than treating a non-empty string as
  true. The credential override is deliberately excluded and remains strictly
  `=== true`: a fail-closed security switch must not be unlocked by a
  truthy-looking value.

Every unit test used clean booleans, which is why none of them caught it.

## [1.7.0]

Closes three gaps that 1.6.0's notes listed as unfixable. They were not.

### Security
- **The approval decision left the page entirely.** Even in a closed shadow
  root, a prompt rendered in the automated page sits in DOM the page owns and
  can be covered. The Approve/Deny decision now happens in an extension page
  in its own window (`moz-extension://`), which the page cannot read, restyle
  or dispatch events into. Only the extension's own pages may answer — a
  message carrying `sender.tab` is refused, so neither a content script nor
  another extension can decide one — and closing the window is a denial. The
  in-page banner survives only as a fallback where no window can be opened,
  and the result then carries `degraded: true`. Firefox does not support
  buttons on notifications, so the notification stays an attention-getter.
- **`browser_execute_script` is refused on protected sites**, not confirmed. A
  script can read any field, so the credential guard never constrained it, and
  a `confirm_token` the agent satisfies itself is no control over arbitrary
  JavaScript. `deny_scripts_on_protected_urls` defaults to true.
- **`unlisted_domains: "confirm"`** inverts the protected-domain policy, so
  anything not in `trusted_url_patterns` requires confirmation. The built-in
  list is ~16 finance, health and government patterns, which left mail, cloud
  consoles and admin panels unprotected by default. Opt-in: it prompts until
  the trusted list is right, and prompt fatigue is its own hazard.

### Changed
- `browser_safety_status` reports whether the credential guard is `enforced`,
  `enforced_except_scripts` or `advisory`, so an agent can discover the limit
  rather than assume the guard covers scripts.

### Added
- 151 tests, up from 141. The background harness's `runtime.onMessage` and
  `onMessageExternal` were `addListener(){}` black holes, so those handlers
  were unreachable from any test — including the gate on who may answer an
  approval and the `onMessageExternal` refusal. Both are now driven.

### Still not fixed, and not fixable here
- Page text in an LLM's context. The untrusted-content fence narrows it.
- Two-step confirmation inside one agent's context: both steps are the same
  party. Only a channel the agent cannot drive is a real second party.
- The guard reads `~/.claudecodebrowser/safety.json`, which the agent being
  gated can write. That belongs in the harness permission layer.

## [1.6.0]

A security and correctness release following a six-dimension audit of the
fork. Several findings were defects in 1.5.x introduced by this fork; the rest
were inherited and long-standing. **Upgrade is recommended**: 1.5.1 contains a
response-filter bug that can stall a page's network requests.

### Security
- **`browser_get_page_info` returned password values in plaintext.** Its
  `forms[]` branch masked and its `interactiveElements[]` branch did not, and
  the selector list includes `input` — so a filled, visible password field
  yielded its first 100 characters to the tool an agent calls first on every
  page. It is an observation tool, so it worked in read-only mode with no
  confirmation. One function now decides what any element's value looks like,
  and every read path goes through it.
- **The credential definition was too narrow.** `autocomplete` is a
  case-insensitive token list, so `Current-Password` and
  `section-login current-password` were not matched; one-time codes, card
  fields and hidden inputs (CSRF and session tokens) were not covered at all.
- **The approval prompt could be clicked by the page.** It was a plain button
  in the page's own DOM at a fixed id, styled inline so a page `!important`
  rule beat it, with no `isTrusted` check — so a site could hide the prompt
  and approve its own protected action, and the audit log recorded
  `allowed_by_human`. It now renders in a closed shadow root and acts only on
  trusted events. It was also dispatched with the tab id stripped and
  broadcast to every frame, so a hidden iframe in an unrelated tab could
  approve an action on a banking tab; it now goes to the top frame of the
  acting tab. An undeliverable prompt is a refusal, not a fallback to a token
  the agent can satisfy itself.
- **The native host trusted whatever answered on port 8765.** Any response
  containing "ok" was accepted as the MCP server, so a process that bound the
  port first received the API token on every poll and could return commands
  that the extension executed with the safety guard never consulted. The
  server now has to prove it holds the shared token.
- **`native_host.log` was a cleartext transcript of the session** — DEBUG
  level, every message in both directions, no redaction, `0644`. Page text,
  tab URLs, typed text and base64 screenshots all landed there.
- **`confirm_token` was bound to the tool name only**, so a token earned
  clicking a harmless element authorised any click on any URL for two minutes.
  It is now bound to the exact call.
- **The blocklist confined nothing.** It was checked only against a `url`
  argument, and no read tool takes one, so blocking a domain stopped
  navigating there while leaving every read tool free on an already-open tab.
- Scroll, hover, highlight and focus_tab were classified as observation
  despite changing state, so read-only mode allowed them.
  `browser_screenshot_all_tabs` was too, despite activating and photographing
  every tab in every window.
- `enabled: false` returned before the scheme guard, so one config key
  re-enabled `file://` and `javascript:` navigation. The `.gov` pattern missed
  `irs.gov?x=1`, so a query string disarmed the guard.
- Headless mode's password guard was bypassed by omitting `selector` and
  typing into the focused element.
- The API token was written before `chmod` and an existing loose file was
  never tightened; `_check_auth` used `==` on a 64-character secret.
- Request and response bodies in network logs are now run through a
  credential-key scrubber, and `capture_bodies: false` suppresses request
  bodies too — it previously only suppressed responses.
- Tool results carrying page content are labelled as untrusted data. This
  narrows prompt injection; it does not solve it.

### Fixed
- **The response-body filter could hang a page.** Firefox keeps a response
  alive until the extension calls `close()` or `disconnect()`; the only
  `close()` was in `onstop`, `onerror` returned without releasing, and nothing
  handled a channel that delivers neither. With `include_all_types` that
  covers documents and scripts, so it could stall page loads. Every exit path
  now releases exactly once, with a watchdog.
- **Response bodies were probably never captured at all**:
  `filterResponseData` requires its listener registered with `"blocking"`.
- `wait_for_network_idle` was permanently poisoned for a tab by one timeout,
  and a WebSocket or long-poll made idle unreachable; requests older than
  `persistent_after` no longer block it.
- Redirects overwrote the first hop's entry and could attach two filters to
  one channel.
- Every `browser_click` fired twice, so non-idempotent handlers ran twice.
- The `text` selector was interpolated into an XPath expression unescaped, so
  a crafted label could redirect the click.
- `browser_get_tabs` silently ignored `current_window_only`, `url_pattern` and
  `include_favicon` — `camelize_args` renames them before dispatch.
- Content-script messages go to the top frame, so an ad or payment iframe can
  no longer answer `getText` or `getPageInfo` for the page.
- The stdio wrapper's 30s timeout was shorter than the server's 90s and 200s
  human waits, so approvals could not complete and a retry ran the action
  twice.
- `browser_navigate` reported the requested URL rather than where the tab
  landed, so a redirect left the guard checking the wrong page.
- `eval_chain` reported success for a failed chain and leaked a console
  listener per step; `wait_and_act` could re-fire a side-effecting action up to
  75 times.
- The inspect context menu used document coordinates against a viewport API;
  observer ids collided within a millisecond; `browser_scroll` reported
  success when its target did not exist; truncated results now say so.
- Release tooling: the Windows packager had no signed-build guard and would
  destroy a signed artifact; `publish-release.sh` verified neither the
  signature nor the version and ignored upload failures; the unsigned zip
  excluded only nested dotfiles; `updates.json` was written before the
  artifact existed and can now carry retired extension ids so older installs
  are not stranded; signing credentials no longer travel in argv.
- `uninstall.sh` said screenshots were preserved and then deleted them. Added
  `scripts/uninstall.ps1`, which did not exist.

### Changed
- Logs default to INFO with rotation at 5 MB, `0600` in a `0700` directory.
  `CLAUDE_BROWSER_DEBUG=1` and `CLAUDE_BROWSER_HOST_DEBUG=1` restore detail.
- `browser_safety_status` reports the mode and which tools are headless-only;
  the three headless-only tools say so in their descriptions.
- The extension's `author` and `homepage_url` name this fork, since it is
  signed and distributed from here; the description credits the original
  author where users see it, in `about:addons`.
- The agent definition documents the safety model, and no longer carries
  another project's context or encourages unprompted browser driving.

### Added
- 131 tests, up from 69: the safety guard's token binding, URL policy, tool
  classification, rate limiter and audit redaction; the stdio wrapper's
  untrusted-content fence and timeout ordering; the response filter's
  release-exactly-once paths; and the registration contract — the background
  harness previously discarded `addListener`'s `extraInfoSpec`, so stripping
  every entry kept the suite green while disabling the feature in Firefox.

## [1.5.1]

### Changed
- Attribution headers added to the source files that shipped without one
  (`popup.js`, `headless_backend.py`, `stdio_wrapper.py`,
  `claudecodebrowser_host.py`), so every file now carries the MIT line and
  credits the original author. `tests/test_extension_identity.py` enforces
  this, along with the credit in the README, the changelog and `LICENSE`.
- Documentation brought in line with 1.5.0: the three previously undocumented
  tools (`browser_find_tabs`, `browser_get_tab_info`,
  `browser_screenshot_all_tabs`), the new logging options, the credential-read
  guard, the file-location table, and a Development section covering the test
  suites and the packaging tasks.

Version bumped only because `popup.js` is part of the signed archive, and AMO
will not re-sign a version that already exists.

## [1.5.0]

First release of this fork (`acornelissen/ClaudeCodeBrowser`), building on
Andre Watson's 1.4.0.

The extension is renamed **ClaudeCodeBrowserX** and carries a fork-owned
extension ID (`{efac2f8e-6c88-4c94-a050-f45cd0298aeb}`), because AMO will not
let a different account sign under upstream's ID. It is therefore a separate
add-on: remove any earlier ClaudeCodeBrowser before installing this one, and
note that an older install will not auto-update to it.

The MCP server, native messaging host and install directory keep the
`claudecodebrowser` name, so existing `mcp__claudecodebrowser__*` tool names
and `~/.claudecodebrowser` paths are unchanged.

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

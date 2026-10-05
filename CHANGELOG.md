# Changelog

All notable changes to ClaudeCodeBrowserX are documented here. Versions follow
[semantic versioning](https://semver.org/). The MCP server, extension, and
docs are versioned together.

ClaudeCodeBrowser was created by Andre Watson
([@nanogenomic](https://github.com/nanogenomic), Ligandal Inc.); 1.1.0–1.4.0
are his releases. 1.5.0 onwards are from the fork at
<https://github.com/acornelissen/ClaudeCodeBrowserX>, since renamed
ClaudeCodeBrowserX.

## [Unreleased]

### Changed
- **Renamed to ClaudeCodeBrowserX.** Everything user-facing moves:
  - the MCP server is registered as `claudecodebrowserx`, so its tools are
    `mcp__claudecodebrowserx__*`;
  - the install folder is `~/.claudecodebrowserx`, and the native messaging
    host is `claudecodebrowserx`;
  - environment variables are `CLAUDE_BROWSERX_*`;
  - the repository is `acornelissen/ClaudeCodeBrowserX`. GitHub redirects
    the old URL, so installed extensions keep finding updates.

  The extension ID is unchanged, so Firefox treats it as the same add-on.

### Migrating
- Run `./scripts/install.sh` (or `install.ps1`). It moves
  `~/.claudecodebrowser` to the new folder with your `safety.json`, token and
  logs, writes the new native host manifest, and points the old one at the
  new host so an extension that has not updated yet still connects.
- Re-register the MCP server: `claude mcp remove claudecodebrowser`, then the
  `claude mcp add` line the installer prints. Update any permission rules
  that name `mcp__claudecodebrowser__*`.
- Rename `CLAUDE_BROWSER_*` variables to `CLAUDE_BROWSERX_*`. The old names
  still work for this release and log a deprecation warning once each.

## [1.9.7]

### Changed
- **The whole-page text scrub is shared too.** Masking credential fields
  inside a whole-page `browser_get_text` was a second pair of hand-kept
  copies, in `content.js` and the headless backend, and was where the
  50-field cap, the prefix-ordering and the 3-character cut-off bugs had
  lived. It is now `scrubNestedCredentials` in `credentials.js`, which both
  modes run.

### Fixed
- **A failed headless start no longer leaks the Playwright driver.** When the
  browser could not launch (a missing executable, say), `start()` raised
  without stopping the driver subprocess, so every failed attempt left one
  running; `stop()` likewise skipped it if closing the browser failed. Both
  now stop the driver. The install hint also names the configured engine
  instead of always Firefox.
- An empty credential field's `value=""` stays empty in captured HTML instead
  of becoming `value="***"`, matching the live read, which answers `null` for
  an empty field.

## [1.9.6]

### Changed
- **What counts as a credential is defined once,** in
  `extension/credentials.js`. It was three hand-kept copies - the field guard
  in `content.js`, the traffic scrubber in `background.js` and the headless
  backend - and most leaks found in three mutation passes were one copy
  behind another. The extension loads the file ahead of both scripts, and
  headless runs the same file in the page. The installers copy it next to
  the server; headless will not start without it.

### Tests
- **Real-browser tests in CI.** 19 end-to-end tests drive the headless
  backend against real Chromium on Linux: field locators, append/clear/Enter,
  credential refusals, clipboard keys, focus inside frames and shadow roots,
  whole-page text masking and masked reads. A missing browser fails the job
  rather than skipping it.

## [1.9.5]

### Fixed
- **A masked read says whether the field is filled again.** In Firefox,
  `browser_get_value` answered `***` for an empty credential field too, and
  in both modes a filled contenteditable credential read as empty, because
  only `.value` was checked. Filled now reads `***`, empty reads `null`.
- **Context-menu screenshots fall under retention.** The native host created
  a custom screenshots directory without the marker the server prunes by,
  and kept non-`.png` names, so those files were never removed.
- Headless `browser_get_text` reports `totalLength`, as Firefox does, instead
  of `total_length`.
- The MCP wrapper reported version 1.0.0; a test now holds every version
  string to the manifest. The unused `mcp_config.json` is gone.

### Security
- **Old installs keep an old, weaker `.gov` pattern.** `safety.json` is
  written once, at first run, so the `\.gov(/|$)` default that shipped from
  1.1.0 stayed in existing files after it was fixed - and it let
  `https://www.irs.gov?x=1`, `#a` and `:443/` skip the protected-site
  confirmation. The guard now recognises superseded defaults exactly and
  applies the fix in memory, logs a warning, and lists it under
  `pattern_upgrades` in `browser_safety_status`. The file is not rewritten,
  and patterns you wrote are never changed.

## [1.9.4]

### Security
- **Path parameters no longer reach the logs.** A session id carried in a URL
  path (`/x;jsessionid=...`) was written to `audit.jsonl` and the server log;
  the value of every `;name=value` path parameter is now masked there and in
  the agent client's output.
- **Card-expiry fields are scrubbed from captured HTML.** The body scrub knew
  five autocomplete tokens and the field guard eight; `cc-exp`,
  `cc-exp-month` and `cc-exp-year` were missing. A test now holds the two
  lists together.

### Changed
- `browser_safety_status` no longer advises turning script execution off when
  it already is.
- `updates.json` lists only the current extension id. Offering the build to
  the retired id could not work: Firefox refuses an update whose id changes,
  so those installs downloaded the package and failed every check.
- `./scripts/install.sh --headless` installs Playwright (pinned) and Chromium,
  so headless mode works after a normal install.
- Releases are cut only from a commit CI passed, and tagged on that commit.

### Removed
- The extension's unused native-host request path (`sendToNativeHost`).

### Docs
- The README, agent notes and tool descriptions were checked against the code
  and corrected: logging flags and methods that never existed are gone,
  console capture is described as what it is (page errors), network scrubbing
  and its limits are documented, and the MCP setup uses `claude mcp add`
  rather than `settings.json`, which Claude Code does not read for servers.

## [1.9.3]

### Security
- **Headless typed into password fields inside web components.** The
  focused-field check stopped at the shadow host, so a `<my-login>` wrapping
  `<input type=password>` passed it, and `browser_type` and
  `browser_press_key` with no selector typed into the password field
  (confirmed in real Chromium). The check now follows shadow roots and frames.
- **Captured traffic:** a relative redirect's path parameters
  (`Location: /cb;code=...`) were logged in clear, and a `>` inside a quoted
  attribute (`data-x="a>b"`) hid the rest of a tag, including the `name` that
  marks it as a credential, from the HTML scrub.

### Fixed
- `data-type="password"` on an ordinary field no longer gets its value masked
  in captured HTML.
- **More field names count as credentials:** `recoveryCodes`/`backup_codes`
  (two-factor recovery codes) and `cardCode` (the CVV), in all three copies of
  the pattern.

## [1.9.2]

### Fixed
- **Large results no longer trip Firefox's 1 MB limit.** The native host sent
  every result the extension returned straight back to the extension as well
  as to the server, so a big screenshot broke the host-to-extension limit on
  that pointless echo. Results now go to the server only; a 15 MB screenshot
  went through cleanly in a live check.
- **A command over the 1 MB limit now fails at once, with a reason.** Its
  failure used to go to the extension, which ignores it, so the call waited
  out the full timeout.
- **The native host could corrupt its own messages.** Two threads wrote the
  length and body of a message separately with no lock; interleaved, Firefox
  reads a corrupt frame and drops the connection.

## [1.9.1]

### Security
- **Captured traffic still leaked three things,** found in a live check of
  1.9.0: the OAuth code scrubbed from a redirect's `Location` came straight
  back in the next request's `Referer`; a captured HTML page kept
  `<input id="mfa" name="mfaCode" value="...">`, because only the first
  `name`/`id` on a tag was judged; and a credential `<textarea>`'s contents
  were never scrubbed.

## [1.9.0]

### Security
- **The agent could switch off credential masking itself.** The server set
  `allow_password` for six tools and passed it through for the rest, so
  `browser_get_text` with `allow_password: true` returned credential fields
  unmasked, and `allowPassword` worked on any tool in Firefox. Every spelling
  the agent sends is now dropped; only `safety.json` sets it.
- **`https:///evil.com/` got past anchored patterns.** The browser reads any
  run of slashes after `https:` as `//`; the guard saw a URL with no host, so
  `^https?://evil\.com` did not block it and an anchored protected pattern
  asked for no confirmation. A `url` that is not a string is now refused
  rather than ignored.
- **OAuth codes and session ids in redirects reached the agent.** A relative
  `Location: /cb?code=...`, a hash-route `#/callback?code=...`, a `Refresh`
  target and a `;jsessionid=` path parameter were all logged whole.
- **Headless:** Shift+Insert, Control+Insert and Shift+Delete pasted, copied
  or cut in a credential field; focus inside an iframe skipped the focused
  credential check (a cross-origin frame is now refused); and a whole-page
  `getText` masked only the first 50 nested credential fields, silently.
- **More field names count as credentials:** `mfaCode`, `verificationCode`,
  `securityCode`, `cc_number`/`ccNumber`, `creditCard`, `pincode` and
  `cookie`, in all three copies of the pattern. `cookieConsent` and similar
  are masked too.
- **Logs:** `url_pattern`, a non-string `url`, and in the agent client
  `requestedUrl`, `href` and `protectedUrl` were written whole.
- **The WebSocket handshake now rejects a browser `Origin`.** WebSockets are
  exempt from CORS, so any page you visit could open a connection to the
  loopback port; the API token refused it, but only after the handshake
  completed and a task had been held for up to 10s waiting for the first
  frame. Verified live: `Origin: https://evil.example` gets `HTTP 403` at the
  handshake, while a no-`Origin` local client still connects and is still
  token-checked. `CLAUDE_BROWSER_WS_ORIGINS` overrides the default.
  *(This was described in a code comment in an earlier release and never
  implemented; the comment read as though it were handled.)*
- **Logging a private-browsing tab is refused.** Nothing distinguished one, so
  a session there would have put request and response bodies into a buffer the
  agent reads — the single expectation a private window exists to uphold.
- **Screenshots are pruned**: 7 days and 500 files by default, both
  configurable, both settable to `0` to keep an indefinite record as a
  deliberate choice. One file was written per `browser_screenshot` call with
  `save_to_file` defaulting to true and nothing ever removing them, which made
  that directory the longest-lived record of your browsing in the project.
  **Pruning only touches a directory this project created** (marked with a
  `.ccb-screenshots` file). An adversarial review of the first version proved
  that `CLAUDE_BROWSER_SCREENSHOTS_DIR=~/Pictures` plus one screenshot
  unlinked four unrelated photographs — a privacy fix that silently destroys
  user data is a worse failure than the retention it closes. The headless
  backend writes through Playwright rather than the server's save path, so
  retention did not exist in headless mode at all; it does now.
- **`browser_find_tabs` requires a filter that narrows** and caps at 50. With
  no filter it returned every tab in every window, uncapped — the whole
  browsing surface, from a tool that reads like a search. `active: false` and
  `audible: false` passed the original check and matched essentially every
  tab, so one boolean defeated it. Use `browser_get_tabs` to list tabs
  deliberately.
- **A permit list is matched anchored; a refuse list stays loose.**
  `allowed_url_patterns` is the strongest confinement available and it was
  prefix-bypassable: `^https://localhost` also matched
  `https://localhost.evil.com/x` and `https://localhostile.io/`, so any
  attacker-controlled host with the right prefix opened the guard.
  `trusted_url_patterns` had the same hole in the other direction. A permit
  pattern must now cover a whole URL prefix ending at a delimiter, or match a
  whole hostname. **Breaking:** an unanchored mid-URL permit pattern such as
  `stripe\.com/dashboard` no longer grants anything and fails closed; write
  `^https://stripe\.com/` instead. `.*` still means everything.
- **An allowlist that cannot compile now permits nothing.** Wrapping each
  pattern in `(?:…)(?=…)` moves a leading `(?i)`/`(?s)`/`(?m)` off position 0,
  which Python rejects — so the pattern was logged and skipped, and because
  `allowed_url_patterns` is only consulted when non-empty, one such pattern
  **switched allowlist mode off entirely**. The strictest confinement on offer
  became none at all, recorded only in a log line the native host sends to a
  discarded stderr. A granting list with nothing usable left in it now keeps
  the restriction on and permits nothing, and `browser_safety_status` reports
  every failure in `pattern_errors`.
- **Userinfo no longer walks through a permit pattern.** `:` is both the
  allowlist's delimiter and the userinfo password separator, so
  `^https://localhost` granted `https://localhost:3000@evil.com/steal`, which
  loads evil.com — and the same URL counted as trusted, so a click on it
  needed no confirmation.
- **A percent-encoded host or a trailing dot no longer bypasses the
  protected-domain check or the blocklist.** The browser percent-decodes the
  host and keeps no trailing dot, so `https://%63hase.com/transfer`,
  `https://www.irs.%67ov/payments` and `https://www.irs.gov./payments` all
  reached protected sites with no confirmation, and `https://chase%2Ecom/x`
  walked past `blocked_url_patterns: ["chase\.com"]`.
- **Breaking:** a permit pattern that names a host must match the whole host —
  `example\.com` no longer covers `www.example.com`; write
  `(.+\.)?example\.com`. It fails closed and the denial says so. A permit
  pattern can also no longer be satisfied by the query string or fragment,
  which closes `https://evil.com/?x=a.stripe.com` against a
  `.*\.stripe\.com` pattern. Do not start a permit pattern with `.*` — the
  README used to recommend exactly that, and it is satisfied by a path segment
  on any host.
- **A backslash no longer walks past the protected-domain check.** Firefox
  loads `https://www.irs.gov\payments` as `https://www.irs.gov/payments`
  under WHATWG parsing, but the delimiter class did not match the raw string
  — so `browser_navigate` reached a `.gov` page with no approval and no
  token. URLs are normalised the way the browser parses them before any
  pattern sees them.
- **Credential redaction covers the shapes a real login uses.** The body
  scrubber matched only `"key":"string"`, and `requestBody.formData` — what
  Firefox hands over for an ordinary HTML form POST — is always
  array-valued, so a form login logged the password verbatim. There was no
  `formData` fixture in the test suite at all. Numeric OTPs, arrays of
  tokens, nested credential objects, multipart frames and `pwd=` were all
  unchanged too, and a credential straddling the 5000-character response cap
  survived as a fragment because the body was truncated before it was
  scrubbed. JSON bodies are now walked structurally, so every value shape is
  covered by construction — including below the walk's depth limit, which
  returned the raw subtree and so logged anything nested deeper than 12 in
  clear. The rebuilt text is used only when something was actually redacted,
  because re-serialising turns `12345678901234567890` into `…567000` and
  `1e400` into `null`.
- **The headless backend's credential guard was the extension's *pre-fix*
  one.** The widened guard below landed in one of the two implementations, so
  for everything except `<input type=password>` and the `autocomplete` tokens
  — `name=passwd`, `id=cvv`, `name="user[password]"`, `otpCode`, `apiKey`, a
  `<textarea>`, a `contenteditable`, `<sl-input type=password>` — headless
  returned the value from `browser_get_value` and wrote into it with
  `browser_type`. `browser_get_text` and `browser_get_elements` applied no
  credential mask there at all. The entries below, and the README's "in both
  attended and headless modes", were false for headless until now.
  The suite was green throughout because its parity test compared only the
  `autocomplete` token list, so every other dimension of the predicate could
  diverge silently; it is now a fixture table run through **both**
  implementations, with the extension's predicate lifted from its source
  rather than reimplemented.
- **The DOM credential guard looks at `name` and `id`, and beyond
  `<input>`.** It recognised only `input[type=password]` and the
  `autocomplete` token list, so `<sl-input type="password">` (the
  Shoelace/Ionic/Vaadin shape), `name="passwd"`, `cvv`, `otp`, `ssn` and a
  `<div contenteditable>` holding a PIN all came back in clear. The same
  field was `***` in the network log and plaintext from
  `browser_get_value` — the scrubber already knew those names.
- **A screenshot of a private-browsing window is never written to disk**, and
  `browser_screenshot_all_tabs` skips private windows. `browser_screenshot` of
  a background tab with `allow_focus: false` is refused rather than returning
  the *active* tab's image labelled as the requested one — Firefox captures
  the active tab of a window, so a screenshot of your open mail was being
  filed as a screenshot of some other page. The private-tab check
  also fails closed: it returned "not private" whenever the tab could not be
  inspected, which is how a private window's traffic reached the buffer.
- **Two read paths returned credentials in clear.** `browser_observe_element`
  reported a mutation's previous value raw, so any page rewriting the `value`
  *attribute* of a password or one-time-code input handed the old value to the
  agent — and because that tool counts as observation it is allowed in
  read-only mode and on a protected site with no confirmation, and the server
  never attached `allow_password` to it, so the guard was not consulted at
  all. `browser_scroll_and_capture` returned element text raw while using the
  same selector list as `browser_get_page_info`, which has always masked. The
  README's claim that "one function decides this for every read path" was
  false in exactly these two places, and that sentence is plausibly *why*
  per-commit review walked past them twice; it now names the set.
- **A credential in a URL reached the agent and the audit log.** The scrubber
  only ever saw request and response *bodies*, so the same `password=hunter2`
  pair was `***` in a POST body and verbatim in the GET URL beside it, along
  with a password-reset `?token=`, an OAuth `?code=`, an implicit-flow
  `#access_token=` and `https://user:pw@host/`. A redirect's `Location` header
  and the recorded redirect target had the same hole — an OAuth code sat next
  to a `Set-Cookie` that *was* redacted. Host and path are kept; query and
  fragment are parsed rather than regexed, since an encoded value can contain
  `&` and `=`.
- **`__proto__` exempted a whole captured body from redaction.** Building the
  scrubbed object with `{}` meant `out["__proto__"] = subtree` invoked the
  `Object.prototype` setter: the subtree became the prototype, `Object.keys()`
  came back empty, nothing was marked `***`, and the "return the original
  bytes when nothing was redacted" optimisation concluded there was nothing to
  do and returned the body verbatim. One attacker-chosen key name, reachable
  from any page because `JSON.parse` creates `__proto__` as an own property.
- **A page could stall the whole extension for minutes.** Moving the body
  scrub before the truncation (so a credential straddling the cut could not
  survive as a fragment) removed the only bound on what the regex passes saw —
  and the collection check runs *before* appending, so a single chunk lands in
  full and Firefox can deliver a whole response in one. The passes are
  quadratic on adversarial input: 5,000 characters of punctuation take 30ms,
  320,000 take two minutes and 1.3 MB takes half an hour, on the
  single-threaded background script
  that also services every tool call and the native port. Found by measuring
  rather than trusting the fix; the collected text is now hard-bounded, with
  enough margin to keep the straddle fix working.
- **`login()` and `fill_form()` abort the sequence on a refusal.** The first
  round of this guarded only the password step, so a refused *username* was
  followed by typing the password and pressing Enter, with the last result
  saying success — the account-lockout generator, with the fields swapped.
  `fill_form` checked a field's result only to decide whether to retry the
  locator, then submitted anyway.
- **A credential no longer travels in an error message.** The content script
  built `Element not found with options: {...}` from the whole options object,
  and `text` is the value being typed, so a failed `browser_type` put the
  password into the agent's log and action history. The extension now names
  only the locator, and the agent additionally scrubs the values it knows it
  sent out of any result text.
- **The agent no longer sends the API token off-machine.** `CLAUDE_BROWSER_URL`
  chooses the server with no validation, and the token is full control of the
  browser; it is now attached for loopback only unless
  `CLAUDE_BROWSER_ALLOW_REMOTE=1`. The agent also stopped printing the
  password in verbose mode, retaining it in the action history, and resending
  it after the browser refused the field.
- **`--command` is an allowlist.** It did `getattr(agent, name)` on whatever
  it was handed: `--command __init__` re-ran the constructor and wiped the
  action history while printing `null`.

### Fixed
- **Headless `browser_type` ignored `clear`, `press_enter` and `delay`.** It
  always replaced the field and never pressed Enter. It now appends unless
  `clear` is set, as Firefox does, and refuses a target that cannot take text.
- **Typing could outlast the call.** The per-key `delay` had no cap, so a long
  text kept typing after the server had given up waiting. It is now lowered
  so the whole text types within about 25s, and `delay: 0` means none.
- **`browser_type` ignored `id`, `name` and `placeholder`.** In Firefox,
  `findElement` read `text` as a locator before any of them, so the call
  searched the page for the text being typed rather than the named field. In
  headless mode only `selector` was read, so the text went into whatever had
  focus. All three now find the field they name, in both modes.
- An `X-API-Key` with a non-ASCII character dropped the connection instead of
  getting a 403: `compare_digest` raises on a non-ASCII `str`. It failed
  closed, so nothing was let through.
- The native host wrote a `.png` for **any** successful response carrying a
  `data` field, so page text and structured results landed on disk as junk
  files and real screenshots were duplicated (`server.py` already saves them).
- **Captured bodies honour the declared charset.** Everything was decoded as
  UTF-8, so a `windows-1252` or `shift_jis` page was logged as mojibake with
  no indication and the agent reasoned over corrupted text believing it was
  the page. An unknown charset label falls back to UTF-8 with a
  `charsetNote`, the streaming decoder is flushed so a multi-byte character
  split across the final chunk is not lost, and truncation at the
  5000-character cap is now flagged with the real length — a short body and a
  cut one previously looked identical.
- **A body is judged by whether it decoded, not by its `content-encoding`.** An
  intermediate version of the above refused any response carrying that header,
  which would have dropped bodies on essentially every real site: Firefox
  hands the stream filter decompressed bytes while the header remains present.
  Caught and corrected before release.
- `browser_observe_element` installed a full-subtree observer whose handle was
  released only by an explicit `browser_stop_observing`, so an agent that
  forgot left it running for the document's lifetime. It now expires after 5
  minutes (`maxLifetimeMs`), reports `expiresInMs`, and `stopObserving` says
  whether it had expired — so a truncated change list is not mistaken for a
  quiet page.
- `POST /browser/command` returned `{"success": true, "message": "Command
  queued"}` having queued nothing, telling its only caller the work had been
  accepted. It now returns `410`.
- The native host never reaped its spawned server, leaving a zombie per
  crash-and-restart cycle. The reap also raced the health-monitor thread and
  only ran on the next restart attempt — so after `MAX_RESTART_ATTEMPTS` the
  last dead child stayed a zombie for the host's life.
- **The native host no longer writes screenshots.** A real screenshot
  response *is* a `data:image/...` string, so the fix above removed only the
  junk-payload half: every genuine screenshot was still written twice, and
  the host's copy used plain `open()` — mode 0644, symlinks followed — under
  a name unrelated to the one the caller asked for. The server already writes
  it with `O_NOFOLLOW` and `0600`.
- **Native-messaging framing.** A single `read()` with no loop meant a short
  read on the pipe silently truncated a message, and a corrupt length prefix
  attempted a multi-gigabyte read. `read_message` now reads exactly and
  distinguishes EOF from one bad frame from a stream that is no longer
  aligned. An oversized outbound message — which Firefox drops *and* which
  tears down the port — sends a failure carrying the same `requestId`
  instead, so the waiter gets an answer.
- **A result no longer claims work that did not happen.** A content script
  that exists but answers nothing resolves the sender's promise with
  `undefined`, and `{success: true, ...undefined}` reported success:
  `browser_get_text` returned success with no text and `browser_click`
  reported a click nobody made. Likewise `wait_for_load: "false"` made
  `browser_refresh` block for its full 30-second timeout, `browser_refresh`
  reported a `bypass_cache` it had not used, `fill_form(submit=True)`
  returned a clean-looking result for a form it never submitted, `search()`
  reported success when the query was refused, `extract_links` returned `[]`
  for a refusal, and the headless `solveCaptcha` claimed human verification
  that never occurred.
- **The headless `scroll` ignored its entire schema.** It read `x`, `y`,
  `deltaX`, `deltaY`; the schema defines `direction`, `amount`, `selector`
  and `to_element`, so `browser_scroll(direction="up", amount=1000)` always
  wheeled 300px *down*. `waitAndAct` looped forever on
  `poll_interval_ms: 0`, and `timeout_ms: "15000"` raised a `TypeError`.
- **The headless credential guard was three divergent copies**, each with its
  own token list: `cc-exp-month` and `cc-exp-year` were missing from all
  three and `cc-exp` from one, so `browser_type` with a selector refused a
  card-expiry field while the same field refused by selector was typed into
  when focused. Both guards also failed *open* on any probe error — a
  timeout or a destroyed execution context mid-navigation disabled the check
  while the write went through. `browser_press_key` had no guard at all, and
  Playwright's `keyboard.press` really types, so it was a headless-only way
  to enter a credential one character at a time.
- **Transport failures are distinguishable from tool failures.** `HTTPError`
  subclasses `URLError`, so a rejected token read as "Connection failed: HTTP
  Error 403: Forbidden" and the user restarted a server that was never down.
- Feature flags arriving as JSON strings no longer fail open. `camelize_args`
  renames keys and coerces nothing, and nothing validates arguments on the
  path, so `"false"` was truthy: `browser_type {submit_form: "false"}`
  submitted the form, `save_to_file: "false"` wrote the PNG to disk anyway,
  and `include_data: "false"` embedded a base64 PNG per tab. Around fifty
  flags across the extension, the server, the headless backend and the
  content script now go through a lenient parser; `allow_password` stays a
  strict `=== true`, because a fail-closed switch must not be opened by a
  typo.
- Duplicate response headers collapsed onto one key, so two `Set-Cookie`
  headers became one with the last value — and almost every real response
  carries several.
- A JSON body claiming `charset=ISO-8859-1` was decoded as declared, turning
  `café` into `cafÃ©` with no note. RFC 8259 requires UTF-8 for
  `application/json` and says the parameter must be ignored.
- `observer_id` could not be reused although it is a documented parameter,
  and `generateSelector` returned selectors that throw when passed back —
  React and MUI ids look like `headlessui-menu-item-:r1:`, which needs
  escaping before `querySelector` will take it.
- `getPageInfo` could report 101 interactive elements and return none of
  them: the cap counted matches rather than collected elements.
- `safeElementValue` threw on a numeric `.value` (`<li>`, `<progress>`,
  `<meter>`, `<md-slider>`) *after* the click had been delivered, so an agent
  that retried acted twice.
- `reload_localhost(port=0)` meant "reload every localhost tab" rather than
  the one port asked for, and interactive mode spun forever on a closed
  stdin.
- A tab argument of `0` meant "no tab" at thirteen call sites, so a request
  naming that tab acted on whichever tab happened to be in front, and
  `tab_id: "7"` from a JSON client made `browser.tabs.get` throw inside
  Firefox.
- `requestBody.error` and a raw chunk carrying a `file` were both dropped, so
  a body Firefox could not read and a file upload each logged as a request
  with no body at all.
- `tab_id: "1"` reached the headless backend uncoerced, which keys its tab map
  by integer, so it reported "No headless tab with id 1" for a tab that was
  open.
- `browser_get_tabs(limit=12.7)` silently gave you the 50-tab default — a
  wider reach than asked for, from the tool whose purpose is the cap — and
  `limit: Infinity`, which `json.loads` accepts, propagated out as a
  traceback instead of a result.
- Credential redaction was *over*-reaching as well as under-reaching: `auth`
  matched `author`, so every captured API response had its whole author
  object replaced with `***`, and `session`, `pass` and `otp` hid
  `sessionCount`, `bypassCache` and `notPublished`. A log that eats the
  fields you were reading is a log you turn the scrubber off for. The short
  entries are anchored now, and the extension's two copies of the list are
  pinned identical by test — their drift was the reason a field could read
  `***` in the network log and plaintext from `browser_get_value`. Anchoring
  alone *lost* ten credential names (`passkey`, `otpCode`, `oauth_verifier`,
  `authz`, `sessionValue` …), because a camelCase hump is not a separator;
  names are matched through a step that normalises camelCase first, and a
  test asserts no future tightening may drop a name the loose pattern
  caught.
- "Take Screenshot for Claude" in the context menu has never done anything.
  It posted an action the native host has no handler for, so the message fell
  through to `/browser/command`.

### Changed

- **`browser_get_value` on a checkbox or radio returns its boolean state**,
  not the submit string. It returned `"on"` whether or not the box was
  ticked, so an agent reading a consent box or a radio group learned nothing;
  the submit string is still reported alongside as `submitValue`.
- **`browser_set_value` now errors rather than claiming success** when it
  cannot set the element: it fell through to an unconditional `set: true` for
  anything that was not an input, textarea, select or contenteditable, so it
  reported success on a `<div>` having changed nothing. It now also sets a
  value-bearing custom element (`<sl-input>` and friends, which are what an
  agent has to target on Shoelace/Ionic/Vaadin pages) and reads the value
  back, refusing when the assignment did not take. On a checkbox or
  `<select>` It assigned `.value` blind and reported `{set: true}`
  having changed nothing, and `browser_select_option` reported success for an
  option that does not exist — where a real `<select>` resets to `''`, so the
  caller believed a choice had been made and the form was submitted empty.
- **`browser_get_text` returns visible text.** It fell back from `innerText`
  to `textContent` unconditionally, and `textContent` includes
  `display:none` content, so a hidden template was returned from a tool whose
  description promises what is visible. A new `source` field says which was
  read.
- `getComputedStyles`' three camelCase defaults (`backgroundColor`,
  `fontSize`, `fontFamily`) were permanently empty, because
  `getPropertyValue` takes a CSS property name. Callers may still pass
  camelCase; the result is keyed by whatever they asked for.

- **The headless screenshot followed a symlink**, truncating the target at
  mode 0644, while the attended path and the native host had both been
  hardened to `O_NOFOLLOW` and `0600` — the headless branch wrote through
  Playwright and never went through the hardened writer. Headless also
  ignored `save_to_file` entirely, and `browser_press_key`'s credential guard
  was bypassed by Playwright key *codes*: `KeyA`, `Digit1`, `Space`,
  `Shift+a` and `Control+v` all type a character and none of them has
  `len(key) == 1`, so 18 of them reached a password field.
- **SIGTERM or SIGINT deadlocked the native host permanently**, because the
  child-process list was guarded by a non-reentrant lock and Python runs
  signal handlers on the main thread — so a signal arriving mid-reap
  self-deadlocked, and the process then ignored SIGTERM and SIGINT and needed
  SIGKILL. Firefox SIGTERMs native hosts when the port closes, so these
  accumulated.
- **One slow headless call wedged the whole backend.** `browser_wait_and_act`
  took an unbounded `timeout_ms`, and the server abandoned the call at 35s
  without cancelling it — so the coroutine kept the backend lock for the life
  of the process and every later headless tool blocked. The timeout is capped
  (and the result says when the cap bit), and an overrunning call is now
  cancelled.

- **`browser_get_console_logs` promised a headless capture that does not
  exist.** Its description said headless "captures the page console in full";
  the headless backend has no `getConsoleLogs` action at all, so the call
  fails with `Unsupported headless action`, and an agent that believed the
  description read that error as "the page logged nothing". The same false
  sentence was in the extension's own result note and in a shipped changelog
  entry. `browser_inject_observer` likewise pointed at
  `browser_get_console_logs` to read a buffer nothing has ever read: it is
  `window.__ccb_mutations` in the page, fetched with
  `browser_execute_script`, and it holds DOM mutations, not console output.
- The standalone agent client had a **third** copy of the redaction list, and
  it was the one left behind — `key` and `url` had been added to the shared
  definition and never to it, so `browser_press_key` printed the key and
  `browser_navigate` printed a URL with its token. Its key masking also ran
  only at the top level and inside `element`, so a credential the *page* held
  under a nested key (`browser_get_elements` returns a list of them) was
  caught only if the client had sent it. A test now pins the mirror against
  the shared list.

### Tests

**Nothing in the suite ever executed the headless credential guard's
JavaScript.** The test double was a Python reimplementation that answered from
*substrings* of the production script, and several parity tests were source
greps — so sabotaging the guard to answer "not a credential" for a password
input, which would let every credential be filled, left the suite green. The
project's main credential defence had no test that it works. The real
predicate is now run through `node` against element literals, with the Python
double's answers compared to node's so it cannot drift; the same sabotage now
fails 35 assertions.

The suite also cleared two `CLAUDE_BROWSER_*` variables, so it gave a
different answer depending on what you had exported — and
`CLAUDE_BROWSER_HEADLESS` *hung* it, waiting out the headless startup timeout
for a browser that was never coming. All of them are cleared now.

`headless_backend.py`, `agent/browser_agent.py`, `getText`,
`getComputedStyles`, `_save_screenshot`, the native host's framing and the
attended human-approval branch had no tests at all. The suite is now 601
Python tests plus 237 JavaScript ones, with every fix above shown to fail
against the source it replaced. Several fixtures were found to be hiding the
bugs they were meant to cover: `attachShadow()` discarded its argument, so
changing the approval prompt's shadow root from `closed` to `open` — which
would let the page read it and click its buttons — kept every test green.

**A mutation run found the server's authentication had no real test.** With
HTTP auth switched off, with the token reduced to a prefix match, and with
the WebSocket handshake accepting anyone or failing open, the suite stayed
green: every endpoint test stubbed the check out. The handlers are now driven
over real sockets. The same run found guards nothing would notice breaking:
the native host's check that the listener on 8765 is really ours, its promise
not to log payloads, the approval page (`approve.js`, untested until now), the
`=== true` approval decision, the second policy pass after a human approves,
camelCase credential names in `content.js`, and several caps and file modes.
Each now fails a test when broken, and five tests that grepped the source or
could not fail for other reasons were rewritten to check behaviour.

## [1.8.0]

### Fixed
- **`browser_get_console_logs` never returned the page's console output**, and
  said it was working. A content script has its own `console`, separate from
  the page's, so it only ever captured the extension's own output — including
  anything `browser_execute_script` printed. Confirmed live. The result now
  states `capturesPageConsole: false` and warns that an empty result does not
  mean the page logged nothing.
- **Page errors and unhandled rejections are now captured**, which is the
  subset that matters for debugging and is reachable without touching page
  globals: they arrive as DOM events on `window`. Tagged `source: "page"`;
  extension output is tagged `source: "extension"`.
- **A logging session is scoped to the page it started on.** It was keyed by
  tab id, which outlives navigation — so starting a session on a dev server
  and then navigating that tab to a bank captured the bank's request and
  response bodies. A cross-origin top-level navigation ends the session;
  same-origin and subframe navigation do not.
- **`browser_screenshot(full_page=true)` has never worked** —
  `captureFullPage` returned page dimensions, which the server then treated as
  a data URL. It now returns the visible capture with
  `fullPageCaptured: false` and points at `browser_scroll_and_capture` or the
  headless backend.
- `browser_reload_all` defaulted `bypass_cache` to false while its schema
  documented true.
- `filterAttached`, internal bookkeeping, no longer appears in results.

### Note
Headless mode (Playwright) does not capture the page console either: it does
not implement `browser_get_console_logs` at all, and the call fails with
`Unsupported headless action: getConsoleLogs`. *(Corrected after release —
this entry originally claimed headless captured it in full, which was never
true. See the Unreleased section.)*

## [1.7.2]

### Fixed
- **Credential values in captured HTML bodies.** With `include_all_types`,
  document bodies are captured, and the body scrubber only understood JSON
  keys and form-encoded pairs — so a server-rendered form was logged as
  `<input type="password" value="SuperSecret123!">` while
  `browser_get_page_info` was correctly masking the same field. Credential-
  looking `<input>` tags now have their `value` attribute blanked, in quoted
  and unquoted forms. Ordinary fields, links and markup are untouched.
- **Queued commands no longer replay.** A command the caller has already
  timed out on sat in the queue indefinitely and executed whenever the browser
  next reconnected — harmless for a read, an unrequested action for a click or
  a type. Commands now expire after `COMMAND_QUEUE_TTL` (240s, above the
  longest human-approval wait).

Both were found by live testing, and neither could have been caught by the
suite as written: the fixtures used well-formed JSON and clean booleans while
the browser sends markup and strings.

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

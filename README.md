# ClaudeCodeBrowser

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-1.9.4-blue.svg)](https://github.com/acornelissen/ClaudeCodeBrowser/releases)
[![Firefox Add-on](https://img.shields.io/badge/Firefox-Add--on-FF7139?logo=firefox-browser)](https://addons.mozilla.org/firefox/)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-8A2BE2.svg)](https://modelcontextprotocol.io)
[![Python](https://img.shields.io/badge/python-3.8%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey.svg)](#installation)

A browser automation system for Claude Code that enables AI-powered interaction with web pages — with **built-in safety guards for humans**. Take screenshots, click elements, type text, navigate pages, and **force refresh browser tabs** when launching development servers. Drive your real Firefox through the extension, or run fully headless (Firefox, Chromium, or WebKit) via Playwright.

The browser extension installs as **ClaudeCodeBrowserX**; the MCP server, native
messaging host and install directory keep the `claudecodebrowser` name.

## Credits

**Originally created by Andre Watson** ([@nanogenomic](https://github.com/nanogenomic)) — dre@ligandal.com
**Organization:** [Ligandal Inc.](https://ligandal.com) · <https://github.com/nanogenomic/ClaudeCodeBrowser>
**License:** MIT · **Copyright:** 2025 Andre Watson (nanogenomic), Ligandal Inc.

This repository is a **fork** maintained by Albert Cornelissen
([@acornelissen](https://github.com/acornelissen)). All of the original design
and implementation is Andre's work; the fork's changes are listed from 1.5.0
onwards in [the changelog](CHANGELOG.md), and it is distributed under the same
MIT license with the original copyright retained.

## Features

- **Screenshots** - Capture the visible area (full page in headless mode only)
- **Click Automation** - Click elements by CSS selector, XPath, text, or coordinates
- **Typing** - Type text into inputs with simulated keystrokes
- **Page Navigation** - Navigate to URLs, create/close/focus tabs
- **Page Refresh** - Force refresh tabs after server restarts (bypass cache)
- **Element Inspection** - Find elements, get page info, highlight elements
- **JavaScript Execution** - Run arbitrary JS in browser context
- **Console & Network Logging** - Capture network traffic (fetch, XHR, WebSocket, beacons) and page errors for debugging. In Firefox the page's own `console.log` calls are not visible (see *Console & Network Logging* below). Attended mode only
- **Safety Guards** - URL restrictions, protected-site confirmation, read-only mode, rate limiting, credential-field protection, audit log ([details](#safety-guards))
- **Headless Mode** - Unattended automation via Playwright: Firefox, Chromium, or WebKit
- **MCP Integration** - Model Context Protocol server for Claude Code

## Browser Support

| Browser | Attended (extension) | Headless (Playwright) |
|---------|---------------------|----------------------|
| Firefox | ✅ Primary target (Manifest V2, AMO-signable) | ✅ `CLAUDE_BROWSER_ENGINE=firefox` (default; not on macOS 27 from a terminal, see [Headless Mode](#headless-mode)) |
| Chromium / Chrome | 🧪 Experimental build via `scripts/build-chrome.sh` (MV3) | ✅ `CLAUDE_BROWSER_ENGINE=chromium` |
| WebKit (Safari engine) | — | ✅ `CLAUDE_BROWSER_ENGINE=webkit` |

Attended mode drives your real browser with your logged-in sessions. Headless mode launches a fresh, isolated browser — best for CI, servers, and unattended tasks. See [Headless Mode](#headless-mode).

## Overview

ClaudeCodeBrowser consists of four main components:

1. **Firefox WebExtension** (*ClaudeCodeBrowserX*) - Runs in the browser to execute automation commands
2. **Native Messaging Host** - Bridge between the extension and local server
3. **MCP Server** - Model Context Protocol server exposing browser automation tools
4. **Browser Agent** - Python agent for high-level browser automation

## Architecture

### Dual-Server Design

ClaudeCodeBrowser uses a **dual-server architecture** for maximum reliability and flexibility:

| Server | Port | Protocol | Purpose |
|--------|------|----------|---------|
| **HTTP Server** | 8765 | HTTP REST | MCP tool calls, health checks, command polling, screenshot retrieval |
| **WebSocket Server** | 8766 | WebSocket | Optional browser channel; the shipped extension does not use it |

**Why Two Servers?**
- **HTTP (8765)**: The channel in use. Claude Code's MCP client sends tool requests here, and the native messaging host polls it every 500ms for pending commands and posts the extension's results back.
- **WebSocket (8766)**: Accepts a token-authenticated browser client and uses it in preference to polling when one is connected. Nothing in this repository connects to it: the Firefox extension talks only to the native host.

The WebSocket handshake refuses a browser `Origin` outright. WebSockets are
exempt from CORS, so any page you visit could otherwise open a connection to
the loopback port — the API token refused it, but only after the handshake
had completed. A local client sending no `Origin` header still connects and
is still token-checked. Override with `CLAUDE_BROWSER_WS_ORIGINS`:

| Value | Effect |
|-------|--------|
| unset | No-`Origin` clients only. The default. |
| `moz-extension://abc,https://my.app` | Those origins **in addition to** no-`Origin` clients. |
| `*` | Check disabled. The API token is then the only control; a warning is logged. |

### Communication Flow

```mermaid
flowchart TB
    CC["Claude Code<br/>(MCP client)"] -->|"stdio (MCP)"| SW["stdio_wrapper.py"]
    SW -->|"HTTP + X-API-Key"| SRV["MCP Server<br/>HTTP :8765 / WebSocket :8766"]
    SRV --> GUARD{{"Safety Guard<br/>URL rules · confirm tokens · rate limit · audit log"}}
    GUARD -->|attended| QUEUE["Command queue<br/>(in-memory)"]
    GUARD -->|"headless mode"| PW["Playwright<br/>Firefox / Chromium / WebKit"]
    QUEUE <-->|"500ms polling"| NH["Native Host<br/>(stdio)"]
    NH <--> EXT["Firefox Extension<br/>(background + content scripts)"]
    EXT --> PAGE["Web Page"]
    PW --> PAGE2["Web Page (headless)"]
```

### Request Flow (Step by Step)

1. **Claude Code → MCP Server**: Tool call over stdio to `stdio_wrapper.py`, which POSTs it to `localhost:8765/mcp/call`
2. **MCP Server → Command Queue**: Command is queued with unique ID
3. **Native Host → MCP Server**: The native host polls `localhost:8765/browser/poll` every 500ms
4. **Native Host → Extension**: The pending command is passed to the extension over native messaging
5. **Extension → Web Page**: Command executed (screenshot, click, type, etc.)
6. **Extension → Native Host → MCP Server**: The result goes back over native messaging and the host posts it to `localhost:8765/browser/response`
7. **MCP Server → Claude Code**: Result returned to original MCP call

In headless mode the server drives Playwright directly and none of steps 2–6 apply.

### Native Host

The native messaging host (`claudecodebrowser_host.py`) is how the Firefox
extension reaches the server; the extension makes no HTTP or WebSocket
connection of its own.
- Polls the server for commands and relays them to the extension, and posts
  the extension's results to the server. Results go to the server only, not
  back to the extension.
- Firefox drops any message over 1 MB from the host to the extension. A
  command over that size is not sent: the call fails at once with a reason
  rather than waiting out its timeout. Results travel the other way, where
  the host accepts up to 64 MB, so a large screenshot is fine.
- Saves the screenshot taken from the context menu's *Take Screenshot for
  Claude*. Screenshots from `browser_screenshot` are saved by the server, at
  `~/.claudecodebrowser/screenshots/`, created `0700` (override with
  `CLAUDE_BROWSER_SCREENSHOTS_DIR`), pruned after 7 days or 500 files — see
  [Screenshot retention](#screenshot-retention)
- Starts the MCP server when it is not running, and restarts it if it dies.
  Before trusting whatever is on port 8765 it requires proof that the listener
  holds the shared API token, so a process that squats the port cannot receive
  the token or issue browser commands. It only ever terminates a Python
  process whose script argument is our own server, re-checked immediately
  before each signal; anything else holding the port is left alone and
  reported as a failure

## Installation

### Prerequisites

**Python 3.** Nothing else is required for attended mode.

**System Python websockets** (optional, for the WebSocket server on port 8766):
```bash
sudo apt install python3-websockets      # Linux
python3 -m pip install websockets        # macOS
```

Use `python3 -m pip`, not `pip3`: on a machine with both mise and Homebrew
Pythons they can be different interpreters, and the server only sees the one
`python3` resolves to.

Without it the server runs HTTP-only, which is all the Firefox extension
uses. `browsers_connected` (in `/health` and `browser_safety_status`) counts
WebSocket clients only, so it reads 0 with the extension connected either
way.

### Quick Install (Linux and macOS)

```bash
cd ClaudeCodeBrowser
./scripts/install.sh
```

### macOS notes

The install script handles these automatically, but if you are installing manually:

- The native messaging manifest goes in `~/Library/Application Support/Mozilla/NativeMessagingHosts/` (not `~/.mozilla/`).
- The native host must live outside TCC-protected folders (`~/Documents`, `~/Desktop`, `~/Downloads`). Firefox is not allowed to execute anything there and fails with `Operation not permitted`. The default install location `~/.claudecodebrowser` is fine.
- The manifest should point to a wrapper script with an absolute `python3` path. Firefox launches native hosts with a minimal PATH, so `#!/usr/bin/env python3` may not resolve (e.g. Homebrew installs).

### Manual Installation

1. **Install the MCP server and agent:**
   ```bash
   mkdir -p ~/.claudecodebrowser/{native-host,mcp-server,agent,screenshots,logs}
   cp native-host/* ~/.claudecodebrowser/native-host/
   cp mcp-server/* ~/.claudecodebrowser/mcp-server/
   cp agent/* ~/.claudecodebrowser/agent/
   chmod +x ~/.claudecodebrowser/**/*.py
   ```

2. **Install native messaging manifest for Firefox:**

   `native-host/claudecodebrowser.json` ships with a placeholder `path`.
   Firefox needs a real absolute path there and does **not** expand `~` or
   `$HOME`, so substitute it while copying:

   ```bash
   # Linux
   mkdir -p ~/.mozilla/native-messaging-hosts
   sed "s|/ABSOLUTE/PATH/TO/HOME|$HOME|" native-host/claudecodebrowser.json \
     > ~/.mozilla/native-messaging-hosts/claudecodebrowser.json
   # macOS
   mkdir -p ~/Library/Application\ Support/Mozilla/NativeMessagingHosts
   sed "s|/ABSOLUTE/PATH/TO/HOME|$HOME|" native-host/claudecodebrowser.json \
     > ~/Library/Application\ Support/Mozilla/NativeMessagingHosts/claudecodebrowser.json
   ```

   (`scripts/install.sh` writes this file for you, with the path already
   resolved — on macOS it points at a small launcher that pins the absolute
   `python3`, because Firefox starts native hosts with a minimal `PATH`.)

3. **Install the Firefox extension:**
   - Open Firefox and go to `about:debugging`
   - Click "This Firefox"
   - Click "Load Temporary Add-on..."
   - Select `extension/manifest.json`

4. **Configure Claude Code MCP:**
   Claude Code does not read MCP servers from `settings.json`. Add it for
   every project (stored in `~/.claude.json`):
   ```bash
   claude mcp add --scope user claudecodebrowser -- \
       python3 ~/.claudecodebrowser/mcp-server/stdio_wrapper.py
   ```
   Or for one project, in that project's `.mcp.json`:
   ```json
   {
     "mcpServers": {
       "claudecodebrowser": {
         "command": "python3",
         "args": ["${HOME}/.claudecodebrowser/mcp-server/stdio_wrapper.py"]
       }
     }
   }
   ```

### Windows Installation

**Quick install (PowerShell):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

This copies the components to `%USERPROFILE%\.claudecodebrowser`, generates the
`.bat` native-host wrapper with your Python path baked in, writes the native
messaging manifest, and registers it in the Windows registry. Then load the
extension (step 1 below) and add the printed MCP config to Claude Code.

**Manual steps:**

1. **Install the Firefox extension:**
   - Open Firefox and go to `about:debugging`
   - Click "This Firefox"
   - Click "Load Temporary Add-on..."
   - Select `extension/manifest.json`

2. **Register the native messaging host:**
   Update the path in `native-host/claudecodebrowser.json` to point to the `.bat` wrapper:
   ```json
   {
     "path": "C:\\path\\to\\ClaudeCodeBrowser\\native-host\\claudecodebrowser_host.bat"
   }
   ```
   Then register it in the Registry:
   ```powershell
   New-Item -Path 'HKCU:\Software\Mozilla\NativeMessagingHosts\claudecodebrowser' -Force | Out-Null
   Set-ItemProperty -Path 'HKCU:\Software\Mozilla\NativeMessagingHosts\claudecodebrowser' -Name '(Default)' -Value 'C:\path\to\ClaudeCodeBrowser\native-host\claudecodebrowser.json'
   ```

3. **Configure Claude Code MCP:**
   Claude Code does not read MCP servers from `settings.json`. Add it for
   every project (stored in `%USERPROFILE%\.claude.json`):
   ```powershell
   claude mcp add --scope user claudecodebrowser -- python C:/path/to/ClaudeCodeBrowser/mcp-server/stdio_wrapper.py
   ```
   Or for one project, in that project's `.mcp.json`:
   ```json
   {
     "mcpServers": {
       "claudecodebrowser": {
         "command": "python",
         "args": ["C:/path/to/ClaudeCodeBrowser/mcp-server/stdio_wrapper.py"]
       }
     }
   }
   ```

## Updating the Firefox Extension

The MCP server and native host are plain Python — pull the repo and restart
them and you're current. **The browser extension is separate**: it runs
inside Firefox and does not update from a `git pull`. How you update it
depends on how it was installed.

Build a versioned package (both scripts also emit `dist/updates.json` for
auto-update — see below):

```bash
# Linux / macOS
./scripts/package-extension.sh          # dist/claudecodebrowser-<version>.xpi
./scripts/package-extension.sh --sign   # signed via AMO (see below)
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\package-extension.ps1
powershell -ExecutionPolicy Bypass -File scripts\package-extension.ps1 -Sign
```

**1. Temporary add-on (development).** If you loaded it through
`about:debugging` → *Load Temporary Add-on*, it is not persistent and does
not auto-update:

- Open `about:debugging#/runtime/this-firefox`
- Find ClaudeCodeBrowserX and click **Reload**, or remove it and load the new
  `manifest.json`/`.xpi` again
- It is removed on Firefox restart, so you reload it each session

This is the quickest loop while developing, and it's where you are if you've
been "reloading the plugin" after each change. When the manifest gains a new
permission (v1.3.0 added `notifications`), a reload picks it up.

**2. Signed, self-distributed `.xpi` (recommended for real use).** A signed
extension installs permanently and can **auto-update**. Sign it through
Mozilla without a public listing:

1. Get the tooling. `web-ext` is pinned in `mise.toml`, so:
   ```bash
   mise install
   ```
2. Create an AMO API key at
   <https://addons.mozilla.org/en-US/developers/addon/api/key/> — "JWT
   issuer" and "JWT secret". **The secret is shown once.** Put both in
   `mise.local.toml`, which is gitignored:
   ```bash
   cp mise.local.toml.example mise.local.toml
   $EDITOR mise.local.toml
   ```
   mise exports them as `AMO_JWT_ISSUER` / `AMO_JWT_SECRET` for the signing
   script. Keep them out of `mise.toml` and out of your shell history.
3. Sign:
   ```bash
   mise run sign      # = ./scripts/package-extension.sh --sign
   ```
   This uploads to AMO's signer with `--channel=unlisted` (no public
   listing) and writes a signed `.xpi` plus `updates.json` to `dist/`.
4. Install the signed `.xpi` in Firefox: `about:addons` → gear →
   *Install Add-on From File*.

> **Never delete the add-on on AMO.** AMO keeps a deleted add-on's ID on a
> denylist, so it can never be signed under again — a later `web-ext sign`
> fails with `Conflict: Duplicate add-on ID found.` and the only way forward
> is a brand-new ID, which means a new add-on identity plus matching updates
> to `allowed_extensions` everywhere. Builds you already signed keep working
> (Firefox verifies the signature against Mozilla's CA offline, and
> `update_url` points at GitHub, not AMO), but you cannot ship another
> version under that ID. Leave unwanted add-ons in place instead.
>
> **If signing fails with `Getting details failed: Not Found`.** That is
> web-ext failing *after* the upload has already validated cleanly, when it
> tries to attach the new version to the existing add-on. Adding a version
> addresses the add-on by GUID, and AMO can answer 404 there — on both the
> submission API (`POST /addons/addon/{guid}/versions/`) and the signing API
> (`PUT /addons/{guid}/versions/{version}/`). Signing later versions normally
> works fine, so when this does happen suspect the add-on's state on AMO
> rather than the tooling: open
> <https://addons.mozilla.org/developers/>, check whether it is listed as
> incomplete, and either finish it there or upload the `.xpi` through that
> page.
>
> **Extension ID.** This fork uses a generated GUID, not upstream's
> `claudecodebrowser@ligandal.com`: AMO rejects a submission under an ID
> registered to a different account. If you fork this in turn you will need
> your own ID again — change `browser_specific_settings.gecko.id` and
> `allowed_extensions` in `native-host/claudecodebrowser.json`. Both
> installers read the ID from the manifest, so there is nothing else to edit,
> and `tests/test_extension_identity.py` checks the two agree. Get it wrong
> and native messaging fails silently.

**Auto-update is already wired** to GitHub Releases. The manifest carries:

```json
"update_url": "https://github.com/acornelissen/ClaudeCodeBrowser/releases/latest/download/updates.json"
```

That's a stable URL — it always resolves to the newest release's
`updates.json` — and the packaging scripts generate that `updates.json`
pointing at the matching versioned `.xpi`. So each new plugin version is just:

1. Bump `version` in `extension/manifest.json`.
2. `./scripts/package-extension.sh --sign` (or the `.ps1` on Windows) — writes
   the signed `claudecodebrowser-<version>.xpi` **and** `updates.json` into
   `dist/`.
3. Publish the release with both assets — one command:
   ```bash
   ./scripts/publish-release.sh            # or --draft to review before publishing
   ```
   It reads the version from the manifest, creates the `v<version>` release,
   and uploads the `.xpi` + `updates.json`. It uses the `gh` CLI if present
   (`gh auth login`), otherwise falls back to the GitHub API with
   `GITHUB_TOKEN` (needs `repo` scope).

   <details><summary>Prefer to run it by hand?</summary>

   With the `gh` CLI:
   ```bash
   VER=$(python3 -c "import json;print(json.load(open('extension/manifest.json'))['version'])")
   gh release create "v$VER" \
     "dist/claudecodebrowser-$VER.xpi" "dist/updates.json" \
     --title "v$VER" --notes "ClaudeCodeBrowser v$VER"
   ```

   With `curl` (set `GITHUB_TOKEN`):
   ```bash
   VER=$(python3 -c "import json;print(json.load(open('extension/manifest.json'))['version'])")
   REPO=acornelissen/ClaudeCodeBrowser
   ID=$(curl -sS -X POST "https://api.github.com/repos/$REPO/releases" \
     -H "Authorization: Bearer $GITHUB_TOKEN" \
     -d "{\"tag_name\":\"v$VER\",\"name\":\"v$VER\"}" | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
   curl -sS -X POST "https://uploads.github.com/repos/$REPO/releases/$ID/assets?name=claudecodebrowser-$VER.xpi" \
     -H "Authorization: Bearer $GITHUB_TOKEN" -H "Content-Type: application/octet-stream" \
     --data-binary @"dist/claudecodebrowser-$VER.xpi"
   curl -sS -X POST "https://uploads.github.com/repos/$REPO/releases/$ID/assets?name=updates.json" \
     -H "Authorization: Bearer $GITHUB_TOKEN" -H "Content-Type: application/json" \
     --data-binary @"dist/updates.json"
   ```
   </details>

Installed copies check `releases/latest/download/updates.json`, see the higher
version, and update themselves within ~24h — or immediately via *Check for
Updates* in `about:addons`. (Format reference: Mozilla's
[updateURL manifest](https://extensionworkshop.com/documentation/manage/updating-your-extension/).)

> Forking? Set `CCB_REPO_SLUG=youruser/yourrepo` when packaging so the
> generated `update_link` points at your releases, and change the `update_url`
> in the manifest to match.

**3. Public AMO listing.** To distribute on
[addons.mozilla.org](https://addons.mozilla.org), run
`web-ext sign --channel=listed` (or submit the `.xpi` in the Developer Hub)
and go through Mozilla's review. Users then install and update like any store
add-on. Best when you want the extension discoverable; heavier because each
version is reviewed.

> Whichever route: bump `version` in `extension/manifest.json` first (it must
> increase for Firefox to treat a build as an update), keep it in step with
> the server version, then repackage.

## Usage

### Starting the MCP Server

```bash
~/.claudecodebrowser/start-server.sh
```

Or directly:
```bash
python3 ~/.claudecodebrowser/mcp-server/stdio_wrapper.py
```

The server runs on:
- HTTP: http://127.0.0.1:8765
- WebSocket: ws://127.0.0.1:8766

### Headless Mode

Run without any visible browser — ideal for CI, servers, and unattended tasks.
The installer can set it up: `./scripts/install.sh --headless` installs
Playwright (pinned to the tested version) and Chromium into the Python the
server runs under, and prints the two settings to add to the server's `env`.
By hand:

```bash
python3 -m pip install playwright
python3 -m playwright install firefox   # or: chromium / webkit

CLAUDE_BROWSER_HEADLESS=1 python3 mcp-server/server.py
```

> **macOS 27: use Chromium.** Firefox is the default engine, but on macOS 27
> it cannot start when the server was launched from a terminal (or from
> Claude Code): the OS's app data protection denies such processes access to
> `~/Library/Application Support/Firefox`, and Firefox exits with `Could not
> find profile folder`. Set `CLAUDE_BROWSER_ENGINE=chromium`. Granting the
> terminal access to other apps' data would work round it, but would also
> expose your browser cookies to the agent's shell.

Pick the engine with `CLAUDE_BROWSER_ENGINE`:

```bash
CLAUDE_BROWSER_ENGINE=chromium CLAUDE_BROWSER_HEADLESS=1 python3 mcp-server/server.py
CLAUDE_BROWSER_ENGINE=webkit   CLAUDE_BROWSER_HEADLESS=1 python3 mcp-server/server.py
```

To use a browser you already have (a system install, or a Playwright build at
a different revision) instead of running `playwright install`, point
`CLAUDE_BROWSER_EXECUTABLE` at the binary:

```bash
CLAUDE_BROWSER_ENGINE=chromium CLAUDE_BROWSER_EXECUTABLE=/usr/bin/chromium \
  CLAUDE_BROWSER_HEADLESS=1 python3 mcp-server/server.py
```

Headless mode supports the core toolset (navigate, screenshot, click, type,
scroll, element queries, script execution, eval chains, waiting, history,
keyboard, text extraction) with real multi-tab management — `browser_create_tab`
returns a `tabId` usable with `tab_id` on every other tool. The same safety
guards apply, except that a protected-site action always takes the
`confirm_token` route, since no human is there to approve it. Startup takes
~15 seconds; the server holds the first command until the browser is ready
(tunable via `CLAUDE_BROWSER_HEADLESS_STARTUP_TIMEOUT`, default 45s).

Not implemented headless, and answered with `Unsupported headless action`:
console and network logging, `browser_observe_element` /
`browser_stop_observing`, `browser_wait_for_change`,
`browser_click_and_wait`, `browser_scroll_and_capture`,
`browser_hard_refresh`, `browser_reload_all`, `browser_reload_by_url`,
`browser_get_tab_info`, `browser_find_tabs` and
`browser_screenshot_all_tabs`. `browser_get_page_info` returns only the URL
and title. Three tools work **only** headless: `browser_eval_chain`,
`browser_wait_and_act` and `browser_inject_observer`.

`browser_type` honours `clear`, `press_enter` and `delay` as the extension
does, and lowers `delay` so the whole text types within 30 seconds (about 25
in Firefox). With no `selector` it types into whatever has focus, so the
credential check follows focus through shadow roots and same-origin frames
to the real field; focus inside a cross-origin frame cannot be checked and
is refused.

### Using the Browser Agent

#### Interactive Mode
```bash
python3 ~/.claudecodebrowser/agent/browser_agent.py -i
```

#### Command Line
```bash
# Take a screenshot
browser-agent --screenshot

# Navigate to a URL
browser-agent --navigate https://example.com

# Get page info
browser-agent --info

# Check server status
browser-agent --check
```

#### Python API
```python
from browser_agent import BrowserAutomationAgent

agent = BrowserAutomationAgent(verbose=True)

# Navigate to a page
agent.navigate("https://example.com")

# Take a screenshot
agent.screenshot("example.png")

# Click an element
agent.click(selector="button.submit")

# Type text
agent.type_text("Hello, World!", selector="#search-input")

# Fill a form. Note: password fields are refused by default - use the
# browser's own password manager for credentials, or set
# "allow_password_typing": true in ~/.claudecodebrowser/safety.json.
agent.fill_form({
    "username": "myuser",
    "email": "myuser@example.com"
}, submit=True)
```

### Available MCP Tools

#### Core Navigation & Screenshots
| Tool | Description |
|------|-------------|
| `browser_screenshot` | Take a screenshot of the visible area (`full_page` works headless only; Firefox falls back to the viewport with `fullPageCaptured: false`) |
| `browser_navigate` | Navigate to a URL, optionally in new tab |
| `browser_go_back` | Navigate back in tab history |
| `browser_go_forward` | Navigate forward in tab history |
| `browser_refresh` | Refresh current page |
| `browser_hard_refresh` | Force refresh bypassing cache (Ctrl+Shift+R) |
| `browser_reload_all` | Reload all browser tabs |
| `browser_reload_by_url` | Reload tabs matching URL pattern |

#### Element Interaction
| Tool | Description |
|------|-------------|
| `browser_click` | Click element by selector, XPath, text, or coordinates |
| `browser_type` | Type text into an input field. Refused on a credential field |
| `browser_scroll` | Scroll page or element (up/down/left/right/top/bottom) |
| `browser_hover` | Hover over an element to trigger hover effects |
| `browser_get_value` | Get the value of an input element. Credential and hidden fields read back as `***` |
| `browser_set_value` | Set input value directly (no typing simulation) |
| `browser_select_option` | Select an option in a dropdown |
| `browser_press_key` | Press a keyboard key (Enter, Escape, arrows, shortcuts) |

#### Page Inspection
| Tool | Description |
|------|-------------|
| `browser_get_page_info` | Get URL, title, forms, headings, interactive elements |
| `browser_get_text` | Extract visible text of the page or an element |
| `browser_get_elements` | Find elements matching a CSS selector |
| `browser_highlight` | Highlight an element for visual debugging |
| `browser_execute_script` | Execute JavaScript in browser context. Not constrained by the credential guard: a script can read any field |

#### Tab Management
| Tool | Description |
|------|-------------|
| `browser_get_tabs` | List open tabs (current window by default; `current_window_only=false` for all) |
| `browser_get_tab_info` | Detailed info for one tab, including its page info |
| `browser_find_tabs` | Find tabs by URL, URL pattern, title, or `active`/`audible` **set to true**. At least one filter that actually narrows is required, and results cap at 50 — with no filter it returned every tab in every window, uncapped. `active: false` matches almost every tab, so it does not count as a filter. Use `browser_get_tabs` to list tabs deliberately. |
| `browser_create_tab` | Create a new tab |
| `browser_close_tab` | Close a tab by ID |
| `browser_focus_tab` | Focus/activate a tab by ID |
| `browser_screenshot_all_tabs` | Cycle through tabs capturing a screenshot of each |

#### Waiting & Synchronization
| Tool | Description |
|------|-------------|
| `browser_wait_for_element` | Wait for element to appear on page |
| `browser_wait_for_change` | Wait for DOM changes (useful after clicks) |
| `browser_wait_for_network_idle` | Wait for network requests to settle (counts subresources too) |
| `browser_click_and_wait` | Click element and wait for DOM changes |

#### Advanced Observation
| Tool | Description |
|------|-------------|
| `browser_observe_element` | Start observing element for changes. Expires after 5 minutes (`max_lifetime_ms`) so a forgotten observer does not run for the document's lifetime; `browser_stop_observing` reports `expired: true` when it did, so a truncated change list is not mistaken for a quiet page |
| `browser_stop_observing` | Stop observing and get accumulated changes |
| `browser_scroll_and_capture` | Scroll through page capturing element info |

#### Console & Network Logging
| Tool | Description |
|------|-------------|
| `browser_start_logging` | Start capturing network traffic and page errors on this tab, until `browser_stop_logging` or a navigation to another origin. `capture_bodies=false` for headers/metadata only; `include_all_types=true` to include images, fonts and stylesheets |
| `browser_stop_logging` | Stop capturing (logs are preserved) |
| `browser_get_console_logs` | Retrieve uncaught page errors, unhandled rejections and the extension's own console output (including what `browser_execute_script` prints). **Not** the page's own `console.log` calls, which a content script cannot see |
| `browser_get_network_logs` | Retrieve captured requests and responses (credential headers, URL parameters and body fields scrubbed) |
| `browser_clear_logs` | Clear all captured logs |

#### Human Approval, Workflows & Auditing
| Tool | Description |
|------|-------------|
| `browser_request_approval` | Ask the human at the browser to Approve/Deny an action (extension window + OS notification) |
| `browser_solve_captcha` | Detect a captcha and hand it to the human to solve, then continue (never auto-solves) |
| `browser_run_workflow` | Run a declarative multi-step workflow with assertions — an end-to-end test runner for web apps |
| `browser_audit_page` | One-call page audit: headings, missing alt text, unlabeled inputs, meta info + screenshot for visual critique |

#### Safety
| Tool | Description |
|------|-------------|
| `browser_safety_status` | Show active safety policy, mode (attended or headless), headless-only tools, credential guard state, rate-limit state, and audit log location |

#### Headless only
In attended Firefox these return `Unknown action`.

| Tool | Description |
|------|-------------|
| `browser_eval_chain` | Run a sequence of JavaScript expressions sharing state, with per-step console capture |
| `browser_wait_and_act` | Poll a condition, then run an action script (timeout capped at 30s) |
| `browser_inject_observer` | Record DOM mutations into `window.__ccb_mutations`, read back with `browser_execute_script` |

### Console & Network Logging

Essential for debugging AI chat interfaces and monitoring API communications.
Attended (Firefox) mode only: the headless backend implements none of the
logging tools.

The `browser-agent` command line has no logging options; call the tools from
Python through `call_tool`:

```python
agent = BrowserAutomationAgent()

# Start on the site you want to watch: navigating the tab to another origin
# ends the session.
agent.navigate("https://example.com/chat")
agent.call_tool("browser_start_logging", clear_existing=True)

agent.type_text("Hello!", selector="#chat-input")
agent.click(selector="#send-button")

errors = agent.call_tool("browser_get_console_logs", level="error")
for log in errors.get("logs", []):
    print(f"[{log['level']}] {log['message']}")

network = agent.call_tool("browser_get_network_logs", url_pattern="api/chat")
for req in network.get("logs", []):
    print(f"{req['method']} {req['url']} -> {req.get('status')}")
    print(f"Response: {str(req.get('responseBody', ''))[:200]}")

agent.call_tool("browser_stop_logging")
```

> **What console capture sees.** A content script cannot see the page's own
> console, so the page's `console.log` calls are **not** captured. What is
> captured: uncaught page errors and unhandled promise rejections
> (`source: "page"`), and output from the extension's own scripts, including
> anything `browser_execute_script` prints (`source: "extension"`). An empty
> result does not mean the page logged nothing; the result says so with
> `capturesPageConsole: false`.

> **Logs on disk.** `~/.claudecodebrowser/logs/` holds `mcp_server.log`,
> `native_host.log` and the guard's `audit.jsonl`, all created `0600` in a
> `0700` directory. `mcp_server.log` and `audit.jsonl` rotate at 5 MB;
> `native_host.log` is rotated only when the host starts and finds it over
> 5 MB. The server and native host log at
> INFO and record the shape of a command, not its payload — raise them with
> `CLAUDE_BROWSER_DEBUG=1` / `CLAUDE_BROWSER_HOST_DEBUG=1` when you need the
> detail, and remember that detail includes page content. Sensitive argument
> values (`text`, `script`, `value`, `password`, `steps`, `action_script`,
> `condition`, `key`, `url_pattern`) are replaced with `***` wherever they sit
> in the arguments, nested objects and lists included, in both the
> application log and the audit log, from one list in `safety.py` that both
> read. `url_pattern` is masked outright: it is a regex matched against whole
> URLs, so it can name a reset token. A `url` argument
> is **reduced rather than masked**: the scheme, host and path are kept, and
> the userinfo, query and fragment — where a password, a reset token or an SSO
> code lives — become a marker, so an entry reads
> `https://***@intranet.example.com/wiki/Home` or
> `https://example.com/reset?***`. A URL in a scheme the guard refuses keeps
> only its scheme (`data:***`), because such a URL is a payload and not a
> location, and a `url` that is not a string is masked whole. `audit.jsonl`
> still records which pages were visited, which is the point of an audit log
> and also a browsing history.

> **How network capture works.** Requests are recorded in the extension's
> background script through Firefox's `webRequest` API, not by replacing the
> page's `fetch`/`XHR`. That means `fetch` is captured (Firefox's content-script
> sandbox makes `window.fetch` read-only, so a content script can only ever see
> XHR), pages with a strict CSP are captured, and no page global is touched.
> Capture is off until `browser_start_logging`, and stops at
> `browser_stop_logging` or as soon as the tab navigates to a different
> origin, so start it on the site you want to watch rather than on
> `about:blank`. It is refused on a private-browsing tab. While nothing is
> being logged, no listeners are attached at all. By default only API-shaped
> traffic is logged — pass `include_all_types: true` for images, fonts and
> stylesheets, which also attaches a response filter to documents and scripts.
> Response bodies are collected for textual content types up to 5000
> characters and request bodies up to 1000; `capture_bodies: false`
> suppresses both.
>
> What is scrubbed before the agent sees it:
>
> - **Headers.** Credential-bearing headers (`Authorization`,
>   `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`,
>   `X-CSRF-Token`, …) are reported as `***`.
> - **URLs.** The request URL and the `Location`, `Content-Location`,
>   `Refresh` and `Referer` headers lose their userinfo, and credential
>   parameters are masked in the query, the fragment (including a hash
>   router's `#/callback?code=...`) and `;name=value` path parameters. `code`,
>   `token`, `access_token`, `sid`, `ticket` and similar count here although
>   they are ordinary names in a body. Relative redirect targets get the same
>   treatment; a URL that cannot be taken apart is withheld.
> - **Bodies.** A JSON body is walked structurally and every value under a
>   credential-shaped key is masked, however deep; form data, multipart
>   fields and `key=value` / `key: "value"` text are scrubbed by name. In
>   HTML, an `<input>` whose tag marks it as a credential (`type=password` or
>   `hidden`, a password, one-time-code, card-number or CVC `autocomplete`
>   token, or a credential-shaped `name` or `id`) has its `value` masked, and
>   a credential `<textarea>` its contents. Redacting an `Authorization`
>   header is worth little if the body that minted the token is kept
>   verbatim.
>
> **What is not scrubbed:** a credential in the free text of a captured HTML
> page, the contents of a `contenteditable` element, and a custom element such
> as `<sl-input type="password" value="...">` (only `<input>` and
> `<textarea>` tags are examined). The scrub is pattern matching over a log,
> not a parser, so treat captured bodies as sensitive.

#### Use Cases
- **Debug AI Chat Interfaces**: See uncaught page errors and API request/response data
- **Monitor API Communications**: Track requests with bodies, captured at the network layer
- **Troubleshoot Errors**: Filter console logs by error level
- **Verify Integrations**: Confirm API calls are being made correctly

### Page Refresh Commands

Essential for development workflows - refresh browser tabs after server restarts:

```bash
# Refresh current tab
browser-agent --refresh

# Hard refresh (bypass cache) - like Ctrl+Shift+R
browser-agent --hard-refresh

# Reload all browser tabs
browser-agent --reload-all

# Reload all localhost tabs
browser-agent --reload-localhost

# Reload localhost on specific port
browser-agent --reload-localhost 5000

# Reload all dev server tabs (localhost, 127.0.0.1, *.local, *.dev)
browser-agent --reload-dev

# Reload tabs matching URL pattern
browser-agent --reload-url "ligandal"
```

#### Python API for Refresh
```python
agent = BrowserAutomationAgent()

# Refresh current page
agent.refresh()

# Hard refresh (bypass cache)
agent.hard_refresh()

# Reload all localhost tabs
agent.reload_localhost()

# Reload localhost:5000 specifically
agent.reload_localhost(port=5000)

# Reload all dev server tabs
agent.reload_dev_servers()

# Reload tabs matching pattern
agent.reload_by_url(url_pattern=r"localhost:500[0-9]")
```

### Tool Parameters

#### browser_click
```json
{
  "selector": "CSS selector",
  "xpath": "XPath expression",
  "text": "Text to find and click",
  "x": 100,
  "y": 200,
  "double_click": false,
  "right_click": false
}
```

#### browser_type
```json
{
  "text": "Text to type",
  "selector": "CSS selector",
  "placeholder": "Placeholder text",
  "name": "Input name",
  "clear": true,
  "press_enter": true,
  "delay": 50
}
```

`delay` is per keystroke: 50ms by default in Firefox, none headless. It is
lowered so the whole text types within about 25 seconds in Firefox and 30
headless, inside the server's wait for a reply. Headless reports the
lowering with `delay_capped`; Firefox lowers it without saying so.

#### browser_scroll
```json
{
  "direction": "down",
  "amount": 300,
  "to_element": "CSS selector"
}
```

## API Endpoints

### HTTP API (Port 8765)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Server health check (the only endpoint that needs no token) |
| `/mcp/tools` | GET | List available MCP tools |
| `/mcp/call` | POST | Execute an MCP tool |
| `/screenshots` | GET | List saved screenshots |
| `/browser/command` | POST | **Gone** — returns `410`. It queued a command and reported success having run nothing; use the MCP tool interface. |
| `/browser/poll` | GET | Next pending command, polled by the native host |
| `/browser/response` | POST | Receive browser response (posted by the native host) |

Every endpoint except `/health` requires the `X-API-Key` header, holding the
token from `~/.claudecodebrowser/api_token`; without it the server answers
`403`.

### Example API Calls

```bash
# Health check
curl http://localhost:8765/health

KEY="X-API-Key: $(cat ~/.claudecodebrowser/api_token)"

# List tools
curl -H "$KEY" http://localhost:8765/mcp/tools

# Take screenshot
curl -X POST http://localhost:8765/mcp/call \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{"name": "browser_screenshot", "arguments": {}}'

# Click element
curl -X POST http://localhost:8765/mcp/call \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{"name": "browser_click", "arguments": {"selector": "button.login"}}'
```

## File Locations

| Path | Description |
|------|-------------|
| `~/.claudecodebrowser/` | Main installation directory |
| `~/.claudecodebrowser/screenshots/` | Saved screenshots (`0700`, each file `0600`; override with `CLAUDE_BROWSER_SCREENSHOTS_DIR`). Pruned after 7 days / 500 files — see [Screenshot retention](#screenshot-retention) |
| `~/.claudecodebrowser/logs/` | Log files, including the safety guard's `audit.jsonl` |
| `~/.claudecodebrowser/api_token` | HTTP/WebSocket API token (`0600`, generated on first run) |
| `~/.claudecodebrowser/safety.json` | Safety guard configuration (written with defaults on first run) |
| `~/.mozilla/native-messaging-hosts/` | Firefox native messaging manifests (Linux) |
| `~/Library/Application Support/Mozilla/NativeMessagingHosts/` | Firefox native messaging manifests (macOS) |
| `mise.local.toml` | Local-only AMO signing credentials (gitignored) |

### Screenshot retention

A screenshot holds whatever was on screen — open mail, a logged-in dashboard,
a bank balance — and one file is written per `browser_screenshot` call, since
`save_to_file` defaults to true. Nothing used to remove them, which made that
directory the longest-lived record of your browsing in the project. The
defaults:

| Variable | Default | Meaning |
|----------|---------|---------|
| `CLAUDE_BROWSER_SCREENSHOT_RETENTION_DAYS` | `7` | Delete screenshots older than this. `0` disables the age sweep. |
| `CLAUDE_BROWSER_SCREENSHOT_MAX_FILES` | `500` | Keep at most this many, oldest deleted first. `0` disables the cap. |

Setting both to `0` keeps an indefinite visual record, which is a choice
rather than an accident. An unparseable value (`7d`, `forever`) logs a
warning and uses the default — it used to raise at import time, and the
native host starts the server with `stderr` discarded, so the traceback went
nowhere and you saw only restart backoff.

**Pruning only ever touches a directory this project created.** It deletes
`*.png` with no way to tell its own files from yours, and
`CLAUDE_BROWSER_SCREENSHOTS_DIR` can point anywhere — `~/Pictures`,
`~/Desktop`, a repo's `docs/screenshots`. So a directory is prunable only if
it contains a `.ccb-screenshots` marker file, which is written when the
server creates the directory itself. Point the override at a directory that
already exists and nothing in it is ever deleted. If you *want* an existing
directory swept, create the marker by hand:

```bash
touch "$CLAUDE_BROWSER_SCREENSHOTS_DIR/.ccb-screenshots"
```

A screenshot of a private-browsing window is never written to disk at all.
The image is returned in the tool response and nowhere else.

### Private browsing

If you allow the extension in private windows, two things are refused rather
than done quietly:

- `browser_start_logging` on a private tab. A logging session there would put
  request and response bodies into a buffer the agent reads, which is the one
  expectation a private window exists to uphold. The check fails closed: if
  the tab cannot be inspected, logging is refused.
- Writing a screenshot of a private window to disk, as above.
  `browser_screenshot_all_tabs` skips private windows entirely, since it is a
  bulk sweep you did not aim at any particular tab.

Everything else still works on a private tab — reading text, clicking,
inspecting — because driving a private window can be exactly what you asked
for. The line is persistence: nothing from a private window is left behind
after the session.

## Troubleshooting

### Extension not loading
- Ensure manifest.json is valid JSON
- Check Firefox console for errors
- Verify the extension ID matches in native messaging manifest
- After pulling a new version, remember the extension must be repackaged and
  reloaded — see [Updating the Firefox Extension](#updating-the-firefox-extension)

### Native messaging not working
- Check that the path in `claudecodebrowser.json` is correct
- Ensure the host script is executable
- Check `~/.claudecodebrowser/logs/native_host.log`

### Server connection issues
- Verify the server is running: `curl http://localhost:8765/health`
- Check `~/.claudecodebrowser/logs/mcp_server.log`
- Ensure no firewall is blocking local connections

### Screenshots not saving
- Check write permissions for `~/.claudecodebrowser/screenshots/`
- Verify the browser has the page fully loaded

## Development

Tooling is pinned with [mise](https://mise.jdx.dev) (Python, Node and
`web-ext`): run `mise install` once.

### Running in development mode

1. Start the MCP server with debug logging:
   ```bash
   CLAUDE_BROWSER_DEBUG=1 python3 mcp-server/server.py
   ```

2. Load the extension temporarily in Firefox

3. Use the browser agent in verbose mode:
   ```bash
   python3 agent/browser_agent.py -i -v
   ```

### Extension debugging
- Open Firefox Developer Tools (F12)
- Go to the Console tab
- Filter by "ClaudeCodeBrowser"

### Tests

No external test dependencies — `unittest` for the Python side, and Node
harnesses that evaluate `content.js` and `background.js` against stubbed
`browser.*`/DOM objects and drive them through their real message entry
points.

```bash
mise run test            # everything
mise run test-python     # MCP server, safety guard, headless backend, native host, stdio wrapper, agent, identity
mise run test-extension  # content script, background script, approval page
```

What the suites cover, beyond the obvious:

- The credential guard masks password reads as well as refusing writes, in the
  content script and the headless backend.
- Network capture is opt-in: asserted against the source that `content.js`
  never assigns to `window.fetch` or `XMLHttpRequest.prototype`.
- The response-body stream filter writes every chunk back unmodified and
  always closes, including when a chunk cannot be decoded — a filter that
  alters a response breaks the page.
- The native host only terminates its own server process.
- The extension ID, `update_url`, `updates.json` key and version strings agree
  across the manifest, the native-host template, both installers, the MCP
  server, the README badge and the changelog.

### Packaging and releasing

```bash
mise run package   # unsigned .xpi (temporary load only)
mise run sign      # AMO-signed .xpi + updates.json  (see Updating the Firefox Extension)
mise run release   # GitHub release with both assets
```

The unsigned path refuses to overwrite a signed `.xpi`: both land on the same
filename and `publish-release.sh` uploads it, so a stray rebuild would
otherwise ship a build nobody can install permanently.


## Workflow Testing & Page Audits

Two tools turn the browser into a lightweight QA rig for building websites:

**`browser_run_workflow`** executes a declarative sequence of tool steps with
assertions — click through a signup flow, submit a form, verify the result —
and reports pass/fail per step with a screenshot captured at the point of
failure:

```json
{
  "steps": [
    { "label": "open app", "tool": "browser_navigate",
      "arguments": { "url": "http://localhost:3000" },
      "assert": { "selector_exists": "#login-form" } },
    { "label": "fill email", "tool": "browser_type",
      "arguments": { "selector": "#email", "text": "test@example.com" } },
    { "label": "submit", "tool": "browser_click",
      "arguments": { "selector": "button[type=submit]" },
      "assert": { "url_contains": "/dashboard", "text_contains": "Welcome" } }
  ]
}
```

Every step passes through the safety guard individually, and failing steps
capture `workflow_fail_<label>.png` automatically.

**`browser_audit_page`** gathers everything needed for a structural and visual
critique in one call: heading hierarchy, images missing alt text, unlabeled
form inputs, empty links/buttons, meta description and viewport info, page
dimensions — plus a screenshot. Point Claude at a page and ask for a critique;
this tool is the evidence-gathering step.

## Safety Guards

Every tool call passes through a safety guard before it reaches the browser.
The guard is designed to keep an automated agent from doing things the human
operating it would not expect, while staying out of the way for normal
development workflows. Policy lives in `~/.claudecodebrowser/safety.json`
(created with safe defaults on first run) and can be inspected at runtime with
the `browser_safety_status` tool.

> **How much the credential guard actually guarantees.** It constrains the
> dedicated tools — typing, reading, element metadata — in both attended and
> headless mode. It does **not** constrain `browser_execute_script`, which can
> read any field including a password. `browser_safety_status` reports which
> of three states you are in: `enforced` (scripts off),
> `enforced_except_scripts` (the default: scripts on, but refused on protected
> sites), or `advisory` (scripts on everywhere). That is stated in the tool
> output rather than only here, because it is the kind of thing an agent
> should be able to find out.

### What the guard enforces

| Guard | Behavior |
|-------|----------|
| **URL scheme guard** | Navigation is limited to `http://`, `https://`, and `about:blank`. `file:`, `javascript:`, `data:`, `chrome:`, `resource:`, and `moz-extension:` targets are always refused. |
| **Blocklist / allowlist** | `blocked_url_patterns` refuses matching URLs; a non-empty `allowed_url_patterns` switches to allowlist mode where only matching URLs may be visited. A list that *refuses* matches loosely (anywhere in the URL), because a near miss there errs towards refusing. A list that *grants* — `allowed_url_patterns`, `trusted_url_patterns` — is matched **anchored**: a pattern grants access when it covers the whole of the URL up to one of its delimiters (`:`, `/`, `?`, `#`), or matches a whole hostname. The query string and fragment cannot satisfy a pattern. So `^https://localhost` covers `https://localhost:3000/app` but not `https://localhost.evil.com/x`, which a loose match used to allow. An unanchored mid-URL pattern such as `stripe\.com/dashboard` therefore no longer grants anything; write `^https://stripe\.com/` instead. A pattern that names a host must match **the whole host**: `example\.com` covers `example.com` and **not** `www.example.com` — write `(.+\.)?example\.com` for a whole domain tree. Do **not** start a permit pattern with `.*`: `.*\.stripe\.com` is satisfied by a path segment on any host, so it grants `https://evil.com/a.stripe.com`. (A permit pattern can no longer be satisfied by the query string or fragment, so `https://evil.com/?x=a.stripe.com` is refused.) `.*` on its own still means everything. |
| **Broken patterns** | A pattern in `allowed_url_patterns` or `trusted_url_patterns` that does not compile is dropped, and if **none** of them compile the restriction stays on and permits nothing — a granting list is never allowed to evaporate into "allow everything". `browser_safety_status` reports every failure in `pattern_errors`. |
| **URL normalisation** | Every pattern sees the URL the browser will actually load: leading/trailing control characters and spaces stripped, tab/CR/LF removed, `\` treated as `/`. Firefox loads `https://www.irs.gov\payments` as `https://www.irs.gov/payments`, and without this a backslash walked straight past the protected-domain check. The host is also percent-decoded, lowercased and stripped of trailing dots, and userinfo is ignored, because the browser loads `https://%63hase.com/`, `https://chase.com./` and `https://localhost:3000@evil.com/` as `chase.com`, `chase.com` and `evil.com`. The guard judges a normalised copy; the URL sent to the browser is the one you asked for. |
| **Protected sites** | State-changing actions (click, type, navigate) on banking, payment, health, and government sites require explicit confirmation — by default from the **human at the browser** (see below), with an agent-side `confirm_token` round trip as the fallback. Read-only actions (screenshots, inspection) are unaffected. Scripts are refused outright by default — see *Scripts on protected sites*. |
| **Password fields (writing)** | Typing into a credential field is refused by default in both attended and headless modes, from one definition the two share and a test pins. A "credential field" is any of: an element with `type="password"`, including a custom element such as `<sl-input type="password">`, which is what component libraries ship and therefore what an agent has to target; an element whose `autocomplete` holds `current-password`, `new-password`, `one-time-code`, `cc-number`, `cc-csc`, `cc-exp`, `cc-exp-month` or `cc-exp-year`; or an element that holds an entered value — `<input>`, `<textarea>`, `<select>`, a `contenteditable`, a custom element — whose `name` or `id` looks like a credential (`passwd`, `cvv`, `otpCode`, `user[password]`, `apiKey`, `mfaCode`, `verificationCode`, `securityCode`, `cc_number`, `creditCard`, `pincode`, `cookie`, `recoveryCodes`, `backup_codes`, `cardCode`). `browser_type`, `browser_set_value` and, in headless mode, `browser_press_key` (whose keys really type there) are covered. Credentials belong in the browser's own password manager. Set `"allow_password_typing": true` to override; an `allow_password` argument sent by the agent, in any spelling, is dropped, so only `safety.json` can open the guard. |
| **Credential fields (reading)** | Reading one back is guarded too: `browser_get_value` returns `***` with `masked: true`, and `browser_get_elements` / `browser_get_page_info` mask the value in element metadata. In the Firefox extension one function decides this for `browser_get_value`, `browser_get_elements`, `browser_get_page_info`, `browser_get_tab_info`, `browser_get_text`, `browser_scroll_and_capture` and `browser_observe_element` — the last two were *not* covered until a data-flow review found them, which is why this sentence names the set instead of asserting completeness. The fields masked are the credential fields defined in the row above, plus `type=hidden` inputs, which carry CSRF and session tokens; a `***` can therefore come from an ordinary text input. `browser_get_text` on a credential field returns `***`, and a whole-page `browser_get_text` masks the text of any credential `<textarea>` or `contenteditable` inside it, reporting `maskedFields`. `allow_password_typing` lifts the mask for `browser_get_value`, `browser_get_elements` and `browser_get_page_info` only; the other readers mask whatever it says. `browser_execute_script` can still read any field — see below. Nor does the mask apply to `browser_screenshot`: a revealed password (a "show password" toggle makes the field `type=text`), an on-screen one-time code or a visible account number is captured as pixels and kept under the retention policy. |
| **Human approval** | The Approve/Deny decision is taken in an **extension window** (`moz-extension://`), which the page being automated cannot read, restyle or click. Only trusted events count, and only the extension's own pages may answer — a content script's attempt is refused. Closing the window is a denial. If no window can be opened the in-page banner is used as a fallback, rendered in a closed shadow root, and the result is marked `degraded: true` because a prompt sharing the DOM with the page is not equivalent. If the prompt cannot be completed, the action is **refused** rather than falling back to a token the agent could satisfy itself. Firefox does not support buttons on notifications, so the notification remains an attention-getter. |
| **Scripts on protected sites** | The script tools — `browser_execute_script`, `browser_eval_chain`, `browser_wait_and_act`, `browser_inject_observer`, and `browser_audit_page`, which runs a fixed inspection script — are **refused outright** on a protected URL, not merely confirmed: a script can read any field on the page, so the credential guard does not constrain it, and a confirmation the agent can satisfy is no control over arbitrary JavaScript. Turn it off with `"deny_scripts_on_protected_urls": false`. |
| **Unlisted domains** | `protected_url_patterns` is a denylist of ~16 finance, health and government patterns, so everything else — your mail, your cloud console, your admin panels — is unprotected by default. Set `"unlisted_domains": "confirm"` to invert that, and list your normal work in `trusted_url_patterns`. Expect a lot of prompts until that list is right; prompt fatigue is its own hazard, which is why it is opt-in. |
| **Confirmation tokens** | A `confirm_token` is bound to a hash of the exact call — tool, arguments and URL — so one earned on a harmless call cannot be spent on a dangerous one. Single-use, 120s. |
| **Blocklist scope** | `blocked_url_patterns` / `allowed_url_patterns` apply to the page a tool acts on, not only to a navigation argument, so blocking a domain also refuses reads on an already-open tab there. |
| **Low-risk acts** | `browser_scroll`, `browser_scroll_and_capture`, `browser_hover`, `browser_highlight` and `browser_focus_tab` change state, so read-only mode blocks them, but they do not raise a protected-site prompt — prompting on every scroll teaches people to click Approve without reading. `browser_screenshot_all_tabs` is **not** observation: it activates and photographs every tab in every window. |
| **Human approval (Duo-style)** | With `protected_approval` set to `"auto"` (default) or `"human"`, a protected action triggers an OS notification plus the Approve/Deny window described above. The action proceeds only if the person clicks **Approve** (60s timeout = deny). `"token"` forces the agent-side flow; headless mode always uses tokens since no human is present. |
| **Read-only mode** | Set `"read_only": true` or `CLAUDE_BROWSER_READ_ONLY=1` to block every state-changing tool while keeping screenshots, page inspection, and log reading available. Useful for "look but don't touch" sessions. |
| **Script toggle** | Set `"allow_script_execution": false` or `CLAUDE_BROWSER_ALLOW_SCRIPTS=0` to disable `browser_execute_script`, `browser_eval_chain`, `browser_wait_and_act`, `browser_inject_observer` and `browser_audit_page` entirely. `browser_audit_page` only observes, but it does so by running a fixed script in the page, so the toggle covers it; `browser_safety_status` lists every tool it covers as `script_tools`. |
| **Rate limiting** | A sliding-window cap (`max_actions_per_minute`, default 120) prevents runaway automation loops. |
| **Audit log** | Every decision (allowed, denied, confirmation requested) is appended to `~/.claudecodebrowser/logs/audit.jsonl` with sensitive argument values (typed text, scripts, passwords, pressed keys, URL patterns) redacted at any depth and URLs reduced to scheme, host and path. Nothing is written while `"enabled": false`. |

### Example `safety.json`

```json
{
  "enabled": true,
  "read_only": false,
  "allow_script_execution": true,
  "confirm_protected_actions": true,
  "max_actions_per_minute": 120,
  "audit_log": true,
  "blocked_url_patterns": ["internal-admin\\.mycompany\\.com"],
  "allowed_url_patterns": [],
  "protected_url_patterns": ["paypal\\.com", "chase\\.com", "\\.gov([:/?#]|$)"],
  "deny_scripts_on_protected_urls": true,
  "allow_password_typing": false
}
```

The defaults include a starter set of protected patterns for common banking,
payment, brokerage, government, and health domains — edit the file to match
your own risk tolerance. Setting `"enabled": false` turns off every policy
check and the audit log (not recommended). The scheme guard still refuses
`file:`, `javascript:`, `data:` and the rest, and the credential guard, which
`allow_password_typing` controls, stays on.

`safety.json` is written once, on first run, so it keeps whatever defaults
were current then. When a default turns out to be weaker than intended and is
fixed, an existing file still holds the old pattern. The guard recognises
those old defaults exactly, applies the fix in memory, logs a warning and
lists it under `pattern_upgrades` in `browser_safety_status`; it does not
rewrite your file, so update it to match. A pattern you wrote yourself is
never changed.

### Credentials and 2FA: what this project deliberately does NOT do

- **No password vault.** ClaudeCodeBrowser never stores credentials, and by
  default refuses to type into password fields. Use the browser's own
  password manager (Firefox autofill, Bitwarden, 1Password, ...): you click
  the autofill yourself, and the secret never passes through the AI, its
  arguments, or its logs.
- **No 2FA auto-approval.** Automating Duo/TOTP/push approvals would defeat
  the purpose of a second factor. When automation reaches a login or 2FA
  wall, the right flow is: Claude pauses (use `browser_request_approval` to
  ping you), you complete the login/2FA yourself in the same tab, then
  automation continues in the authenticated session.

The Duo-style pattern this project *does* implement is pointed the other way:
**you are the second factor for Claude's actions.** Sensitive operations
push a notification to you and wait for your explicit Approve click in the
browser.

### Captchas: detect and hand off, never auto-solve

`browser_solve_captcha` follows the same human-in-the-loop principle.
Captchas exist to tell humans from bots, so auto-solving them (via OCR or
third-party solver farms) is explicitly **not** something this project does.
Instead:

- It **detects** reCAPTCHA, hCaptcha, Cloudflare Turnstile, and generic
  image/text captchas on the page.
- It **notifies you** (OS notification + an in-page banner) and **pauses**.
- **You solve it** in the same tab. For token-based widgets (reCAPTCHA,
  hCaptcha, Turnstile) it auto-detects completion and continues; otherwise
  click **Done**. `detect_only: true` just reports what's present without
  waiting.

In headless mode there is no human, so it reports what it detected and that a
human is required — re-run that step in attended mode (the Firefox extension)
so you can complete the challenge.

## Pairs Well With

ClaudeCodeBrowser is the *browser hands* of a Claude Code setup. For
secretary-style workflows, combine it with purpose-built MCP connectors
rather than screen-driving web apps: Gmail/Calendar MCP connectors handle
email triage and scheduling far more reliably than clicking through webmail,
while this project covers the parts that genuinely need a browser — visual
review of what you're building, workflow testing, form filling, and anything
without an API.

## Uninstalling

### Remove the Firefox add-on

1. Open `about:addons` in Firefox (or menu → Add-ons and themes)
2. Find **ClaudeCodeBrowserX** under Extensions
3. Click the `…` menu next to it and choose **Remove**

If the extension was loaded temporarily via `about:debugging`, it disappears
on its own the next time Firefox restarts — or click **Remove** on the
`about:debugging#/runtime/this-firefox` page.

### Remove the native host and server

```bash
./scripts/uninstall.sh
```

This removes the native messaging manifest, the `~/.claudecodebrowser`
install directory, and its own symlinks in `~/bin`. If there are saved
screenshots it asks first, and can keep a copy in
`~/claudecodebrowser-screenshots`. If you registered the MCP server with
Claude Code, also run:

```bash
claude mcp remove claudecodebrowser
```

## Security Considerations

- The server only binds to localhost (127.0.0.1) by default
- All HTTP endpoints except `/health` require the `X-API-Key` token
  (auto-generated at `~/.claudecodebrowser/api_token`, mode 0600)
- The WebSocket control channel requires the same token as its first frame,
  so no other local process can register as the browser or forge responses
- No CORS headers are sent, so web pages cannot reach the API from the browser
- The extension refuses `runtime.onMessageExternal` messages, so co-installed
  extensions cannot issue automation commands through it
- Native messaging is restricted to the specific extension ID
- A configurable safety guard (see [Safety Guards](#safety-guards)) enforces
  URL restrictions, protected-site confirmation, read-only mode, rate
  limiting, and audit logging
- Screenshots are stored under `~/.claudecodebrowser/screenshots` with `0700`
  permissions, not in a world-readable shared `/tmp`
- Password fields are protected in both directions: neither typed into nor
  read back without an explicit opt-in
- Network capture is off until you ask for it, happens in the extension's
  background script via `webRequest`, and never replaces a page's own
  `fetch`, `XMLHttpRequest` or `console`
- Credential-bearing headers, credential URL parameters and credential
  fields in bodies are scrubbed out of captured network logs (best effort;
  see *How network capture works*)
- No data is sent to external servers

## License

MIT License - Copyright (c) 2025 Andre Watson (nanogenomic), Ligandal Inc.

See [LICENSE](LICENSE) for full details.

This fork is published under the same license and retains the original
copyright. ClaudeCodeBrowser was created by
**Andre Watson** ([@nanogenomic](https://github.com/nanogenomic), [Ligandal
Inc.](https://ligandal.com)) — upstream:
<https://github.com/nanogenomic/ClaudeCodeBrowser>. See
[Credits](#credits).

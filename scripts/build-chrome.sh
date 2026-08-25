#!/bin/bash
#
# Build an EXPERIMENTAL Chrome/Chromium (Manifest V3) version of the extension.
#
# The extension is developed and tested against Firefox (Manifest V2). This
# script assembles a Chrome-compatible build by adding a small API shim
# (browser -> chrome, browserAction -> action) and an MV3 manifest. The result
# loads in Chrome via chrome://extensions -> "Load unpacked", but is not yet
# regularly tested there — treat it as a preview. For unattended Chromium
# automation, the headless Playwright backend (CLAUDE_BROWSER_ENGINE=chromium)
# is the supported path.
#
# Known MV3 caveat: the background script runs as a service worker. Chrome
# 105+ keeps the worker alive while the native messaging port is open, so the
# native-host connection is what keeps the extension responsive.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$SCRIPT_DIR/extension"
OUT="$SCRIPT_DIR/build/chrome"

echo "Building experimental Chrome extension into $OUT ..."

rm -rf "$OUT"
mkdir -p "$OUT"

# Copy static assets
cp -r "$SRC/icons" "$OUT/"
cp -r "$SRC/popup" "$OUT/" 2>/dev/null || true
cp "$SRC/content.css" "$OUT/" 2>/dev/null || true

# Prepend the compatibility shim to the scripts
SHIM='// Chromium compatibility shim (added by build-chrome.sh)
if (typeof browser === "undefined") { globalThis.browser = chrome; }
if (typeof chrome !== "undefined" && !chrome.browserAction && chrome.action) { chrome.browserAction = chrome.action; }
'

printf '%s\n' "$SHIM" | cat - "$SRC/background.js" > "$OUT/background.js"
printf '%s\n' "$SHIM" | cat - "$SRC/content.js" > "$OUT/content.js"

# Manifest V3 for Chrome
VERSION=$(python3 -c "import json; print(json.load(open('$SRC/manifest.json'))['version'])")
cat > "$OUT/manifest.json" << EOF
{
  "manifest_version": 3,
  "name": "ClaudeCodeBrowser (Experimental Chrome Build)",
  "version": "$VERSION",
  "description": "Browser automation extension for Claude Code - EXPERIMENTAL Chrome build; Firefox is the primary target",
  "author": "Ligandal",
  "homepage_url": "https://ligandal.com",

  "permissions": [
    "activeTab",
    "tabs",
    "nativeMessaging",
    "storage",
    "scripting",
    "webNavigation",
    "contextMenus"
  ],
  "host_permissions": ["<all_urls>"],

  "background": {
    "service_worker": "background.js"
  },

  "content_scripts": [
    {
      "matches": ["<all_urls>"],
      "js": ["content.js"],
      "css": ["content.css"],
      "run_at": "document_end",
      "all_frames": true
    }
  ],

  "action": {
    "default_icon": {
      "16": "icons/icon-16.png",
      "32": "icons/icon-32.png",
      "48": "icons/icon-48.png"
    },
    "default_title": "ClaudeCodeBrowser",
    "default_popup": "popup/popup.html"
  },

  "icons": {
    "16": "icons/icon-16.png",
    "32": "icons/icon-32.png",
    "48": "icons/icon-48.png",
    "128": "icons/icon-128.png"
  },

  "web_accessible_resources": [
    { "resources": ["icons/*"], "matches": ["<all_urls>"] }
  ]
}
EOF

# Native messaging manifest template for Chrome (uses allowed_origins with the
# extension ID, which Chrome assigns when the unpacked extension is loaded)
cat > "$OUT/claudecodebrowser.chrome.json" << EOF
{
  "name": "claudecodebrowser",
  "description": "ClaudeCodeBrowser Native Messaging Host",
  "path": "$HOME/.claudecodebrowser/native-host/claudecodebrowser_host.py",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://REPLACE_WITH_YOUR_EXTENSION_ID/"
  ]
}
EOF

echo ""
echo "Done. To try it:"
echo "  1. Open chrome://extensions, enable Developer mode"
echo "  2. Click 'Load unpacked' and select: $OUT"
echo "  3. Copy the extension ID Chrome assigns, then edit"
echo "     $OUT/claudecodebrowser.chrome.json and replace REPLACE_WITH_YOUR_EXTENSION_ID"
echo "  4. Install the native messaging manifest:"
echo "       Linux:  ~/.config/google-chrome/NativeMessagingHosts/claudecodebrowser.json"
echo "       macOS:  ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/claudecodebrowser.json"
echo "       (create the directory if needed, copy the edited file there)"
echo ""
echo "This build is EXPERIMENTAL. For reliable Chromium automation use the"
echo "headless backend instead: CLAUDE_BROWSER_ENGINE=chromium CLAUDE_BROWSER_HEADLESS=1"

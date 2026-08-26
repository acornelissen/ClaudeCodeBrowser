#!/bin/bash
#
# Package (and optionally sign) the Firefox extension into a versioned .xpi.
#
# Usage:
#   ./scripts/package-extension.sh            # build an unsigned .xpi
#   ./scripts/package-extension.sh --sign     # build + sign via AMO (needs API creds)
#
# Signing uses Mozilla's web-ext tool and the AMO API. It requires:
#   - Node.js + web-ext            (npm install -g web-ext)
#   - AMO_JWT_ISSUER / AMO_JWT_SECRET  (from https://addons.mozilla.org/developers/addon/api/key/)
#
# A signed .xpi can be installed permanently and — if you set an update_url
# (see below) — auto-updates. An unsigned .xpi only loads as a temporary
# add-on via about:debugging, or in Developer/Nightly with
# xpinstall.signatures.required=false.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$SCRIPT_DIR/extension"
DIST="$SCRIPT_DIR/dist"
SIGN=0
[ "$1" = "--sign" ] && SIGN=1

VERSION=$(python3 -c "import json; print(json.load(open('$SRC/manifest.json'))['version'])")
EXT_ID=$(python3 -c "import json; print(json.load(open('$SRC/manifest.json'))['browser_specific_settings']['gecko']['id'])")
XPI="$DIST/claudecodebrowser-${VERSION}.xpi"

mkdir -p "$DIST"

echo "Packaging ClaudeCodeBrowser extension v${VERSION} (${EXT_ID})"

if [ "$SIGN" = "1" ]; then
    if ! command -v web-ext &> /dev/null; then
        echo "Error: web-ext not found. Install it with: npm install -g web-ext" >&2
        exit 1
    fi
    if [ -z "$AMO_JWT_ISSUER" ] || [ -z "$AMO_JWT_SECRET" ]; then
        echo "Error: set AMO_JWT_ISSUER and AMO_JWT_SECRET (from the AMO API key page)." >&2
        exit 1
    fi
    echo "Signing via AMO (channel: unlisted, self-distribution)..."
    # --channel=unlisted signs without a public AMO listing, for self-hosting.
    # Use --channel=listed to submit to the public AMO catalog instead.
    web-ext sign \
        --source-dir "$SRC" \
        --artifacts-dir "$DIST" \
        --channel=unlisted \
        --api-key "$AMO_JWT_ISSUER" \
        --api-secret "$AMO_JWT_SECRET"
    echo "Signed .xpi written to $DIST (auto-named by web-ext)."
else
    # Plain zip -> .xpi. The archive root must be the manifest, not a folder.
    ( cd "$SRC" && zip -r -FS "$XPI" . -x '*.DS_Store' > /dev/null )
    echo "Unsigned package: $XPI"
    echo ""
    echo "To load it: Firefox -> about:debugging -> This Firefox ->"
    echo "  Load Temporary Add-on -> select the .xpi (or extension/manifest.json)."
    echo "For a permanently installable, auto-updating build, re-run with --sign."
fi

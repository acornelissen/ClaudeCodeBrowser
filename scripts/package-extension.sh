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

# Paths go through the environment rather than being interpolated into Python
# source: a checkout path containing a quote would otherwise break or inject.
VERSION=$(CCB_SRC="$SRC" python3 -c "import json,os; print(json.load(open(os.environ['CCB_SRC']+'/manifest.json'))['version'])")
EXT_ID=$(CCB_SRC="$SRC" python3 -c "import json,os; print(json.load(open(os.environ['CCB_SRC']+'/manifest.json'))['browser_specific_settings']['gecko']['id'])")
XPI="$DIST/claudecodebrowserx-${VERSION}.xpi"

# Where release assets live. The manifest's update_url points at
# releases/latest/download/updates.json (a stable URL), and each release's
# .xpi is downloaded from its versioned tag.
REPO_SLUG="${CCB_REPO_SLUG:-acornelissen/ClaudeCodeBrowserX}"
XPI_URL="https://github.com/${REPO_SLUG}/releases/download/v${VERSION}/claudecodebrowserx-${VERSION}.xpi"

mkdir -p "$DIST"

echo "Packaging ClaudeCodeBrowserX extension v${VERSION} (${EXT_ID})"


# Emit the Firefox update manifest. Called only after the artifact exists and
# has been validated: written up front, an aborted run left updates.json
# advertising a version with no matching .xpi.
write_update_manifest() {
    # Only the current id. Offering this build under a retired id cannot
    # rescue those installs: Firefox refuses an update whose id differs
    # ("Refusing to upgrade addon X to different ID Y", XPIInstall), so the
    # entry only made them download the .xpi and fail every update check.
CCB_EXT_ID="$EXT_ID" CCB_VERSION="$VERSION" CCB_XPI_URL="$XPI_URL" \
python3 > "$DIST/updates.json" <<'PYEOF'
import json, os

update = [{"version": os.environ["CCB_VERSION"],
           "update_link": os.environ["CCB_XPI_URL"]}]
print(json.dumps({"addons": {os.environ["CCB_EXT_ID"]: {"updates": update}}},
                 indent=2))
PYEOF
echo "Wrote update manifest: $DIST/updates.json"
}

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
    # Credentials go through the environment, not argv: anything in argv is
    # readable from `ps` by any process on the machine for the duration, and
    # this credential is what stands between release-write access and signed
    # code running with <all_urls> and nativeMessaging.
    WEB_EXT_API_KEY="$AMO_JWT_ISSUER" \
    WEB_EXT_API_SECRET="$AMO_JWT_SECRET" \
    web-ext sign \
        --source-dir "$SRC" \
        --artifacts-dir "$DIST" \
        --channel=unlisted
    # web-ext names the signed file with underscores; normalize to the name
    # the update manifest expects so update_link resolves.
    # Match this version's artifact rather than the newest file in dist/:
    # if web-ext exits 0 without producing one, picking by mtime copied the
    # PREVIOUS version's signed .xpi onto this version's filename, and the
    # release then advertised a version the archive does not contain.
    SIGNED=""
    for candidate in "$DIST"/*-"${VERSION}".xpi; do
        [ -f "$candidate" ] || continue
        [ "$candidate" = "$XPI" ] && continue
        SIGNED="$candidate"
    done
    if [ -n "$SIGNED" ]; then
        cp "$SIGNED" "$XPI"
    fi
    if [ ! -f "$XPI" ]; then
        echo "Error: no signed .xpi for version ${VERSION} was produced." >&2
        exit 1
    fi
    if ! unzip -l "$XPI" 2>/dev/null | grep -q 'META-INF/mozilla.rsa'; then
        echo "Error: $XPI carries no Mozilla signature." >&2
        exit 1
    fi
    PACKAGED_VERSION=$(unzip -p "$XPI" manifest.json | CCB_KEY=version python3 -c \
        "import json,os,sys; print(json.load(sys.stdin)[os.environ['CCB_KEY']])")
    if [ "$PACKAGED_VERSION" != "$VERSION" ]; then
        echo "Error: $XPI contains version $PACKAGED_VERSION, expected $VERSION." >&2
        exit 1
    fi
    write_update_manifest
    echo "Signed .xpi ready: $XPI (verified signed, version $PACKAGED_VERSION)"
    echo ""
    echo "To publish this as an auto-updating release:"
    echo "  1. Create GitHub release tag v${VERSION}"
    echo "  2. Upload BOTH assets: $XPI  and  $DIST/updates.json"
    echo "  Installed copies then update within ~24h (or via about:addons > Check for Updates)."
else
    # Refuse to clobber a signed build with an unsigned one. Both land at the
    # same path, and publish-release.sh uploads that path — so a stray
    # unsigned rebuild would otherwise ship an unsigned .xpi that nobody can
    # install permanently.
    if [ -f "$XPI" ] && unzip -l "$XPI" 2>/dev/null | grep -q 'META-INF/mozilla.rsa'; then
        echo "Error: $XPI is a SIGNED build. Refusing to overwrite it with an" >&2
        echo "unsigned one. Delete it first if that is really what you want:" >&2
        echo "  rm '$XPI'" >&2
        exit 1
    fi

    # Plain zip -> .xpi. The archive root must be the manifest, not a folder.
    # Exclude build state and editor droppings; web-ext already skips dotfiles
    # when signing, so the two paths produce the same archive contents.
    # '.*' catches top-level dotfiles, '*/.*' catches nested ones. With only
    # the latter, a stray extension/.env shipped.
    ( cd "$SRC" && zip -r -FS "$XPI" . \
        -x '*.DS_Store' -x '.*' -x '*/.*' > /dev/null )
    write_update_manifest
    echo "Unsigned package: $XPI"
    echo ""
    echo "To load it: Firefox -> about:debugging -> This Firefox ->"
    echo "  Load Temporary Add-on -> select the .xpi (or extension/manifest.json)."
    echo "For a permanently installable, auto-updating build, re-run with --sign."
fi

#!/bin/bash
#
# Publish a GitHub release for the current extension version and upload the
# signed .xpi and its updates.json as assets — the last step of the
# auto-update flow.
#
# Prerequisites:
#   - gh CLI, authenticated (gh auth login), OR a GITHUB_TOKEN for the curl path
#   - A signed build already in dist/:
#       ./scripts/package-extension.sh --sign     (or the .ps1 on Windows)
#
# Usage:
#   ./scripts/publish-release.sh                 # create/publish release vX.Y.Z
#   ./scripts/publish-release.sh --draft         # create it as a draft to review first

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$SCRIPT_DIR/extension"
DIST="$SCRIPT_DIR/dist"
REPO_SLUG="${CCB_REPO_SLUG:-nanogenomic/ClaudeCodeBrowser}"

DRAFT=0
[ "$1" = "--draft" ] && DRAFT=1

VERSION=$(python3 -c "import json; print(json.load(open('$SRC/manifest.json'))['version'])")
TAG="v${VERSION}"
XPI="$DIST/claudecodebrowser-${VERSION}.xpi"
UPDATES="$DIST/updates.json"

# Both assets must exist, or auto-update would break (updates.json would point
# at a missing .xpi).
for f in "$XPI" "$UPDATES"; do
    if [ ! -f "$f" ]; then
        echo "Error: $f not found. Build first: ./scripts/package-extension.sh --sign" >&2
        exit 1
    fi
done

NOTES="ClaudeCodeBrowser ${TAG}

Install: download \`claudecodebrowser-${VERSION}.xpi\` and open it in Firefox
(about:addons → gear → Install Add-on From File). Installed copies auto-update
from this release's updates.json.

See the README for the full changelog and safety notes."

echo "Publishing ${TAG} to ${REPO_SLUG}"
echo "  assets: $(basename "$XPI"), $(basename "$UPDATES")"

if command -v gh &> /dev/null; then
    DRAFT_FLAG=""
    [ "$DRAFT" = "1" ] && DRAFT_FLAG="--draft"
    gh release create "$TAG" "$XPI" "$UPDATES" \
        --repo "$REPO_SLUG" \
        --title "$TAG" \
        --notes "$NOTES" \
        $DRAFT_FLAG
    echo "Done. Release: https://github.com/${REPO_SLUG}/releases/tag/${TAG}"
else
    echo "gh CLI not found — falling back to the GitHub API via curl."
    if [ -z "$GITHUB_TOKEN" ]; then
        echo "Error: set GITHUB_TOKEN (a token with 'repo' scope) for the curl path." >&2
        exit 1
    fi
    API="https://api.github.com/repos/${REPO_SLUG}"
    UPLOADS="https://uploads.github.com/repos/${REPO_SLUG}"

    # Create the release; capture its id. Values are passed via the
    # environment (not interpolated into Python source) so quotes and newlines
    # in the notes can't break the JSON or inject code.
    BODY=$(CCB_TAG="$TAG" CCB_DRAFT="$DRAFT" python3 -c "
import json, os, sys
print(json.dumps({
    'tag_name': os.environ['CCB_TAG'],
    'name': os.environ['CCB_TAG'],
    'body': sys.stdin.read(),
    'draft': os.environ['CCB_DRAFT'] == '1',
}))" <<< "$NOTES")
    RESP=$(curl -sS -X POST "$API/releases" \
        -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        -d "$BODY")
    RELEASE_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" <<< "$RESP")
    if [ -z "$RELEASE_ID" ]; then
        echo "Error: failed to create release. Response:" >&2
        echo "$RESP" >&2
        exit 1
    fi

    # Upload both assets
    for f in "$XPI" "$UPDATES"; do
        name=$(basename "$f")
        ctype="application/octet-stream"
        [ "$name" = "updates.json" ] && ctype="application/json"
        curl -sS -X POST "$UPLOADS/releases/$RELEASE_ID/assets?name=$name" \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "Content-Type: $ctype" \
            --data-binary @"$f" > /dev/null
        echo "  uploaded $name"
    done
    echo "Done. Release: https://github.com/${REPO_SLUG}/releases/tag/${TAG}"
fi

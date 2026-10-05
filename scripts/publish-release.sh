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
REPO_SLUG="${CCB_REPO_SLUG:-acornelissen/ClaudeCodeBrowser}"

DRAFT=0
[ "$1" = "--draft" ] && DRAFT=1

VERSION=$(CCB_SRC="$SRC" python3 -c "import json,os; print(json.load(open(os.environ['CCB_SRC']+'/manifest.json'))['version'])")
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

# Existence is not enough. Publishing an unsigned or mismatched .xpi breaks
# auto-update for every existing install, silently and for everyone.
if ! unzip -l "$XPI" 2>/dev/null | grep -q 'META-INF/mozilla.rsa'; then
    echo "Error: $XPI is NOT signed. Firefox will refuse it, and publishing it" >&2
    echo "would break auto-update for every installed copy. Run --sign first." >&2
    exit 1
fi

CHECK=$(CCB_XPI="$XPI" CCB_UPDATES="$UPDATES" CCB_VERSION="$VERSION" python3 <<'PYEOF'
import json, os, sys, zipfile

with zipfile.ZipFile(os.environ['CCB_XPI']) as archive:
    manifest = json.loads(archive.read('manifest.json'))
packaged = manifest['version']
ext_id = manifest['browser_specific_settings']['gecko']['id']
updates = json.loads(open(os.environ['CCB_UPDATES']).read())
expected = os.environ['CCB_VERSION']

problems = []
if packaged != expected:
    problems.append(f'xpi contains version {packaged}, expected {expected}')
if ext_id not in updates.get('addons', {}):
    problems.append(f'updates.json has no entry for {ext_id}')
else:
    advertised = [u['version'] for u in updates['addons'][ext_id]['updates']]
    if expected not in advertised:
        problems.append(
            f'updates.json advertises {advertised} for {ext_id}, not {expected}')
print('\n'.join(problems))
PYEOF
)
if [ -n "$CHECK" ]; then
    echo "Error: release assets are inconsistent:" >&2
    echo "$CHECK" >&2
    exit 1
fi
echo "  verified: signed, version $VERSION, updates.json agrees"

# The release is cut from the commit checked out here, and only once CI has
# passed on it. Without --target, GitHub tagged whatever main pointed at,
# which need not be the commit that was built, tested and signed.
HEAD_SHA=$(git -C "$SCRIPT_DIR" rev-parse HEAD)
CI_URL="repos/${REPO_SLUG}/commits/${HEAD_SHA}/check-runs?check_name=test"
if command -v gh &> /dev/null; then
    CI=$(gh api "$CI_URL" --jq '.check_runs[0].conclusion // "none"' 2>/dev/null) || CI="unknown"
elif [ -n "$GITHUB_TOKEN" ]; then
    CI=$(printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" | \
        curl -sS --config - -H "Accept: application/vnd.github+json" \
        "https://api.github.com/${CI_URL}" | python3 -c "
import json, sys
runs = json.load(sys.stdin).get('check_runs') or [{}]
print(runs[0].get('conclusion') or 'none')") || CI="unknown"
else
    CI="unknown"
fi
if [ "$CI" != "success" ]; then
    echo "Error: CI ('test') has not passed on ${HEAD_SHA} (it reports: ${CI})." >&2
    echo "Push this commit and wait for the test workflow to pass, then retry." >&2
    exit 1
fi
echo "  verified: CI passed on ${HEAD_SHA}"

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
        --target "$HEAD_SHA" \
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
    BODY=$(CCB_TAG="$TAG" CCB_DRAFT="$DRAFT" CCB_SHA="$HEAD_SHA" python3 -c "
import json, os, sys
print(json.dumps({
    'tag_name': os.environ['CCB_TAG'],
    'target_commitish': os.environ['CCB_SHA'],
    'name': os.environ['CCB_TAG'],
    'body': sys.stdin.read(),
    'draft': os.environ['CCB_DRAFT'] == '1',
}))" <<< "$NOTES")
    RESP=$(printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" | \
        curl -sS --fail-with-body -X POST "$API/releases" \
        --config - \
        -H "Accept: application/vnd.github+json" \
        -d "$BODY") || {
            echo "Error: creating the release failed:" >&2
            echo "$RESP" >&2
            exit 1
        }
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
        # --fail-with-body: without it curl exits 0 on 422 (a duplicate asset
        # name on a re-run is the common case) and the script reported success
        # for a release missing an asset. Credentials go on stdin, not argv.
        if ! printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" | \
             curl -sS --fail-with-body -X POST \
               "$UPLOADS/releases/$RELEASE_ID/assets?name=$name" \
               --config - \
               -H "Content-Type: $ctype" \
               --data-binary @"$f" > /dev/null; then
            echo "Error: upload of $name failed. The release is incomplete;" >&2
            echo "delete it and retry rather than leaving it half-published." >&2
            exit 1
        fi
        echo "  uploaded $name"
    done
    echo "Done. Release: https://github.com/${REPO_SLUG}/releases/tag/${TAG}"
fi

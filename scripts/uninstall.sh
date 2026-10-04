#!/bin/bash
#
# ClaudeCodeBrowser Uninstallation Script
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

INSTALL_DIR="$HOME/.claudecodebrowser"
if [ "$(uname)" = "Darwin" ]; then
    FIREFOX_NATIVE_MANIFESTS_DIR="$HOME/Library/Application Support/Mozilla/NativeMessagingHosts"
else
    FIREFOX_NATIVE_MANIFESTS_DIR="$HOME/.mozilla/native-messaging-hosts"
fi

echo -e "${YELLOW}ClaudeCodeBrowser Uninstaller${NC}"
echo ""

read -p "This will remove ClaudeCodeBrowser. Continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

# Remove native messaging manifest
if [ -f "$FIREFOX_NATIVE_MANIFESTS_DIR/claudecodebrowser.json" ]; then
    rm "$FIREFOX_NATIVE_MANIFESTS_DIR/claudecodebrowser.json"
    echo -e "${GREEN}✓ Removed Firefox native messaging manifest${NC}"
fi

# Remove symlinks
if [ -L "$HOME/bin/claudecodebrowser-server" ]; then
    rm "$HOME/bin/claudecodebrowser-server"
    echo -e "${GREEN}✓ Removed symlink: claudecodebrowser-server${NC}"
fi

if [ -L "$HOME/bin/browser-agent" ]; then
    rm "$HOME/bin/browser-agent"
    echo -e "${GREEN}✓ Removed symlink: browser-agent${NC}"
elif [ -e "$HOME/bin/browser-agent" ]; then
    echo -e "${YELLOW}⚠ ~/bin/browser-agent is not our symlink; leaving it alone${NC}"
fi

# Ask about screenshots. "Preserved" has to mean preserved: this used to print
# a reassurance and then delete the parent directory containing them.
KEEP_SCREENSHOTS=0
if [ -d "$INSTALL_DIR/screenshots" ] && [ "$(ls -A "$INSTALL_DIR/screenshots" 2>/dev/null)" ]; then
    read -p "Remove saved screenshots? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        KEEP_SCREENSHOTS=1
    fi
fi

if [ "$KEEP_SCREENSHOTS" = "1" ]; then
    KEEP_DIR="$HOME/claudecodebrowser-screenshots"
    mkdir -p "$KEEP_DIR"
    # cp -R then remove, so a failure cannot lose the originals.
    if cp -R "$INSTALL_DIR/screenshots/." "$KEEP_DIR/" 2>/dev/null; then
        chmod 700 "$KEEP_DIR" 2>/dev/null || true
        echo -e "${GREEN}✓ Screenshots moved to $KEEP_DIR${NC}"
    else
        echo -e "${YELLOW}⚠ Could not copy screenshots; leaving $INSTALL_DIR in place${NC}"
        echo "   Move them yourself, then re-run this script."
        exit 1
    fi
fi

# Remove installation directory. This also removes the API token, safety.json
# and the logs (which include the audit log).
if [ -d "$INSTALL_DIR" ]; then
    echo "Removing $INSTALL_DIR (API token, safety.json, logs and audit log)"
    rm -rf "$INSTALL_DIR"
    echo -e "${GREEN}✓ Removed installation directory${NC}"
fi

# The Chrome build instructs users to install a native-messaging manifest too.
for CHROME_MANIFEST in \
    "$HOME/.config/google-chrome/NativeMessagingHosts/claudecodebrowser.json" \
    "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/claudecodebrowser.json"; do
    if [ -f "$CHROME_MANIFEST" ]; then
        rm "$CHROME_MANIFEST"
        echo -e "${GREEN}✓ Removed Chrome native messaging manifest${NC}"
    fi
done

echo ""
echo -e "${GREEN}ClaudeCodeBrowser has been uninstalled.${NC}"
echo ""
echo "To remove the Firefox extension itself:"
echo "  1. Open about:addons in Firefox (menu > Add-ons and themes)"
echo "  2. Find ClaudeCodeBrowser under Extensions"
echo "  3. Click the ... menu next to it and choose Remove"
echo ""
echo "If it was loaded temporarily via about:debugging, it will disappear"
echo "on the next Firefox restart."
echo ""
echo "If you registered the MCP server with Claude Code, also run:"
echo "  claude mcp remove claudecodebrowser"

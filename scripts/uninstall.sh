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
fi

# Ask about screenshots
if [ -d "$INSTALL_DIR/screenshots" ] && [ "$(ls -A "$INSTALL_DIR/screenshots" 2>/dev/null)" ]; then
    read -p "Remove saved screenshots? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$INSTALL_DIR/screenshots"
        echo -e "${GREEN}✓ Removed screenshots${NC}"
    else
        echo "Screenshots preserved at: $INSTALL_DIR/screenshots"
    fi
fi

# Remove installation directory
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo -e "${GREEN}✓ Removed installation directory${NC}"
fi

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

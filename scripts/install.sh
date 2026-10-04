#!/bin/bash
#
# ClaudeCodeBrowser Installation Script
#
# This script installs the ClaudeCodeBrowser components:
# - Firefox extension
# - Native messaging host
# - MCP server
# - Browser automation agent
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
INSTALL_DIR="$HOME/.claudecodebrowser"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
    FIREFOX_NATIVE_MANIFESTS_DIR="$HOME/Library/Application Support/Mozilla/NativeMessagingHosts"
else
    FIREFOX_NATIVE_MANIFESTS_DIR="$HOME/.mozilla/native-messaging-hosts"
fi

echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║       ClaudeCodeBrowser Installation Script                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check dependencies
echo -e "${YELLOW}Checking dependencies...${NC}"

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is required but not installed.${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "${GREEN}✓ Python $PYTHON_VERSION found${NC}"

# Check for Firefox
if command -v firefox &> /dev/null; then
    FIREFOX_VERSION=$(firefox --version 2>/dev/null | head -n1)
    echo -e "${GREEN}✓ $FIREFOX_VERSION found${NC}"
elif [ -d "/Applications/Firefox.app" ] || [ -d "$HOME/Applications/Firefox.app" ]; then
    echo -e "${GREEN}✓ Firefox.app found${NC}"
else
    echo -e "${YELLOW}⚠ Firefox not found${NC}"
fi

# Create installation directory
echo -e "\n${YELLOW}Creating installation directory...${NC}"
mkdir -p "$INSTALL_DIR"/{native-host,mcp-server,agent,screenshots,logs}
echo -e "${GREEN}✓ Created $INSTALL_DIR${NC}"

# Copy files
echo -e "\n${YELLOW}Installing components...${NC}"

# Copy native host
cp "$SCRIPT_DIR/native-host/claudecodebrowser_host.py" "$INSTALL_DIR/native-host/"
chmod +x "$INSTALL_DIR/native-host/claudecodebrowser_host.py"
echo -e "${GREEN}✓ Native messaging host installed${NC}"

# Copy MCP server
cp "$SCRIPT_DIR/mcp-server/server.py" "$INSTALL_DIR/mcp-server/"
cp "$SCRIPT_DIR/mcp-server/safety.py" "$INSTALL_DIR/mcp-server/"
cp "$SCRIPT_DIR/mcp-server/headless_backend.py" "$INSTALL_DIR/mcp-server/"
cp "$SCRIPT_DIR/mcp-server/stdio_wrapper.py" "$INSTALL_DIR/mcp-server/"
cp "$SCRIPT_DIR/mcp-server/mcp_config.json" "$INSTALL_DIR/mcp-server/"
chmod +x "$INSTALL_DIR/mcp-server/server.py"
echo -e "${GREEN}✓ MCP server installed${NC}"

# Copy agent
cp "$SCRIPT_DIR/agent/browser_agent.py" "$INSTALL_DIR/agent/"
chmod +x "$INSTALL_DIR/agent/browser_agent.py"
echo -e "${GREEN}✓ Browser agent installed${NC}"

# Install native messaging manifest for Firefox
echo -e "\n${YELLOW}Installing Firefox native messaging manifest...${NC}"
mkdir -p "$FIREFOX_NATIVE_MANIFESTS_DIR"

NATIVE_HOST_PATH="$INSTALL_DIR/native-host/claudecodebrowser_host.py"
if [ "$OS" = "Darwin" ]; then
    # Firefox on macOS launches native hosts with a minimal PATH (launchd's),
    # so "#!/usr/bin/env python3" may not resolve (e.g. Homebrew installs).
    # Use a launcher with the absolute python3 path resolved at install time.
    # Prefer a stable launcher over a version-pinned install path: with mise
    # active, command -v python3 resolves to .../installs/python/3.12/bin, which
    # disappears when that version is pruned and the bridge then dies with no
    # error the user can see.
    PYTHON_BIN="$(command -v python3)"
    if [ -x "$HOME/.local/share/mise/shims/python3" ]; then
        PYTHON_BIN="$HOME/.local/share/mise/shims/python3"
    fi
    cat > "$INSTALL_DIR/native-host/run_host.sh" << WRAPEOF
#!/bin/bash
exec "$PYTHON_BIN" "$INSTALL_DIR/native-host/claudecodebrowser_host.py"
WRAPEOF
    chmod +x "$INSTALL_DIR/native-host/run_host.sh"
    NATIVE_HOST_PATH="$INSTALL_DIR/native-host/run_host.sh"
fi

# Read the extension ID from the manifest rather than repeating it here.
# Firefox only talks to the native host if this list matches the ID exactly,
# and a copy that drifts out of sync breaks the bridge silently.
EXT_ID=$(CCB_DIR="$SCRIPT_DIR" python3 -c "import json,os; print(json.load(open(os.environ['CCB_DIR']+'/extension/manifest.json'))['browser_specific_settings']['gecko']['id'])")
if [ -z "$EXT_ID" ]; then
    echo -e "${RED}Error: could not read the extension ID from extension/manifest.json${NC}"
    exit 1
fi

cat > "$FIREFOX_NATIVE_MANIFESTS_DIR/claudecodebrowser.json" << EOF
{
  "name": "claudecodebrowser",
  "description": "ClaudeCodeBrowser Native Messaging Host",
  "path": "$NATIVE_HOST_PATH",
  "type": "stdio",
  "allowed_extensions": [
    "$EXT_ID"
  ]
}
EOF
echo -e "${GREEN}✓ Firefox native messaging manifest installed (extension $EXT_ID)${NC}"

# Create convenience scripts
echo -e "\n${YELLOW}Creating convenience scripts...${NC}"

# Start server script
cat > "$INSTALL_DIR/start-server.sh" << 'EOF'
#!/bin/bash
cd "$(dirname "$0")/mcp-server"
python3 server.py
EOF
chmod +x "$INSTALL_DIR/start-server.sh"

# Agent script
cat > "$INSTALL_DIR/browser-agent" << EOF
#!/bin/bash
python3 "$INSTALL_DIR/agent/browser_agent.py" "\$@"
EOF
chmod +x "$INSTALL_DIR/browser-agent"

# Create symlinks in ~/bin if it exists
if [ -d "$HOME/bin" ]; then
    # browser-agent is a generic name and ln -sf replaces a regular file, so
    # this used to destroy a user's own script with no warning (and uninstall
    # only reverses it when it is still our symlink).
    for link in claudecodebrowser-server browser-agent; do
        target="$INSTALL_DIR/browser-agent"
        [ "$link" = "claudecodebrowser-server" ] && target="$INSTALL_DIR/start-server.sh"
        if [ -e "$HOME/bin/$link" ] && [ ! -L "$HOME/bin/$link" ]; then
            echo -e "${YELLOW}⚠ ~/bin/$link exists and is not a symlink; leaving it alone${NC}"
            continue
        fi
        ln -sf "$target" "$HOME/bin/$link"
    done
    echo -e "${GREEN}✓ Created symlinks in ~/bin${NC}"
fi

echo -e "${GREEN}✓ Convenience scripts created${NC}"

# Install Python dependencies
echo -e "\n${YELLOW}Checking Python dependencies...${NC}"

# Check for websockets
if python3 -c "import websockets" 2>/dev/null; then
    echo -e "${GREEN}✓ websockets module found${NC}"
else
    echo -e "${YELLOW}Installing websockets module...${NC}"
    # python3 -m pip, not pip3: the capability check above uses python3, and on
    # a mixed mise/Homebrew machine pip3 can belong to a different interpreter,
    # so the install "succeeded" into a Python the server never uses.
    if python3 -m pip install --user websockets; then
        echo -e "${GREEN}✓ websockets installed${NC}"
    else
        echo -e "${YELLOW}⚠ Could not install websockets (WebSocket support will be disabled)${NC}"
    fi
fi

# Print Firefox extension installation instructions
echo -e "\n${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}Firefox Extension Installation:${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "The Firefox extension needs to be installed manually:"
echo ""
echo "Option 1: Temporary installation (for testing)"
echo "  1. Open Firefox and navigate to: about:debugging"
echo "  2. Click 'This Firefox' in the left sidebar"
echo "  3. Click 'Load Temporary Add-on...'"
echo "  4. Navigate to: $SCRIPT_DIR/extension"
echo "  5. Select 'manifest.json'"
echo "  Note: Firefox drops temporary add-ons on restart, and a temporary"
echo "  load only overrides an installed copy of the same ID for that session."
echo ""
echo "Option 2: Permanent installation (requires an AMO-signed build)"
echo "  Release builds of Firefox refuse unsigned extensions, and"
echo "  xpinstall.signatures.required only works in Developer Edition,"
echo "  Nightly and ESR - not on release. So sign it:"
echo "    mise install                              # gets web-ext"
echo "    cp mise.local.toml.example mise.local.toml && \$EDITOR mise.local.toml"
echo "    mise run sign                             # writes a signed .xpi to dist/"
echo "  Then open dist/*.xpi in Firefox (about:addons > gear >"
echo "  'Install Add-on From File'). See 'Signing' in the README."
echo ""

# Print Claude Code MCP configuration
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}Claude Code MCP Configuration:${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "Add this to your Claude Code settings (~/.claude/settings.json):"
echo ""
echo '{
  "mcpServers": {
    "claudecodebrowser": {
      "command": "python3",
      "args": ["'$INSTALL_DIR'/mcp-server/stdio_wrapper.py"]
    }
  }
}'
echo ""

# Print usage instructions
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}Usage:${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "1. Start the MCP server:"
echo "   $INSTALL_DIR/start-server.sh"
echo ""
echo "2. Use the browser agent (interactive mode):"
echo "   $INSTALL_DIR/browser-agent -i"
echo ""
echo "3. Take a screenshot:"
echo "   $INSTALL_DIR/browser-agent --screenshot"
echo ""
echo "4. Navigate to a URL:"
echo "   $INSTALL_DIR/browser-agent --navigate https://example.com"
echo ""

# Built-in resilience info
echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}Auto-Restart & Crash Recovery (Built-in):${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo "The extension includes built-in resilience:"
echo "  • Native host auto-starts MCP server when needed"
echo "  • Health monitoring restarts server on crashes"
echo "  • Exponential backoff prevents restart storms"
echo "  • Extension auto-reconnects to native host"
echo ""
echo "No external process managers (PM2/systemd) required!"
echo ""

echo -e "${GREEN}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║       Installation complete!                                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

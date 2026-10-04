# ClaudeCodeBrowser Installation Script for Windows
#
# Installs the native messaging host, MCP server, and browser agent, and
# registers the native host with Firefox via the Windows registry.
#
# Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1

$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:USERPROFILE ".claudecodebrowser"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Write-Host ""
Write-Host "+--------------------------------------------------------------+"
Write-Host "|       ClaudeCodeBrowser Installation Script (Windows)        |"
Write-Host "+--------------------------------------------------------------+"
Write-Host ""

# --- Check dependencies -------------------------------------------------

$Python = $null
foreach ($candidate in @("python", "python3", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        $version = & $cmd.Source --version 2>&1
        if ($version -match "Python 3") {
            $Python = $cmd.Source
            Write-Host "[ok] $version found at $Python"
            break
        }
    }
}
if (-not $Python) {
    Write-Host "[error] Python 3 is required but was not found on PATH." -ForegroundColor Red
    Write-Host "        Install it from https://www.python.org/downloads/ and re-run."
    exit 1
}

$Firefox = Get-Command firefox -ErrorAction SilentlyContinue
if (-not $Firefox) {
    foreach ($dir in @("$env:ProgramFiles\Mozilla Firefox", "${env:ProgramFiles(x86)}\Mozilla Firefox")) {
        if (Test-Path (Join-Path $dir "firefox.exe")) { $Firefox = $dir; break }
    }
}
if ($Firefox) {
    Write-Host "[ok] Firefox found"
} else {
    Write-Host "[warn] Firefox not found - install it before using the extension" -ForegroundColor Yellow
}

# --- Copy files ---------------------------------------------------------

Write-Host ""
Write-Host "Installing components to $InstallDir ..."

foreach ($sub in @("native-host", "mcp-server", "agent", "screenshots", "logs")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir $sub) | Out-Null
}

Copy-Item (Join-Path $RepoRoot "native-host\claudecodebrowser_host.py") (Join-Path $InstallDir "native-host\")
foreach ($f in @("server.py", "safety.py", "headless_backend.py", "stdio_wrapper.py", "mcp_config.json")) {
    Copy-Item (Join-Path $RepoRoot "mcp-server\$f") (Join-Path $InstallDir "mcp-server\")
}
Copy-Item (Join-Path $RepoRoot "agent\browser_agent.py") (Join-Path $InstallDir "agent\")
Write-Host "[ok] Components installed"

# --- Native messaging host wrapper --------------------------------------

# Firefox on Windows can only launch .exe/.bat native hosts, not .py files,
# so generate a .bat wrapper with the absolute Python path baked in.
$HostBat = Join-Path $InstallDir "native-host\run_host.bat"
$HostPy = Join-Path $InstallDir "native-host\claudecodebrowser_host.py"
@"
@echo off
"$Python" "$HostPy" %*
"@ | Set-Content -Path $HostBat -Encoding ASCII
Write-Host "[ok] Native host wrapper created: $HostBat"

# --- Native messaging manifest + registry key ---------------------------

$ManifestPath = Join-Path $InstallDir "native-host\claudecodebrowser.json"

# Read the extension ID from the manifest rather than repeating it here.
# Firefox only talks to the native host if this list matches the ID exactly,
# and a copy that drifts out of sync breaks the bridge silently.
$ExtManifest = Get-Content (Join-Path $RepoRoot "extension\manifest.json") -Raw | ConvertFrom-Json
$ExtId = $ExtManifest.browser_specific_settings.gecko.id
if (-not $ExtId) {
    Write-Host "Error: could not read the extension ID from extension\manifest.json"
    exit 1
}

$Manifest = @{
    name = "claudecodebrowser"
    description = "ClaudeCodeBrowser Native Messaging Host"
    path = $HostBat
    type = "stdio"
    allowed_extensions = @($ExtId)
}
$Manifest | ConvertTo-Json | Set-Content -Path $ManifestPath -Encoding UTF8

# Firefox on Windows finds native hosts through the registry, not a directory.
$RegKey = "HKCU:\Software\Mozilla\NativeMessagingHosts\claudecodebrowser"
New-Item -Path $RegKey -Force | Out-Null
Set-ItemProperty -Path $RegKey -Name "(Default)" -Value $ManifestPath
Write-Host "[ok] Registry key set: $RegKey -> $ManifestPath"

# --- Instructions -------------------------------------------------------

Write-Host ""
Write-Host "=============================================================="
Write-Host "Firefox Extension Installation:"
Write-Host "=============================================================="
Write-Host ""
Write-Host "  1. Open Firefox and navigate to: about:debugging"
Write-Host "  2. Click 'This Firefox' in the left sidebar"
Write-Host "  3. Click 'Load Temporary Add-on...'"
Write-Host "  4. Select: $RepoRoot\extension\manifest.json"
Write-Host ""
Write-Host "=============================================================="
Write-Host "Claude Code MCP Configuration:"
Write-Host "=============================================================="
Write-Host ""
Write-Host "  claude mcp add claudecodebrowser -- `"$Python`" `"$InstallDir\mcp-server\stdio_wrapper.py`""
Write-Host ""
Write-Host "+--------------------------------------------------------------+"
Write-Host "|       Installation complete!                                 |"
Write-Host "+--------------------------------------------------------------+"

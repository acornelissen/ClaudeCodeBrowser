# ClaudeCodeBrowserX Uninstallation Script for Windows
#
# Removes the native messaging host, its registry key, the MCP server and the
# browser agent. There was previously no Windows uninstaller at all, so an
# install made by install.ps1 could not be removed by any script.
#
# Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1

$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:USERPROFILE ".claudecodebrowserx"
$RegKey = "HKCU:\Software\Mozilla\NativeMessagingHosts\claudecodebrowserx"

Write-Host ""
Write-Host "ClaudeCodeBrowserX Uninstaller (Windows)"
Write-Host ""

$reply = Read-Host "This will remove ClaudeCodeBrowserX. Continue? (y/N)"
if ($reply -notmatch '^[Yy]') {
    Write-Host "Cancelled."
    exit 0
}

# Firefox finds native hosts through the registry on Windows.
if (Test-Path $RegKey) {
    Remove-Item -Path $RegKey -Recurse -Force
    Write-Host "[ok] Removed native messaging registry key"
}
# The pre-rename host name the installer keeps pointing at the new host.
$LegacyRegKey = "HKCU:\Software\Mozilla\NativeMessagingHosts\claudecodebrowser"
if (Test-Path $LegacyRegKey) {
    Remove-Item -Path $LegacyRegKey -Recurse -Force
    Write-Host "[ok] Removed the pre-rename registry key"
}

# Chrome, if the experimental build's manifest was installed.
$ChromeManifest = Join-Path $env:LOCALAPPDATA `
    "Google\Chrome\User Data\NativeMessagingHosts\claudecodebrowserx.json"
if (Test-Path $ChromeManifest) {
    Remove-Item $ChromeManifest -Force
    Write-Host "[ok] Removed Chrome native messaging manifest"
}

# Screenshots are the only user data in here; keep them if asked.
$Screenshots = Join-Path $InstallDir "screenshots"
if ((Test-Path $Screenshots) -and (Get-ChildItem $Screenshots -Force | Measure-Object).Count -gt 0) {
    $keep = Read-Host "Remove saved screenshots? (y/N)"
    if ($keep -notmatch '^[Yy]') {
        $KeepDir = Join-Path $env:USERPROFILE "claudecodebrowserx-screenshots"
        New-Item -ItemType Directory -Path $KeepDir -Force | Out-Null
        Copy-Item -Path (Join-Path $Screenshots "*") -Destination $KeepDir -Recurse -Force
        Write-Host "[ok] Screenshots moved to $KeepDir"
    }
}

if (Test-Path $InstallDir) {
    Write-Host "Removing $InstallDir (API token, safety.json, logs and audit log)"
    Remove-Item $InstallDir -Recurse -Force
    Write-Host "[ok] Removed installation directory"
}

Write-Host ""
Write-Host "ClaudeCodeBrowserX has been uninstalled."
Write-Host ""
Write-Host "To remove the Firefox extension itself:"
Write-Host "  1. Open about:addons in Firefox"
Write-Host "  2. Find ClaudeCodeBrowserX under Extensions"
Write-Host "  3. Click the ... menu next to it and choose Remove"
Write-Host ""
Write-Host "If you registered the MCP server with Claude Code, also run:"
Write-Host "  claude mcp remove claudecodebrowserx"
Write-Host "  claude mcp remove claudecodebrowser    # if registered before the rename"

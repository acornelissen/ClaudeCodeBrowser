# Package (and optionally sign) the Firefox extension into a versioned .xpi
# on Windows. PowerShell equivalent of scripts/package-extension.sh.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\package-extension.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\package-extension.ps1 -Sign
#
# Signing uses Mozilla's web-ext (npm install -g web-ext) and an AMO API key
# in $env:AMO_JWT_ISSUER / $env:AMO_JWT_SECRET
# (https://addons.mozilla.org/developers/addon/api/key/).

param([switch]$Sign)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Src = Join-Path $RepoRoot "extension"
$Dist = Join-Path $RepoRoot "dist"

$manifest = Get-Content (Join-Path $Src "manifest.json") -Raw | ConvertFrom-Json
$Version = $manifest.version
$ExtId = $manifest.browser_specific_settings.gecko.id
$Xpi = Join-Path $Dist "claudecodebrowser-$Version.xpi"

$RepoSlug = if ($env:CCB_REPO_SLUG) { $env:CCB_REPO_SLUG } else { "acornelissen/ClaudeCodeBrowser" }
$XpiUrl = "https://github.com/$RepoSlug/releases/download/v$Version/claudecodebrowser-$Version.xpi"

New-Item -ItemType Directory -Force -Path $Dist | Out-Null

Write-Host "Packaging ClaudeCodeBrowser extension v$Version ($ExtId)"

# Firefox update manifest, so installed copies can auto-update.
$updates = [ordered]@{
    addons = [ordered]@{
        "$ExtId" = [ordered]@{
            updates = @(
                [ordered]@{ version = $Version; update_link = $XpiUrl }
            )
        }
    }
}
$updatesPath = Join-Path $Dist "updates.json"
$updates | ConvertTo-Json -Depth 6 | Set-Content -Path $updatesPath -Encoding UTF8
Write-Host "Wrote update manifest: $updatesPath"

if ($Sign) {
    if (-not (Get-Command web-ext -ErrorAction SilentlyContinue)) {
        Write-Host "Error: web-ext not found. Install it with: npm install -g web-ext" -ForegroundColor Red
        exit 1
    }
    if (-not $env:AMO_JWT_ISSUER -or -not $env:AMO_JWT_SECRET) {
        Write-Host "Error: set `$env:AMO_JWT_ISSUER and `$env:AMO_JWT_SECRET (AMO API key page)." -ForegroundColor Red
        exit 1
    }
    Write-Host "Signing via AMO (channel: unlisted, self-distribution)..."
    # Credentials via the environment, not argv: argv is readable from any
    # process listing for the duration.
    $env:WEB_EXT_API_KEY = $env:AMO_JWT_ISSUER
    $env:WEB_EXT_API_SECRET = $env:AMO_JWT_SECRET
    try {
        web-ext sign --source-dir $Src --artifacts-dir $Dist --channel=unlisted
    } finally {
        Remove-Item Env:WEB_EXT_API_KEY -ErrorAction SilentlyContinue
        Remove-Item Env:WEB_EXT_API_SECRET -ErrorAction SilentlyContinue
    }

    # Match this version's artifact, not the newest file in dist/: picking by
    # mtime could copy the previous version's signed .xpi onto this version's
    # filename, and the release would then advertise a version the archive
    # does not contain.
    $signed = Get-ChildItem $Dist -Filter "*-$Version.xpi" |
        Where-Object { $_.FullName -ne $Xpi } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($signed) { Copy-Item $signed.FullName $Xpi -Force }
    if (-not (Test-Path $Xpi)) {
        Write-Host "Error: no signed .xpi for version $Version was produced." -ForegroundColor Red
        exit 1
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Xpi)
    try {
        $isSigned = $zip.Entries.FullName -contains 'META-INF/mozilla.rsa'
    } finally { $zip.Dispose() }
    if (-not $isSigned) {
        Write-Host "Error: $Xpi carries no Mozilla signature." -ForegroundColor Red
        exit 1
    }
    Write-Host "Signed .xpi ready: $Xpi (verified signed)"
    Write-Host ""
    Write-Host "To publish as an auto-updating release:"
    Write-Host "  1. Create GitHub release tag v$Version"
    Write-Host "  2. Upload BOTH assets: $Xpi and $updatesPath"
} else {
    # Refuse to clobber a signed build with an unsigned one. Both land on the
    # same path, and publish-release.sh uploads that path, so a stray rebuild
    # here would ship a build nobody can install permanently. The bash script
    # has guarded this; this one deleted the signed artifact unconditionally.
    if (Test-Path $Xpi) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($Xpi)
        try {
            $wasSigned = $zip.Entries.FullName -contains 'META-INF/mozilla.rsa'
        } finally { $zip.Dispose() }
        if ($wasSigned) {
            Write-Host "Error: $Xpi is a SIGNED build. Refusing to overwrite it" -ForegroundColor Red
            Write-Host "with an unsigned one. Delete it first if that is really" -ForegroundColor Red
            Write-Host "what you want." -ForegroundColor Red
            exit 1
        }
        Remove-Item $Xpi
    }

    # Zip the extension contents with manifest.json at the archive root,
    # excluding build state and dotfiles so the two unsigned paths agree.
    $staging = Join-Path ([System.IO.Path]::GetTempPath()) ("ccb-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        Copy-Item -Path (Join-Path $Src "*") -Destination $staging -Recurse -Force
        Get-ChildItem $staging -Recurse -Force |
            Where-Object { $_.Name -like ".*" -or $_.Name -eq "Thumbs.db" } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Compress-Archive -Path (Join-Path $staging "*") -DestinationPath "$Xpi.zip" -Force
        Move-Item "$Xpi.zip" $Xpi -Force
    } finally {
        Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Unsigned package: $Xpi"
    Write-Host ""
    Write-Host "To load it: Firefox -> about:debugging -> This Firefox ->"
    Write-Host "  Load Temporary Add-on -> select the .xpi (or extension\manifest.json)."
    Write-Host "For a permanent, auto-updating build, re-run with -Sign."
}

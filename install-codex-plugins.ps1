# Bootstrap: clone or update woodor-ai/plugins then run the interactive installer.
# Usage (one-liner):
#   iwr -useb https://raw.githubusercontent.com/woodor-ai/plugins/main/install-codex-plugins.ps1 | iex
#
# Also copied verbatim to ~/.agent-meeting/bin/codex-plugins.ps1 by install-codex.py
# after the first install (with a codex-plugins.cmd launcher alongside it) — run
# `codex-plugins` locally afterwards instead of re-pasting the one-liner. Any
# extra args (only meaningful when run via -File, not piped through iex) are
# forwarded to install-codex.py.

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/woodor-ai/plugins"
$Dest    = Join-Path $env:USERPROFILE ".codex\plugins-src"

# dependency checks
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git not found. Install from https://git-scm.com and re-run."
    exit 1
}

$py = $null
foreach ($c in @("python", "py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) {
    Write-Error "python not found. Install Python 3.9+ from https://python.org and re-run."
    exit 1
}

# clone or update
if (Test-Path (Join-Path $Dest ".git")) {
    Write-Host "Updating $Dest ..."
    git -C $Dest pull --ff-only
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    Write-Host "Cloning $RepoUrl to $Dest ..."
    git clone $RepoUrl $Dest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host ""
Write-Host "Running interactive installer ..."
& $py (Join-Path $Dest "install-codex.py") @args
exit $LASTEXITCODE

# mycodex: bridge a codex session into agent-meeting, or update woodor-ai/plugins.
#
#   mycodex --update                     pull (or clone) woodor-ai/plugins and
#                                         rerun the interactive installer; any
#                                         extra args are forwarded to it.
#   mycodex [<name>] [--port N] [--control-url URL] [--no-codex]
#                                         start (or resume) a bridged codex
#                                         session — needs agent-meeting installed
#                                         (run `mycodex --update` first).
#
# Single source of truth, copied verbatim (no per-install templating) into
# ~/.agent-meeting/bin/mycodex.ps1 by both install-codex.py (root installer,
# unconditional — makes `--update` work even before agent-meeting is installed)
# and session-bootstrap.py (agent-meeting's own SessionStart hook — self-heals
# this file if bin/ is ever wiped and rebuilt). Fully self-locating: no absolute
# path is baked in, so the file is byte-identical everywhere it is copied.
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RestArgs
)

$ErrorActionPreference = "Stop"

$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$BinDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MeetingHome = if ($env:MEETING_HOME) { $env:MEETING_HOME } else { Split-Path -Parent $BinDir }
$AmCodexMeeting = Join-Path $CodexHome "plugins\agent-meeting\codex\codex-meeting.py"
$Vpy = Join-Path $MeetingHome "venv\Scripts\python.exe"

if ($RestArgs.Count -gt 0 -and $RestArgs[0] -eq "--update") {
    $updateArgs = @()
    if ($RestArgs.Count -gt 1) { $updateArgs = $RestArgs[1..($RestArgs.Count - 1)] }

    $RepoUrl = "https://github.com/woodor-ai/plugins"
    $Dest = Join-Path $CodexHome "plugins-src"

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
    & $py (Join-Path $Dest "install-codex.py") @updateArgs
    exit $LASTEXITCODE
}

if (-not (Test-Path $Vpy) -or -not (Test-Path $AmCodexMeeting)) {
    Write-Error "mycodex: agent-meeting is not installed - run 'mycodex --update' to install it, then retry."
    exit 1
}

& $Vpy $AmCodexMeeting @RestArgs
exit $LASTEXITCODE

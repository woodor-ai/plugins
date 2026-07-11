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
# ~/.agent-meeting/bin/mycodex-impl.ps1 by both install-codex.py (root installer,
# unconditional — makes `--update` work even before agent-meeting is installed)
# and session-bootstrap.py (agent-meeting's own SessionStart hook — self-heals
# this file if bin/ is ever wiped and rebuilt). Fully self-locating: no absolute
# path is baked in, so the file is byte-identical everywhere it is copied.
#
# Named mycodex-impl.ps1 (not mycodex.ps1) deliberately: PowerShell resolves a
# bare `mycodex` to a same-named .ps1 before the .cmd sibling, and a .ps1 with
# the command's own name is blocked by the default Restricted execution policy
# in a real user shell. mycodex.cmd is the only PATH entry and invokes this
# file explicitly with -ExecutionPolicy Bypass, sidestepping that resolution
# order entirely.
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

# Terminal window title: codex's TUI has no programmable status bar (unlike
# Claude Code's), so the window/tab title is the only identity cue available.
# Set it here (not in codex-meeting.py) because on Windows the foreground
# codex process shares THIS console window -- title must be set before
# handing off. Mirrors codex-meeting.py's own name/control_url argparse
# defaults (not its mDNS discovery, which is overkill for a cosmetic title):
# --name positional, --control-url flag, else launcher.json's saved value.
# ASCII-only (no emoji) -- legacy conhost can render emoji as garbage glyphs.
function Get-MyCodexTitle {
    param([string[]]$ArgList)
    $name = $null
    $controlUrl = $null
    $i = 0
    while ($i -lt $ArgList.Count) {
        $a = $ArgList[$i]
        if ($a -eq "--port") { $i += 2; continue }
        if ($a -eq "--control-url") { if ($i + 1 -lt $ArgList.Count) { $controlUrl = $ArgList[$i + 1] }; $i += 2; continue }
        if ($a -eq "--no-codex") { $i += 1; continue }
        if ($a -notlike "-*" -and -not $name) { $name = $a }
        $i += 1
    }
    if (-not $name) {
        $h = ($env:COMPUTERNAME -replace '[^A-Za-z0-9-]', '-').Trim('-')
        if (-not $h) { $h = "host" }
        $name = ("codex-$h").Substring(0, [Math]::Min(20, ("codex-$h").Length))
    }
    if (-not $controlUrl) {
        $launcherJson = Join-Path $MeetingHome "codex\launcher.json"
        if (Test-Path $launcherJson) {
            try { $controlUrl = (Get-Content $launcherJson -Raw | ConvertFrom-Json).control_url } catch {}
        }
    }
    $hostport = ""
    if ($controlUrl) {
        try {
            $u = [Uri]$controlUrl
            if ($u.Host -and $u.Port -gt 0) { $hostport = "$($u.Host):$($u.Port)" }
        } catch {}
    }
    if ($hostport) { return "[meeting] $name | $hostport" }
    return "[meeting] $name"
}

$OrigTitle = $Host.UI.RawUI.WindowTitle
try {
    $Host.UI.RawUI.WindowTitle = Get-MyCodexTitle -ArgList $RestArgs
    & $Vpy $AmCodexMeeting @RestArgs
    $ExitCode = $LASTEXITCODE
} finally {
    $Host.UI.RawUI.WindowTitle = $OrigTitle
}
exit $ExitCode

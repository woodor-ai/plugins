# mycodex: bridge a Codex session into agent-meeting.
#
#   am-update                             update agent-meeting and installed
#                                         Claude Code/Codex integrations.
#   mycodex [<name>] [--control-url URL] [--proj X] [--global] [--no-codex]
#                                         start a brokered Codex
#                                         session — needs agent-meeting installed
#                                         (run `am-update` first).
#
# Single source of truth, copied verbatim (no per-install templating) into
# ~/.agent-meeting/bin/mycodex-impl.ps1 by both install-codex.py (root installer,
# unconditional — makes the launcher available after installation)
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
$SourceStamp = Join-Path $MeetingHome ".bin-plugin-root"
$PluginBin = if (Test-Path $SourceStamp) { (Get-Content $SourceStamp -TotalCount 1).Trim() } else { "" }
$PluginRoot = if ($PluginBin) { Split-Path -Parent $PluginBin } else { "" }
$AmCodexMeeting = if ($PluginRoot) { Join-Path $PluginRoot "codex\codex-meeting.py" } else { "" }
$Vpy = Join-Path $MeetingHome "venv\Scripts\python.exe"

if ($RestArgs.Count -gt 0 -and $RestArgs[0] -eq "--update") {
    Write-Error "mycodex --update has moved to am-update. Run: am-update"
    exit 2
}

if (-not $PluginBin -or -not (Test-Path $Vpy) -or -not (Test-Path $AmCodexMeeting)) {
    Write-Error "mycodex: agent-meeting is not installed - run 'am-update' to install it, then retry."
    exit 1
}

# Terminal window title: codex's TUI has no programmable status bar (unlike
# Claude Code's), so the window/tab title is the only identity cue available.
# codex-meeting.py owns this end-to-end (a background thread in that same
# shared console process periodically calls SetConsoleTitleW) -- no title
# logic needed here.
& $Vpy $AmCodexMeeting @RestArgs
exit $LASTEXITCODE

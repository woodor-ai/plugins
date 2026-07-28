$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
$PythonArguments = @("-3")
if (-not $PythonLauncher) {
    $PythonLauncher = Get-Command python -ErrorAction Stop
    $PythonArguments = @()
}

function Invoke-RepositoryPython {
    param([Parameter(Mandatory = $true)][string]$ScriptPath)
    & $PythonLauncher.Source @PythonArguments $ScriptPath
    if ($LASTEXITCODE -ne 0) {
        throw "Python installer failed ($LASTEXITCODE): $ScriptPath"
    }
}

$MeetingHome = if ($env:MEETING_HOME) {
    $env:MEETING_HOME
} else {
    Join-Path ([Environment]::GetFolderPath("UserProfile")) ".agent-meeting"
}
Invoke-RepositoryPython (
    Join-Path $RepoRoot "installers\shared\install-agent-meeting-package.py"
)
Invoke-RepositoryPython (
    Join-Path $RepoRoot "installers\shared\migrate-agent-meeting-legacy-layout.py"
)
& (Join-Path $MeetingHome "bin\am-configure-codex-user-environment.exe") @args
if ($LASTEXITCODE -ne 0) {
    throw "Codex environment configuration failed ($LASTEXITCODE)"
}
Invoke-RepositoryPython (
    Join-Path $RepoRoot "installers\shared\register-codex-marketplace.py"
)

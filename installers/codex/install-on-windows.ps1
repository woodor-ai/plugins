$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
$PythonArguments = @("-3")
if (-not $PythonLauncher) {
    $PythonLauncher = Get-Command python -ErrorAction Stop
    $PythonArguments = @()
}

function Invoke-RepositoryPython {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [string[]]$ScriptArguments = @()
    )
    & $PythonLauncher.Source @PythonArguments $ScriptPath @ScriptArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python installer failed ($LASTEXITCODE): $ScriptPath"
    }
}

$InstallArguments = @("--configure-codex") + $args
Invoke-RepositoryPython `
    (Join-Path $RepoRoot "installers\shared\install-agent-meeting-package.py") `
    $InstallArguments
Invoke-RepositoryPython (
    Join-Path $RepoRoot "installers\shared\migrate-agent-meeting-legacy-layout.py"
)
Invoke-RepositoryPython (
    Join-Path $RepoRoot "installers\shared\register-codex-marketplace.py"
)

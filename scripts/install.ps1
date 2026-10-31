$ErrorActionPreference = "Stop"

Write-Host "Installing Daemon..." -ForegroundColor Cyan

# Always install from the repository containing this script, even when the
# script is launched by absolute path from another working directory.
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = $null
$pythonArgs = @()

foreach ($candidate in @("python", "py")) {
    try {
        & $candidate --version *> $null
        if ($LASTEXITCODE -eq 0) {
            $python = $candidate
            if ($candidate -eq "py") {
                $pythonArgs = @("-3")
            }
            break
        }
    } catch {
        continue
    }
}

if ($null -eq $python) {
    throw "Python 3.11+ was not found. Install it from https://www.python.org/downloads/windows/ or run: winget install Python.Python.3.12"
}

if (!(Test-Path ".venv")) {
    & $python @pythonArgs -m venv .venv
}

$venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e ".[keyring,crypto,repl]"

Write-Host ""
Write-Host "Daemon is installed." -ForegroundColor Green
Write-Host "Run it with:"
Write-Host "  .\.venv\Scripts\daemon.exe auth add"
Write-Host "  .\.venv\Scripts\daemon.exe"
Write-Host ""
Write-Host "If Ollama is installed, use:"
Write-Host "  .\.venv\Scripts\daemon.exe auth add --provider ollama"
Write-Host "  .\.venv\Scripts\daemon.exe models list"

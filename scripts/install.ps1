$ErrorActionPreference = "Stop"

Write-Host "Installing Daemon..." -ForegroundColor Cyan

# Always install the package found next to this script, even when the script
# is launched by absolute path from another working directory.
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

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw "LOCALAPPDATA is not set. Start a normal Windows PowerShell session and retry."
}

$installRoot = Join-Path $env:LOCALAPPDATA "Programs\Daemon"
$venvPython = Join-Path $installRoot "Scripts\python.exe"
if (!(Test-Path $venvPython)) {
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
    & $python @pythonArgs -m venv $installRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the Daemon Python environment."
    }
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Could not upgrade pip in the Daemon environment."
}
& $venvPython -m pip install ".[keyring,crypto,repl]"
if ($LASTEXITCODE -ne 0) {
    throw "Daemon installation failed. See the pip error above."
}

$scriptsPath = Join-Path $installRoot "Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @($userPath -split ";" | Where-Object { $_ })
if ($pathEntries -notcontains $scriptsPath) {
    $updatedPath = (@($pathEntries) + $scriptsPath) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
}

Write-Host ""
Write-Host "Daemon is installed." -ForegroundColor Green
Write-Host "Installed for your Windows user at:"
Write-Host "  $installRoot"
Write-Host "Open a NEW PowerShell window, change to any project folder, and run:"
Write-Host "  daemon launch"
Write-Host ""
Write-Host "To enter an Ollama Cloud API key securely:"
Write-Host "  daemon auth add --provider ollama-cloud"

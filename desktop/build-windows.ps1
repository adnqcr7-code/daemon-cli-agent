$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if (-not (Get-Command rustc -ErrorAction SilentlyContinue)) {
  Write-Host 'Install Rust from https://rustup.rs and reopen PowerShell.' -ForegroundColor Yellow
  exit 1
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  Write-Host 'Install Node.js LTS and reopen PowerShell.' -ForegroundColor Yellow
  exit 1
}
npm install
npm run build:windows
Write-Host "Build complete. Look in src-tauri\target\release\bundle\nsis\" -ForegroundColor Green

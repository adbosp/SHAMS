# SHAMS Desktop — development launch (Windows PowerShell).
#   powershell -ExecutionPolicy Bypass -File scripts\desktop-dev.ps1 [-Workspace C:\AI\SHAMS_Projects] [-BrowserOnly]
# Needs: Python venv with SHAMS (.venv), Node 20+, and for the desktop window Rust (rustup) + WebView2.
param(
  [string]$Workspace = $(if ($env:SHAMS_WORKSPACE) { $env:SHAMS_WORKSPACE } elseif (Test-Path "C:\AI\SHAMS_Projects") { "C:\AI\SHAMS_Projects" } else { "$HOME\SHAMS" }),
  [switch]$BrowserOnly
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$Desk = Join-Path $Root "apps\desktop"
Push-Location $Desk
try {
  if (-not (Test-Path "node_modules")) { npm ci }
  $env:SHAMS_DIR = $Root; $env:SHAMS_PYTHON = $Py; $env:SHAMS_WORKSPACE = $Workspace
  if ($BrowserOnly -or -not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    if (-not $BrowserOnly) { Write-Warning "Rust/cargo not found: starting the browser version (backend + Vite) instead of the desktop window." }
    $backend = Start-Process -FilePath $Py -ArgumentList @("-m", "apps.cli.main", "desktop-backend", $Workspace, "--port", "8765") -WorkingDirectory $Root -PassThru -NoNewWindow
    try {
      $env:VITE_SHAMS_API = "http://127.0.0.1:8765"
      Start-Process "http://127.0.0.1:1420/"
      npm run dev
    } finally { if ($backend -and -not $backend.HasExited) { Stop-Process -Id $backend.Id } }   # only the backend this script started
  } else {
    npm run tauri:dev
  }
} finally { Pop-Location }

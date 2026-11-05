# SHAMS Desktop — Windows installer build (NSIS .exe + MSI). Model weights are never bundled.
#   powershell -ExecutionPolicy Bypass -File scripts\desktop-build.ps1
# Output: apps\desktop\src-tauri\target\release\bundle\{nsis,msi}\
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
foreach ($tool in "node", "npm", "cargo") {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool not found. Install Node 20+ and Rust (https://rustup.rs, MSVC toolchain)." }
}
Push-Location (Join-Path $Root "apps\desktop")
try {
  npm ci
  npm run tauri:build
  Get-ChildItem "src-tauri\target\release\bundle" -Recurse -Include *.exe, *.msi | ForEach-Object { Write-Host "built: $($_.FullName)" }
} finally { Pop-Location }

#!/usr/bin/env bash
# SHAMS Desktop — development launch (Linux/macOS). ./scripts/desktop-dev.sh [workspace] [--browser]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="${1:-${SHAMS_WORKSPACE:-$HOME/SHAMS}}"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
cd "$ROOT/apps/desktop"; [ -d node_modules ] || npm ci
export SHAMS_DIR="$ROOT" SHAMS_PYTHON="$PY" SHAMS_WORKSPACE="$WS"
if [ "${2:-}" = "--browser" ] || ! command -v cargo >/dev/null; then
  (cd "$ROOT" && "$PY" -m apps.cli.main desktop-backend "$WS" --port 8765) & BACK=$!
  trap 'kill $BACK' EXIT
  VITE_SHAMS_API=http://127.0.0.1:8765 npm run dev
else
  npm run tauri:dev
fi

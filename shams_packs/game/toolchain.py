"""Web game test toolchain: Vitest + Three.js installed once in a SHAMS folder, never into a user project.

Location: ``$SHAMS_TOOLCHAINS/web`` or ``~/.shams/toolchains/web``. ``shams toolchain install web`` runs npm
there with pinned versions. Test runs happen in a temporary folder inside it, so Node resolves ``three`` and
``vitest`` from the toolchain's node_modules without links or a per-project install.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PACKAGES = {"vitest": "3.2.4", "three": "0.178.0", "@types/three": "0.178.1"}


def toolchains_root() -> Path:
    return Path(os.environ.get("SHAMS_TOOLCHAINS") or Path.home() / ".shams" / "toolchains")


def web_dir() -> Path:
    return toolchains_root() / "web"


def vitest_entry(root: Path | None = None) -> Path | None:
    p = (root or web_dir()) / "node_modules" / "vitest" / "vitest.mjs"
    return p if p.exists() else None


def install_web(root: Path | None = None, npm: str | None = None) -> dict:
    """Install the pinned packages. Returns {"ok", "dir", "log"}."""
    d = root or web_dir()
    d.mkdir(parents=True, exist_ok=True)
    pkg = d / "package.json"
    if not pkg.exists():
        pkg.write_text('{"name": "shams-web-toolchain", "private": true, "type": "module"}\n', encoding="utf-8")
    npm = npm or shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return {"ok": False, "dir": str(d), "log": "npm not found on PATH (install Node.js 22+)"}
    argv = [npm, "install", "--no-audit", "--no-fund", "--save-exact", *[f"{k}@{v}" for k, v in PACKAGES.items()]]
    p = subprocess.run(argv, cwd=d, capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace",
                       shell=os.name == "nt" and npm.lower().endswith(".cmd"))
    return {"ok": p.returncode == 0 and vitest_entry(d) is not None, "dir": str(d),
            "log": (p.stdout + p.stderr).strip()[-3000:]}


def status() -> dict:
    d = web_dir()
    return {"dir": str(d), "installed": vitest_entry(d) is not None,
            "versions": {k: _version(d, k) for k in PACKAGES}}


def _version(d: Path, name: str) -> str | None:
    import json
    p = d / "node_modules" / name / "package.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None

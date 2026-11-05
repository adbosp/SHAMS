"""Game Pack QC checkers (ADR-115).

``compile``: Web/Three.js code (TypeScript or JavaScript) gets a real syntax check with Node.js (types
stripped by ``node:module``, then ``node --check``). That is a parse check, not a full type check: the
project's ``npm run typecheck`` needs installed packages and runs with the engine adapter. Unity, Unreal and
Godot code needs the engine itself, which is not wired yet, so it stays NOT_VERIFIED instead of a fake PASS.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "js" / "syntax_check.mjs"
WEB = {"text/typescript": ".ts", "application/typescript": ".ts", "text/javascript": ".js",
       "application/javascript": ".js"}
ENGINE_CODE = {"text/x-csharp": "Unity (C#)", "text/x-c++": "Unreal (C++)", "text/x-gdscript": "Godot (GDScript)"}


def node_version(node: str) -> tuple[int, int]:
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip().lstrip("v")
    major, minor, *_ = (out.split(".") + ["0", "0"])
    return int(major), int(minor)


def compile_check(kind: str, criterion: dict, contents: list[str], outputs: list[dict] | None = None):
    outs = [o for o in outputs or [] if o.get("role", "output") == "output"]
    web = [o for o in outs if o.get("media_type") in WEB]
    other = sorted({ENGINE_CODE.get(o.get("media_type"), o.get("media_type")) for o in outs
                    if o.get("media_type") in ENGINE_CODE})
    if not web:
        if other:
            return "NOT_VERIFIED", f"{', '.join(other)} code needs the engine to compile; engine runner not wired yet"
        return "FAIL", "missing output: no code file to compile"
    node = shutil.which("node")
    if not node:
        return "NOT_VERIFIED", "Node.js not found on PATH: cannot check the web code"
    if node_version(node) < (22, 13):
        return "NOT_VERIFIED", "Node.js 22.13 or newer is needed to check TypeScript"
    with tempfile.TemporaryDirectory() as td:
        files = []
        for o in web:
            name = Path(o.get("path") or o["logical_id"].replace(".", "_")).name
            if not name.endswith(WEB[o["media_type"]]):
                name += WEB[o["media_type"]]
            p = Path(td) / name
            shutil.copyfile(o["file"], p)
            files.append(str(p))
        p = subprocess.run([node, "--no-warnings", str(SCRIPT), *files], capture_output=True, text=True,
                           timeout=120, encoding="utf-8", errors="replace")
    if p.returncode == 0:
        return "PASS", f"{len(files)} web code file(s) parse (syntax check, not a full type check)"
    return "FAIL", "compile error: " + (p.stdout + p.stderr).strip()[-1500:]


compile_check.wants_outputs = True

"""Game Pack QC checkers (ADR-115).

``compile``: Web/Three.js code (TypeScript or JavaScript) gets a real syntax check with Node.js (types
stripped by ``node:module``, then ``node --check``). That is a parse check, not a full type check: the
project's ``npm run typecheck`` needs installed packages and runs with the engine adapter. Unity, Unreal and
Godot code needs the engine itself, which is not wired yet, so it stays NOT_VERIFIED instead of a fake PASS.
"""
from __future__ import annotations

import os
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


# --------------------------------------------------------------------------- unit tests (Web: Vitest)
VITEST_CONFIG = """export default {
  test: {
    include: ["**/*.{test,spec}.{ts,js,mjs}", "**/*Test.{ts,js}", "**/*Tests.{ts,js}"],
    exclude: ["**/node_modules/**"],
    environment: "node",
    globals: true,
    watch: false,
    testTimeout: 20000,
  },
};
"""
TEST_NAME = ("Test.", "Tests.", ".test.", ".spec.")


def _is_test(name: str) -> bool:
    return any(t in Path(name).name for t in TEST_NAME)


def unit_test_check(kind: str, criterion: dict, contents: list[str], outputs: list[dict] | None = None):
    """Runs the task's Vitest tests (Web/Three.js) against its code and the upstream code it imports.
    Other engines need their own test runner: NOT_VERIFIED."""
    import tempfile as _tf

    from shams_packs.game import toolchain
    all_files = [o for o in outputs or [] if o.get("media_type") in WEB]
    outs = [o for o in all_files if o.get("role", "output") == "output"]
    if not outs:
        engine = sorted({ENGINE_CODE[o["media_type"]] for o in outputs or [] if o.get("media_type") in ENGINE_CODE})
        if engine:
            return "NOT_VERIFIED", f"{', '.join(engine)} tests need the engine test runner; not wired yet"
        return "FAIL", "missing output: no code to test"
    filt = (criterion.get("check") or {}).get("test_filter")
    tests = [o for o in outs if _is_test(o.get("path") or o["logical_id"]) or o["logical_id"].startswith("test.")]
    if filt:
        tests = [o for o in tests if filt in (o.get("path") or "")] or tests
    if not tests:
        return "FAIL", "tests failed: no test file among the outputs (expected a *Tests.ts / *.test.ts file)"
    node = shutil.which("node")
    entry = toolchain.vitest_entry()
    if not node or entry is None:
        return "NOT_VERIFIED", ("web test toolchain not installed: run `shams toolchain install web`" if node
                                else "Node.js not found on PATH: cannot run the web tests")
    work_root = toolchain.web_dir() / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    td = Path(_tf.mkdtemp(dir=work_root))
    try:
        test_paths = []
        for o in sorted(all_files, key=lambda o: o.get("role", "output") == "output"):    # outputs win
            rel = Path(o.get("path") or (o["logical_id"].replace(".", "_") + WEB[o["media_type"]]))
            if rel.is_absolute() or ".." in rel.parts:
                rel = Path(rel.name)
            if not rel.suffix:
                rel = rel.with_suffix(WEB[o["media_type"]])
            dest = td / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(o["file"], dest)
            if o in tests:
                if not _is_test(dest.name):                  # make sure Vitest picks it up
                    new = dest.with_name(dest.stem + ".test" + dest.suffix)
                    dest.rename(new)
                    dest = new
                test_paths.append(dest.relative_to(td).as_posix())
        (td / "vitest.config.mjs").write_text(VITEST_CONFIG, encoding="utf-8")
        try:
            p = subprocess.run([node, str(entry), "run", "--root", str(td), "--config", str(td / "vitest.config.mjs"),
                                *test_paths], cwd=td, capture_output=True, text=True, timeout=300,
                               encoding="utf-8", errors="replace", env={**os.environ, "CI": "1", "NO_COLOR": "1"})
        except subprocess.TimeoutExpired:
            return "FAIL", "tests failed: Vitest timed out after 300 s"
    finally:
        shutil.rmtree(td, ignore_errors=True)
    log = (p.stdout + "\n" + p.stderr).strip()
    tail = "\n".join(log.splitlines()[-40:])
    if p.returncode == 0:
        summary = next((l.strip() for l in log.splitlines() if l.strip().startswith("Tests")), "tests pass")
        return "PASS", f"Vitest: {summary}"
    return "FAIL", "tests failed: " + tail[-2000:]


unit_test_check.wants_outputs = True

"""Deterministic and model-based checkers selected by ``criterion.check.type`` (ADR-120).

    {"kind": "schema",     "check": {"type": "glb", "require_uv": true, "max_size_m": 5}}
    {"kind": "screenshot", "check": {"type": "image", "min_width": 256}}
    {"kind": "metric",     "check": {"type": "scene_report", "min_objects": 2, "require_camera": true}}
    {"kind": "runtime_log","check": {"type": "<type an extension registered>"}}
    {"kind": "compile",    "check": {"type": "python"}}
    {"kind": "unit_test",  "check": {"type": "pytest"}}
    {"kind": "visual",     "check": {"type": "vlm", "min_score": 6}}

A criterion without ``check.type`` goes to the checker the Domain Pack or a plugin registered for its
kind; with neither it stays NOT_VERIFIED (ADR-010). Checkers see the produced files (path-aware), so
binary assets are inspected for real.
"""
from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from shams.verification import assets
from shams.verification.qc import STAGE


# check.type -> fn(criterion, outputs, settings) -> (verdict, detail); registered by extensions
CHECK_TYPES: dict[str, Callable] = {}


def register_check_type(name: str, fn: Callable) -> None:
    CHECK_TYPES[name] = fn


def pick(outputs: list[dict], check: dict, media: tuple[str, ...]) -> list[dict]:
    sel = [o for o in outputs if o.get("media_type") in media and o.get("role", "output") == "output"]
    if check.get("logical_id"):
        sel = [o for o in sel if o["logical_id"] == check["logical_id"]]
    return sel


class AssetCheckers:
    """One callable per criterion kind; dispatches on check.type, falls back to the previous checker."""
    wants_outputs = True

    def __init__(self, fallback: dict[str, Callable] | None = None, *, settings: dict | None = None,
                 visual: Callable[[list[str], str, dict], tuple[str, str]] | None = None,
                 python: str | None = None, timeout: float = 600):
        self.fallback = dict(fallback or {})
        self.settings = settings or {}
        self.visual, self.python, self.timeout = visual, python or sys.executable, timeout

    def table(self) -> dict[str, Callable]:
        return {kind: self._for(kind) for kind in STAGE}

    def _for(self, kind: str):
        def check(k: str, criterion: dict, contents: list[str], outputs: list[dict] | None = None):
            ctype = (criterion.get("check") or {}).get("type")
            fn = (getattr(self, f"_{ctype}", None) or self._extension(ctype)) if ctype else None
            if fn is not None and outputs is not None:
                try:
                    return fn(criterion, outputs)
                except Exception as e:  # noqa: BLE001 - a broken checker must not crash QC
                    return "NOT_VERIFIED", f"checker {ctype} crashed: {type(e).__name__}: {e}"
            fb = self.fallback.get(k)
            if fb is None:
                return "NOT_VERIFIED", f"no checker for kind '{k}'" + (f" / type '{ctype}'" if ctype else "")
            if getattr(fb, "wants_outputs", False):
                return fb(k, criterion, contents, outputs=outputs)
            return fb(k, criterion, contents)
        check.wants_outputs = True
        return check

    def _extension(self, ctype: str):
        fn = CHECK_TYPES.get(ctype)
        return (lambda c, outputs: fn(c, outputs, self.settings)) if fn else None

    # ------------------------------------------------------------------ 3D
    def _glb(self, c: dict, outputs: list[dict]):
        sel = pick(outputs, c.get("check") or {}, ("model/gltf-binary",))
        if not sel:
            return "FAIL", "missing output: no GLB produced"
        res = [(o["logical_id"], *assets.check_glb(o["file"], c.get("check"))) for o in sel]
        bad = [f"{lid}: {d}" for lid, v, d in res if v != "PASS"]
        return ("FAIL", "; ".join(bad)) if bad else ("PASS", "; ".join(f"{lid}: {d}" for lid, _, d in res))

    def _scene_report(self, c: dict, outputs: list[dict]):
        chk = c.get("check") or {}
        sel = pick(outputs, chk, ("application/json",))
        if not sel:
            return "FAIL", "missing output: no scene report"
        rep = json.loads(Path(sel[0]["file"]).read_text(encoding="utf-8"))
        errs = []
        if rep.get("mesh_objects", 0) < int(chk.get("min_objects", 1)):
            errs.append(f"{rep.get('mesh_objects', 0)} mesh objects < {chk.get('min_objects', 1)}")
        if chk.get("require_camera", False) and not rep.get("cameras") and not rep.get("camera_added"):
            errs.append("no camera")
        if chk.get("require_light", False) and not rep.get("lights"):
            errs.append("no light")
        if chk.get("require_materials", True) and not rep.get("materials"):
            errs.append("no materials")
        nm = sum(o.get("non_manifold_edges", 0) for o in rep.get("objects", []))
        if "max_non_manifold" in chk and nm > int(chk["max_non_manifold"]):
            errs.append(f"{nm} non-manifold edges > {chk['max_non_manifold']}")
        tris = sum(o.get("triangles", 0) for o in rep.get("objects", []))
        if chk.get("max_triangles") and tris > int(chk["max_triangles"]):
            errs.append(f"{tris} triangles > budget {chk['max_triangles']}")
        summary = (f"{rep.get('mesh_objects')} meshes, {tris} tris, cameras {rep.get('cameras')}, lights "
                   f"{rep.get('lights')}, materials {len(rep.get('materials', []))}, non-manifold edges {nm}")
        return ("FAIL", "scene check: " + "; ".join(errs) + f" ({summary})") if errs else ("PASS", summary)

    # ------------------------------------------------------------------ text
    def _text(self, c: dict, outputs: list[dict]):
        chk = c.get("check") or {}
        sel = pick(outputs, chk, ("text/markdown", "text/plain", "application/json", "text/x-python"))
        if not sel:
            return "FAIL", "missing output: no text produced"
        n = min(len(Path(o["file"]).read_text(encoding="utf-8", errors="replace").strip()) for o in sel)
        need = int(chk.get("min_chars", 1))
        return ("PASS", f"{n} chars") if n >= need else ("FAIL", f"text output has {n} chars < {need}")

    # ------------------------------------------------------------------ images
    def _image(self, c: dict, outputs: list[dict]):
        sel = pick(outputs, c.get("check") or {}, ("image/png",))
        if not sel:
            return "FAIL", "missing output: no PNG image"
        res = [(o["logical_id"], *assets.check_png(o["file"], c.get("check"))) for o in sel]
        bad = [f"{lid}: {d}" for lid, v, d in res if v != "PASS"]
        return ("FAIL", "render evidence: " + "; ".join(bad)) if bad else \
            ("PASS", "; ".join(f"{lid}: {d}" for lid, _, d in res))

    _render = _image

    def _vlm(self, c: dict, outputs: list[dict]):
        if self.visual is None:
            return "NOT_VERIFIED", "no visual_qc model configured"
        sel = pick(outputs, c.get("check") or {}, ("image/png", "image/jpeg"))
        if not sel:
            return "FAIL", "missing output: no image for visual QC"
        desc = c["description"]
        briefs = [o for o in outputs if o.get("role") == "input" and str(o.get("media_type", "")).startswith("text/")
                  and o.get("file")]
        for o in briefs[:2]:                          # e.g. the art brief the image was drawn from
            try:
                desc += " | Character description: " + Path(o["file"]).read_text(
                    encoding="utf-8", errors="replace").strip()[:600]
            except OSError:
                pass
        return self.visual([o["file"] for o in sel], desc, c.get("check") or {})

    # ------------------------------------------------------------------ code
    @staticmethod
    def _materialize(outputs: list[dict], td: Path) -> list[tuple[Path, str]]:
        """Python outputs (and upstream Python inputs, so imports resolve) flat in one folder."""
        out = []
        for o in sorted(outputs, key=lambda o: o.get("role", "output") == "output"):   # outputs win
            if o.get("media_type") == "text/x-python":
                p = td / Path(o.get("path") or f"{o['logical_id'].replace('.', '_')}.py").name
                shutil.copyfile(o["file"], p)
                out.append((p, o.get("role", "output")))
        return out

    def _python(self, c: dict, outputs: list[dict]):
        with tempfile.TemporaryDirectory() as td:
            files = [f for f, role in self._materialize(outputs, Path(td)) if role == "output"]
            if not files:
                return "FAIL", "missing output: no Python file"
            for f in files:
                try:
                    py_compile.compile(str(f), doraise=True)
                except py_compile.PyCompileError as e:
                    return "FAIL", f"compile error: SyntaxError in {f.name}: {e.msg.strip()[-400:]}"
        return "PASS", f"{len(files)} Python file(s) compile"

    def _pytest(self, c: dict, outputs: list[dict]):
        with tempfile.TemporaryDirectory() as td:
            tests = [str(f) for f, role in self._materialize(outputs, Path(td))
                     if role == "output" and (f.name.startswith("test_") or f.name.endswith("_test.py"))]
            if not tests:
                return "FAIL", "tests failed: no test_*.py among the outputs"
            env = {**os.environ, "PYTHONPATH": td, "PYTHONDONTWRITEBYTECODE": "1"}
            try:
                p = subprocess.run([self.python, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", *tests],
                                   cwd=td,
                                   capture_output=True, text=True, timeout=self.timeout, env=env,
                                   encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                return "FAIL", "tests failed: pytest timed out"
        tail = (p.stdout + p.stderr).strip().splitlines()[-40:]
        if p.returncode == 0:
            return "PASS", tail[-1] if tail else "pytest passed"
        if p.returncode == 5:
            return "FAIL", "tests failed: pytest collected no tests"
        return "FAIL", "tests failed: " + "\n".join(tail)[-1900:]


# --------------------------------------------------------------------------- visual QC with a VL model
VISUAL_PROMPT = """You are an independent visual QC inspector. Judge ONLY what is visible in the image(s).
Criterion: {criterion}
Reply with JSON: {{"pass": true|false, "score": 0-10, "reason": "<one sentence>"}}"""


def make_visual_checker(provider: Any, model: str, exclusive: bool = True):
    """Visual QC through a vision model (Ollama native chat with images). With ``exclusive`` the other
    Ollama models are unloaded first so the VL model fits on one GPU."""
    import base64

    from shams.workers import extract_json

    def image_b64(p: str) -> str:
        return base64.b64encode(Path(p).read_bytes()).decode("ascii")

    def visual(paths: list[str], criterion: str, check: dict) -> tuple[str, str]:
        if exclusive and hasattr(provider, "ps"):
            for m in provider.ps():
                if m["name"] != model:
                    provider.unload(m["name"])
        msgs = [{"role": "user", "content": VISUAL_PROMPT.format(criterion=criterion)}]
        imgs = [image_b64(p) for p in paths[:4]]
        r = provider.chat(msgs, model=model, images=imgs, format="json")
        if not r.get("text", "").strip():
            # some vision models answer only in their reasoning channel when thinking is forced off
            r = provider.chat(msgs, model=model, images=imgs, format="json", think=True)
        try:
            v = extract_json(r["text"])
        except ValueError:
            return "NOT_VERIFIED", f"visual model reply not JSON: {r['text'][:200]}"
        score = float(v.get("score", 10 if v.get("pass") else 0))
        ok = bool(v.get("pass")) and score >= float(check.get("min_score", 6))
        detail = f"{model}: score {score:g}/10, {v.get('reason', '')}"[:500]
        return ("PASS", detail) if ok else ("FAIL", "visual check failed: " + detail)
    return visual

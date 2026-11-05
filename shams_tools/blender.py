"""Blender integration (ADR-120): the 3D Scene worker and the GLB import check.

The 3D Scene worker is a *coding* model writing Blender Python: procedural geometry, modifiers,
materials, lights and a camera. Blender runs the script headless (``blender_harness.py``), renders
evidence and exports GLB; errors go back to the model for a bounded number of fixes. It does not
generate neural meshes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from shams import providers
from shams.verification import assets
from shams.verification.checkers import pick
from shams.workers import base_handoff, output_plan
from shams_tools.routing import choose_3d_route

HARNESS = Path(__file__).resolve().parent / "blender_harness.py"


def find_blender(configured: str | None = None) -> str | None:
    """Configured path -> PATH -> newest install in the usual places (Windows / macOS / Linux)."""
    if configured:
        return configured if Path(configured).exists() else None
    w = shutil.which("blender")
    if w:
        return w
    if sys.platform == "win32":
        roots = [Path(os.environ[e]) / "Blender Foundation" for e in ("ProgramFiles", "ProgramFiles(x86)")
                 if os.environ.get(e)]
        cands = [p / "blender.exe" for r in roots if r.exists() for p in r.iterdir() if p.is_dir()]
    elif sys.platform == "darwin":
        cands = [Path("/Applications/Blender.app/Contents/MacOS/Blender")]
    else:
        cands = [Path("/usr/bin/blender"), Path("/snap/bin/blender")]
    found = [c for c in cands if c.exists()]

    def ver(p: Path) -> tuple:
        return tuple(int(x) for x in re.findall(r"\d+", p.parent.name)) or (0,)
    return str(sorted(found, key=ver)[-1]) if found else None


def blender_from(settings: dict) -> str | None:
    return find_blender(((settings.get("tools") or {}).get("blender") or {}).get("path"))


SCENE_PROMPT = """You are the 3D Scene / Procedural specialist of SHAMS. You write Blender Python (bpy) that BUILDS a scene.
Rules:
- Start from an empty scene. Units are metres, Z is up. Real-world scale (a chair is ~0.9 m tall).
- Allowed imports: bpy, bmesh, math, mathutils, random. No file, network, os, sys or subprocess access.
- Do NOT render, save or export: SHAMS renders evidence and exports GLB itself.
- Give every object a clear name. Use Principled BSDF materials with sensible base colours.
- Prefer primitives, modifiers (bevel, array, mirror, solidify) and bmesh over huge vertex lists. Stay within the polygon budget.
- Add a camera that frames the subject and at least one light.
Reply with exactly one ```python fenced block containing the whole script, then one line starting with SUMMARY: ."""

FORBIDDEN = re.compile(r"(?m)^\s*(?:import|from)\s+(os|sys|subprocess|shutil|socket|urllib|requests|pathlib|ctypes)\b|"
                       r"\b(__import__|exec|eval|open)\s*\(|bpy\.ops\.wm\.(save|quit|open)|bpy\.ops\.render\.|"
                       r"bpy\.ops\.export_scene\.")


def extract_code(text: str) -> str | None:
    m = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m:
        return max(m, key=len).strip()
    return text.strip() if "import bpy" in text else None


def run_blender(blender: str, spec: dict, workdir: Path, timeout: float) -> dict:
    sp = workdir / "blender_spec.json"
    spec = {**spec, "report": str(workdir / "blender_report.json")}
    sp.write_text(json.dumps(spec), encoding="utf-8")
    t0 = time.monotonic()
    try:
        p = subprocess.run([blender, "-b", "--factory-startup", "-noaudio", "--python-exit-code", "3",
                            "-P", str(HARNESS), "--", str(sp)], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Blender timed out after {timeout:.0f}s", "seconds": timeout}
    rp = Path(spec["report"])
    rep = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else \
        {"ok": False, "error": f"Blender exited {p.returncode} without a report: {(p.stderr or p.stdout)[-1500:]}"}
    rep["seconds"] = round(time.monotonic() - t0, 2)
    return rep


class Scene3DWorker:
    """LLM (a coding model) writes Blender Python; Blender runs it headless; errors go back to the model
    for up to ``max_fix_rounds`` fixes inside one attempt. Outputs by media type: model/gltf-binary (GLB),
    image/png (render evidence), text/x-python (script), application/json (scene report)."""

    def __init__(self, *, entry: dict, attempts_root: Path, provider: Any = None,
                 read_blob: Callable[[str], Path] | None = None, **_: Any):
        cfg = entry.get("provider_config", {})
        self.entry, self.provider, self.read_blob = entry, provider, read_blob
        self.model = entry["worker"].get("model") or ""
        self.attempts_root = Path(attempts_root)
        self.blender = find_blender(cfg.get("blender"))
        self.max_fix_rounds = int(cfg.get("max_fix_rounds", 2))
        self.timeout = float(cfg.get("blender_timeout", 300))
        self.resolution = int(cfg.get("render_resolution", 512))
        self.engine = cfg.get("render_engine", "BLENDER_WORKBENCH")
        self.chat_options = dict(cfg.get("chat_options", {}))
        self.log: list[dict] = []

    def _messages(self, ctx: dict, route: str) -> list[dict]:
        task = ctx["task"]
        payload = {"goal": ctx.get("goal", ""), "task": task["title"], "description": task.get("description", ""),
                   "route": route, "acceptance_criteria": [c["description"] for c in task["acceptance_criteria"]],
                   "polygon_budget": next((c.get("check", {}).get("max_triangles") for c in task["acceptance_criteria"]
                                           if c.get("check", {}).get("max_triangles")), 50000),
                   "upstream_summaries": ctx.get("upstream_summaries", []),
                   "prior_failures": ctx.get("prior_failures", [])}
        return [{"role": "system", "content": SCENE_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)}]

    def generate_script(self, messages: list[dict]) -> tuple[str | None, str, dict]:
        r = providers.timed_chat(self.provider, messages, self.model, **self.chat_options)
        return extract_code(r["text"]), r["text"], r

    def build(self, ctx: dict, workdir: Path, want: dict[str, str | None], route: str) -> tuple[dict, str, dict]:
        """Generate -> static check -> run in Blender -> fix loop. Returns (report, script, usage)."""
        messages = self._messages(ctx, route)
        usage = {"tokens_in": 0, "tokens_out": 0, "wall_seconds": 0.0}
        script, rep = "", {"ok": False, "error": "no attempt"}
        for round_ in range(self.max_fix_rounds + 1):
            code, raw, r = self.generate_script(messages)
            usage["tokens_in"] += r.get("tokens_in", 0)
            usage["tokens_out"] += r.get("tokens_out", 0)
            usage["wall_seconds"] += r.get("wall_seconds", 0.0)
            if not code:
                problem = "Reply had no ```python block."
                rep = {"ok": False, "error": problem}
            elif FORBIDDEN.search(code):
                problem = f"Forbidden construct: {FORBIDDEN.search(code).group(0)!r}. Only bpy/bmesh/math/mathutils/random."
                rep = {"ok": False, "error": problem}
                script = code
            else:
                script = code
                sp = workdir / "scene_script.py"
                sp.write_text(code, encoding="utf-8")
                rep = run_blender(self.blender, {"script": str(sp), "glb": want.get("glb"), "render": want.get("render"),
                                                 "resolution": self.resolution, "engine": self.engine},
                                  workdir, self.timeout)
                usage["wall_seconds"] += rep.get("seconds", 0)
                if rep.get("ok") and rep.get("mesh_objects", 0) == 0:
                    rep = {**rep, "ok": False, "error": "The script ran but created no mesh objects."}
                if rep.get("ok"):
                    self.log.append({"round": round_, "result": "ok"})
                    return rep, script, usage
                problem = rep.get("error", "unknown Blender error")
            self.log.append({"round": round_, "result": problem[-300:]})
            messages = messages + [{"role": "assistant", "content": raw},
                                   {"role": "user", "content": "Blender reported this problem; send the full fixed "
                                                               f"script.\n{problem[-2500:]}"}]
        return rep, script, usage

    def run(self, ctx: dict, worker_ref: dict) -> tuple[list[dict], dict]:
        adir, plan = output_plan(ctx, self.attempts_root)
        adir.mkdir(parents=True, exist_ok=True)
        if not self.blender:
            return [], base_handoff(ctx, worker_ref, "BLOCKED", "Blender not found",
                                    blocked_reason="tool unavailable: Blender program not found; set tools.blender.path")
        route, why = choose_3d_route(ctx["task"], ctx.get("inputs", []), sf3d_available=False)
        if route == "reuse":
            route = "procedural"          # adapting upstream meshes is a later step; build procedurally
        by_mt = {o["media_type"]: o for o in plan}
        work = adir / "_blender"
        work.mkdir(exist_ok=True)
        want = {"glb": str(work / "scene.glb") if "model/gltf-binary" in by_mt else None,
                "render": str(work / "render.png")}
        rep, script, usage = self.build(ctx, work, want, route)
        decisions = [{"decision": f"3D route: {route}", "reason": why},
                     {"decision": "procedural geometry via Blender Python", "reason": "coding model, not a mesh generator"}]
        decisions += [{"decision": f"fix round {e['round']}", "reason": e["result"]} for e in self.log if e["result"] != "ok"]
        if not rep.get("ok"):
            return [], base_handoff(ctx, worker_ref, "FAILED", "Blender scene failed", usage=usage, decisions=decisions,
                                    known_issues=[{"issue": rep.get("error", "")[-800:], "severity": "high"}],
                                    blocked_reason=rep.get("error", "")[-500:])
        files = []
        produced = {"model/gltf-binary": want["glb"], "image/png": want["render"]}
        for o in plan:
            dst = Path(o["abs"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            mt = o["media_type"]
            if mt in produced and produced[mt] and Path(produced[mt]).exists():
                shutil.copyfile(produced[mt], dst)
            elif mt in ("text/x-python", "text/plain"):
                dst.write_text(script, encoding="utf-8")
            elif mt == "application/json":
                dst.write_text(json.dumps(rep, indent=1), encoding="utf-8")
            else:
                continue
            files.append(o)
        summary = (f"Built {rep.get('mesh_objects')} mesh objects with Blender {rep.get('blender')} "
                   f"({route}); materials: {', '.join(rep.get('materials', [])[:6]) or 'none'}")
        return files, base_handoff(ctx, worker_ref, "READY_FOR_QC", summary, usage=usage, decisions=decisions)




def check_blender_import(c: dict, outputs: list[dict], settings: dict) -> tuple[str, str]:
    """check.type = "blender_import": the GLB imports into Blender and renders a non-blank image."""
    blender = blender_from(settings)
    if not blender:
        return "NOT_VERIFIED", "Blender not found: import check skipped"
    sel = pick(outputs, c.get("check") or {}, ("model/gltf-binary",))
    if not sel:
        return "FAIL", "missing output: no GLB to import"
    with tempfile.TemporaryDirectory() as td:
        glb = Path(td) / "asset.glb"
        shutil.copyfile(sel[0]["file"], glb)
        rep = run_blender(blender, {"import_glb": str(glb), "render": str(Path(td) / "import.png"),
                                    "resolution": 256}, Path(td), 600)
        if not rep.get("ok"):
            return "FAIL", "GLB invalid: Blender import failed: " + rep.get("error", "")[-600:]
        img_v, img_d = assets.check_png(Path(td) / "import.png")
    if rep.get("mesh_objects", 0) == 0:
        return "FAIL", "GLB invalid: imported scene has no mesh"
    if img_v != "PASS":
        return "FAIL", f"render evidence after import: {img_d}"
    return "PASS", f"Blender {rep.get('blender')} imported {rep.get('mesh_objects')} meshes; render {img_d}"

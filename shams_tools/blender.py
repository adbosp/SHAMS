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
from shams_tools import scene_json
from shams_tools.routing import choose_3d_route, scene_builder

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
- This is Python for Blender (bpy), NOT JavaScript/three.js: no `new`, `const`, `let`, `THREE.`, semicolons or braces.
Example of the expected style (a different subject):
```python
import bpy

def material(name, rgba):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = rgba
    return m

bpy.ops.mesh.primitive_plane_add(size=6, location=(0, 0, 0))
floor = bpy.context.active_object
floor.name = "Floor"
floor.data.materials.append(material("Concrete", (0.5, 0.5, 0.5, 1)))

bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.25))
box = bpy.context.active_object
box.name = "Crate"
box.scale = (0.5, 0.5, 0.5)
box.modifiers.new("Bevel", "BEVEL").width = 0.02
box.data.materials.append(material("Wood", (0.45, 0.28, 0.12, 1)))

bpy.ops.object.light_add(type="SUN", location=(3, -3, 5))
bpy.context.active_object.data.energy = 3
bpy.ops.object.camera_add(location=(2.5, -2.5, 1.8))
cam = bpy.context.active_object
cam.rotation_euler = (1.15, 0, 0.785)
bpy.context.scene.camera = cam
```
Reply with exactly one ```python fenced block containing the whole script, then one line starting with SUMMARY: ."""

NOT_PYTHON = re.compile(r"(?m)\bTHREE\.|^\s*(const|let|var)\s+\w+\s*=|=\s*new\s+[A-Z]|;\s*$|^\s*function\s+\w+\s*\(|"
                        r"\bconsole\.log\b|=>")

FORBIDDEN = re.compile(r"(?m)^\s*(?:import|from)\s+(os|sys|subprocess|shutil|socket|urllib|requests|pathlib|ctypes)\b|"
                       r"\b(__import__|exec|eval|open)\s*\(|bpy\.ops\.wm\.(save|quit|open)|bpy\.ops\.render\.|"
                       r"bpy\.ops\.export_scene\.")


def _syntax_error(code: str) -> str | None:
    try:
        compile(code, "scene_script.py", "exec")
        return None
    except SyntaxError as e:
        return f"SyntaxError line {e.lineno}: {e.msg}: {(e.text or '').strip()[:120]}"


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
        self.blender_model = cfg.get("blender_model") or self.model      # complex scenes may use a stronger coder
        self.attempts_root = Path(attempts_root)
        self.blender = find_blender(cfg.get("blender"))
        self.max_fix_rounds = int(cfg.get("max_fix_rounds", 3))
        self.timeout = float(cfg.get("blender_timeout", 300))
        self.resolution = int(cfg.get("render_resolution", 512))
        self.engine = cfg.get("render_engine", "BLENDER_WORKBENCH")
        self.chat_options = {"options": {"temperature": 0.2}, **dict(cfg.get("chat_options", {}))}
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
        r = providers.timed_chat(self.provider, messages, self.blender_model, **self.chat_options)
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
            elif NOT_PYTHON.search(code) or _syntax_error(code):
                m = NOT_PYTHON.search(code)
                problem = (f"This is not valid Blender Python: {('found ' + repr(m.group(0).strip())) if m else _syntax_error(code)}. "
                           "Write plain Python with bpy (no three.js/JavaScript syntax).")
                rep = {"ok": False, "error": problem}
                script = code
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
                    issues = blender_physics(rep)
                    if not issues or round_ == self.max_fix_rounds:
                        rep["warnings"] = issues
                        rep["fix_rounds"] = list(self.log)
                        self.log.append({"round": round_, "result": "ok"})
                        return rep, script, usage
                    rep = {**rep, "ok": False, "error": "The script ran, but the scene is physically wrong (values are "
                                                         "for each object's centre, Z up): " + "; ".join(issues[:8])}
                problem = rep.get("error", "unknown Blender error")
            self.log.append({"round": round_, "result": (problem if len(problem) <= 600 else problem[:300] + " … " + problem[-300:])})
            messages = messages + [{"role": "assistant", "content": raw},
                                   {"role": "user", "content": "The script was checked (and run in Blender if valid) and has this problem; send the full fixed "
                                                               f"script.\n{problem[-2500:]}"}]
        return rep, script, usage

    # ------------------------------------------------------------------ simple scenes: JSON, no Blender
    def _json_messages(self, ctx: dict, budget: int) -> list[dict]:
        task = ctx["task"]
        params = task.get("params") or {}
        payload = {"goal": ctx.get("goal", ""), "task": task["title"],
                   "description": params.get("prompt") or task.get("description", ""),
                   "acceptance_criteria": [c["description"] for c in task["acceptance_criteria"]],
                   "polygon_budget": budget, "prior_failures": ctx.get("prior_failures", [])}
        return [{"role": "system", "content": scene_json.SCENE_JSON_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)}]

    def build_json(self, ctx: dict, work: Path, want: dict[str, str | None], budget: int) -> tuple[dict, str, dict]:
        """Model writes a scene description -> SHAMS validates, builds GLB, renders -> fix loop."""
        messages = self._json_messages(ctx, budget)
        usage = {"tokens_in": 0, "tokens_out": 0, "wall_seconds": 0.0}
        opts = dict(self.chat_options)
        if getattr(self.provider, "name", "") == "ollama":
            opts["format"] = "json"
        rep, raw = {"ok": False, "error": "no attempt"}, ""
        for round_ in range(self.max_fix_rounds + 1):
            r = providers.timed_chat(self.provider, messages, self.model, **opts)
            raw = r.get("text", "")
            for k in ("tokens_in", "tokens_out", "wall_seconds"):
                usage[k] += r.get(k, 0)
            try:
                last = round_ == self.max_fix_rounds        # last round: geometry warnings no longer block
                rep = scene_json.build_scene(raw, want.get("glb"), want.get("render"), resolution=self.resolution,
                                             max_triangles=budget, check_support=not last)
                rep["scene_description"] = scene_json.extract_json(raw)
                rep["fix_rounds"] = [e for e in self.log]          # what each earlier round got wrong
                self.log.append({"round": round_, "result": "ok"})
                return rep, raw, usage
            except scene_json.SceneError as e:
                problem = str(e)
            rep = {"ok": False, "error": problem}
            self.log.append({"round": round_, "result": (problem if len(problem) <= 600 else problem[:300] + " … " + problem[-300:])})
            messages = messages + [{"role": "assistant", "content": raw[:6000]},
                                   {"role": "user", "content": f"SHAMS could not build this scene: {problem}\n"
                                                               "Send the full corrected JSON object."}]
        return rep, raw, usage

    def run(self, ctx: dict, worker_ref: dict) -> tuple[list[dict], dict]:
        adir, plan = output_plan(ctx, self.attempts_root)
        adir.mkdir(parents=True, exist_ok=True)
        builder, bwhy = scene_builder(ctx["task"], blender_available=bool(self.blender))
        if builder == "blender" and not self.blender:
            return [], base_handoff(ctx, worker_ref, "BLOCKED", "Blender not found",
                                    blocked_reason="tool unavailable: Blender program not found (needed for a complex "
                                                   f"scene: {bwhy}); set tools.blender.path")
        route, why = choose_3d_route(ctx["task"], ctx.get("inputs", []), sf3d_available=False)
        if route == "reuse":
            route = "procedural"          # adapting upstream meshes is a later step; build procedurally
        by_mt = {o["media_type"]: o for o in plan}
        work = adir / ("_scene" if builder == "json" else "_blender")
        work.mkdir(exist_ok=True)
        want = {"glb": str(work / "scene.glb") if "model/gltf-binary" in by_mt else None,
                "render": str(work / "render.png")}
        budget = next((c.get("check", {}).get("max_triangles") for c in ctx["task"]["acceptance_criteria"]
                       if c.get("check", {}).get("max_triangles")), 50000)
        decisions = [{"decision": f"scene builder: {builder}", "reason": bwhy}, {"decision": f"3D route: {route}", "reason": why}]
        if builder == "json":
            rep, script, usage = self.build_json(ctx, work, want, int(budget))
            decisions.append({"decision": "scene described as JSON, GLB + render built by SHAMS",
                              "reason": "simple scene; Blender is reserved for complex scenes"})
            if not rep.get("ok") and self.blender:
                decisions += [{"decision": f"json fix round {e['round']}", "reason": e["result"]} for e in self.log
                              if e["result"] != "ok"]
                decisions.append({"decision": "fall back to Blender", "reason": "JSON scene failed after fix rounds"})
                self.log = []
                builder, work = "blender", adir / "_blender"
                work.mkdir(exist_ok=True)
                want = {"glb": str(work / "scene.glb") if want["glb"] else None, "render": str(work / "render.png")}
                rep, script, u2 = self.build(ctx, work, want, route)
                usage = {k: usage[k] + u2[k] for k in usage}
        else:
            rep, script, usage = self.build(ctx, work, want, route)
            decisions.append({"decision": "procedural geometry via Blender Python", "reason": "complex scene"})
        decisions += [{"decision": f"fix round {e['round']}", "reason": e["result"]} for e in self.log if e["result"] != "ok"]
        if not rep.get("ok"):
            return [], base_handoff(ctx, worker_ref, "FAILED", f"3D scene failed ({builder})", usage=usage,
                                    decisions=decisions,
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
            elif mt in ("text/x-python", "text/plain") and builder == "blender":
                dst.write_text(script, encoding="utf-8")
            elif mt == "application/json":
                dst.write_text(json.dumps(rep, indent=1), encoding="utf-8")
            else:
                continue
            files.append(o)
        tool = f"Blender {rep.get('blender')}" if builder == "blender" else "SHAMS scene JSON builder"
        summary = (f"Built {rep.get('mesh_objects')} mesh objects with {tool} "
                   f"({route}); materials: {', '.join(rep.get('materials', [])[:6]) or 'none'}")
        return files, base_handoff(ctx, worker_ref, "READY_FOR_QC", summary, usage=usage, decisions=decisions)




def blender_physics(rep: dict) -> list[str]:
    """Same physical checks as JSON scenes, on the world bounding boxes Blender reported (Z up -> Y up)."""
    boxes = []
    for o in rep.get("objects", []):
        if o.get("type") == "MESH" and o.get("bbox"):
            lo, hi = o["bbox"]
            boxes.append(({"name": o["name"], "shape": "mesh"}, [lo[0], lo[2], lo[1]], [hi[0], hi[2], hi[1]]))
    return scene_json.box_problems(boxes, axis="z") if boxes else []


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

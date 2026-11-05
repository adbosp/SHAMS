"""Integration self-test on the real machine (ADR-120): ``shams selftest``.

Every test really executes against the installed models and tools. Results are PASS / FAIL / SKIPPED /
BLOCKED; nothing is reported as passing without having run:

* PASS     the step ran and every check passed
* FAIL     the step ran and a check failed (the detail says which)
* BLOCKED  a prerequisite is missing on this machine (model, tool, file, service); nothing ran
* SKIPPED  not selected, or depends on an earlier step that did not pass

Writes ``report.json`` and ``report.md`` plus all evidence (scripts, GLB, renders, images) into
``<out>/``.
"""
from __future__ import annotations

import json
import platform
import shutil
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from shams import contracts, providers
from shams.models import ModelRegistry
from shams.nodes.registry import NodeRegistry
from shams.verification import assets
from shams.verification.checkers import AssetCheckers, make_visual_checker

PASS, FAIL, SKIPPED, BLOCKED = "PASS", "FAIL", "SKIPPED", "BLOCKED"

PLANNER_GOAL = ("Make a tiny Python command-line tool that converts temperatures between Celsius and Fahrenheit, "
                "with pytest unit tests, plus a simple 3D scene (a wooden table with a red ball on it) rendered "
                "as preview evidence.")
SCENE_TASK = ("Simple low-poly scene: a wooden table (rectangular top about 1.2 x 0.8 m, 0.75 m high, four legs) "
              "standing on a large grey floor plane, a red sphere (radius 0.12 m) resting on the table top, "
              "a camera looking at the table and one sun light.")
CODE_TASK = ("Write temp.py with functions c_to_f(c: float) -> float and f_to_c(f: float) -> float, and "
             "test_temp.py with pytest tests (0C=32F, 100C=212F, -40C=-40F, round trip). Reply with exactly two "
             "fenced code blocks: first ```python title=temp.py then ```python title=test_temp.py .")
BUGGY = "def c_to_f(c):\n    return c * 9 / 5\n\n\ndef f_to_c(f):\n    return (f - 32) * 5 / 9\n"
BUGGY_TEST = ("from temp import c_to_f, f_to_c\n\n\ndef test_freezing():\n    assert c_to_f(0) == 32\n\n\n"
              "def test_boiling():\n    assert c_to_f(100) == 212\n\n\ndef test_round_trip():\n"
              "    assert abs(f_to_c(c_to_f(37.5)) - 37.5) < 1e-9\n")


class Ctx:
    def __init__(self, settings: dict, out: Path, log: Callable[[str], None]):
        self.settings, self.out, self.log = settings, out, log
        self.reg = ModelRegistry(settings)
        self.results: dict[str, dict] = {}
        self.data: dict[str, Any] = {}
        o = (settings.get("models") or {}).get("ollama") or {}
        self.ollama = providers.create("ollama", {k: v for k, v in o.items() if k == "base_url"} | {"keep_alive": "5m"})

    def binding(self, role: str):
        return self.reg.binding(role)

    def model(self, role: str) -> str | None:
        b = self.binding(role)
        return b.model_id if b and b.ok else None

    def free_gpu(self) -> list[str]:
        """GPU sequential: evict every Ollama model (and ask the image backend to free VRAM)."""
        freed = []
        try:
            freed = self.ollama.unload_all()
        except providers.ProviderError:
            pass
        c = (self.settings.get("tools") or {}).get("comfyui")
        if c:
            from shams_tools.comfyui import ComfyUI
            ComfyUI(**{k: v for k, v in c.items() if k == "base_url"}).unload()
        return freed


def _vram() -> str:
    try:
        from shams import hardware
        g = hardware.gpus()
        return ", ".join(f"{x['name']}: {x['vram_used_mb'] or '?'} / {x['vram_total_mb']} MB" for x in g) or "no GPU"
    except Exception:  # noqa: BLE001
        return "?"


def _code_blocks(text: str) -> list[str]:
    import re
    return [b.strip() + "\n" for b in re.findall(r"```(?:python|py)?[^\n]*\n(.*?)```", text, re.S)]


def _pytest(files: dict[str, str], workdir: Path) -> tuple[str, str]:
    workdir.mkdir(parents=True, exist_ok=True)
    outs = []
    for name, code in files.items():
        p = workdir / name
        p.write_text(code, encoding="utf-8")
        outs.append({"logical_id": name.replace(".", "_"), "path": name, "media_type": "text/x-python",
                     "file": str(p), "role": "output"})
    return AssetCheckers()._pytest({"description": "pytest"}, outs)


# --------------------------------------------------------------------------- tests
def t1_planner(c: Ctx) -> tuple[str, str]:
    from shams.planning import llm_planner
    m = c.model("planner")
    if not m:
        return BLOCKED, f"planner role not resolved: {c.binding('planner').as_dict() if c.binding('planner') else 'not configured'}"
    workers = c.reg.workers()
    caps = sorted({cap for w in workers for cap in w["capabilities"]})
    nodes = NodeRegistry.from_dict({"node": [{"id": "node.local", "kind": "local", "cpu_slots": 2,
                                              "gpus": [{"index": 0, "vram_mb": 12288}]}],
                                    "worker": [{k: v for k, v in w.items() if k != "config"} | {"node": "node.local"}
                                               for w in workers]})
    prov = providers.create("ollama", {"base_url": c.ollama.root, "num_ctx": 16384})
    r = llm_planner.plan(prov, m, goal=PLANNER_GOAL, project_id="selftest", capabilities=caps, nodes=nodes,
                         max_rounds=3)
    (c.out / "t1_planner_reply.txt").write_text(r["raw"], encoding="utf-8")
    if r["dag"] is None:
        return FAIL, f"{m}: no valid DAG after {r['rounds']} rounds: " + "; ".join(r["issues"][:5])
    (c.out / "t1_dag.json").write_text(json.dumps(r["dag"], indent=1, ensure_ascii=False), encoding="utf-8")
    tasks = r["dag"]["tasks"]
    return PASS, (f"{m}: valid DAG, {len(tasks)} tasks, capabilities "
                  f"{sorted({t['capability'] for t in tasks})}, {r['rounds']} round(s), {r['seconds']} s")


def t2_coder(c: Ctx) -> tuple[str, str]:
    m = c.model("coder")
    if not m:
        return BLOCKED, "coder role not resolved"
    prov = providers.create("ollama", {"base_url": c.ollama.root, "num_ctx": 16384})
    t0 = time.monotonic()
    r = prov.chat([{"role": "user", "content": CODE_TASK}], model=m)
    blocks = _code_blocks(r["text"])
    (c.out / "t2_create_reply.txt").write_text(r["text"], encoding="utf-8")
    if len(blocks) < 2:
        return FAIL, f"{m}: create step returned {len(blocks)} code blocks, expected 2"
    v1, d1 = _pytest({"temp.py": blocks[0], "test_temp.py": blocks[1]}, c.out / "t2_create")
    v0, d0 = _pytest({"temp.py": BUGGY, "test_temp.py": BUGGY_TEST}, c.out / "t2_buggy")
    if v0 != FAIL:
        return FAIL, "fixture error: the buggy module should fail its tests"
    fix_prompt = (f"temp.py fails its tests.\n```python\n{BUGGY}```\npytest output:\n{d0[-800:]}\n"
                  "Return only the corrected temp.py in one ```python block.")
    r2 = prov.chat([{"role": "user", "content": fix_prompt}], model=m)
    (c.out / "t2_fix_reply.txt").write_text(r2["text"], encoding="utf-8")
    fixed = (_code_blocks(r2["text"]) or [""])[0]
    v2, d2 = _pytest({"temp.py": fixed, "test_temp.py": BUGGY_TEST}, c.out / "t2_fix")
    secs = round(time.monotonic() - t0, 1)
    if v1 == PASS and v2 == PASS:
        return PASS, f"{m}: created code passes its tests ({d1}); fixed the bug, tests pass ({d2}); {secs} s"
    return FAIL, f"{m}: create={v1} ({d1[:200]}); fix={v2} ({d2[:200]})"


def t3_scene(c: Ctx) -> tuple[str, str]:
    from shams_tools.blender import Scene3DWorker, blender_from
    m = c.model("scene3d")
    bl = blender_from(c.settings)
    if not m:
        return BLOCKED, "scene3d role not resolved"
    if not bl:
        return BLOCKED, "Blender not found (tools.blender.path)"
    c.free_gpu()
    entry = {"worker": {"worker_id": "selftest.scene3d", "kind": "llm_agent", "model": m},
             "provider_config": {"blender": bl, "max_fix_rounds": 2}}
    prov = providers.create("ollama", {"base_url": c.ollama.root, "num_ctx": 16384})
    w = Scene3DWorker(entry=entry, attempts_root=c.out, provider=prov)
    work = c.out / "t3_scene"
    work.mkdir(exist_ok=True)
    ctx = {"goal": "selftest", "task": {"task_id": "task.scene", "title": "Table scene", "description": SCENE_TASK,
                                        "acceptance_criteria": [{"description": "table, ball, camera, light",
                                                                 "check": {"max_triangles": 20000}}]}}
    rep, script, usage = w.build(ctx, work, {"glb": str(work / "scene.glb"), "render": str(work / "render.png")},
                                 "procedural")
    (work / "scene_report.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    fixes = sum(1 for e in w.log if e["result"] != "ok")
    if not rep.get("ok"):
        return FAIL, f"{m}: Blender scene failed after {fixes} fix round(s): {rep.get('error', '')[-300:]}"
    c.data["scene"] = {"glb": str(work / "scene.glb"), "render": str(work / "render.png"), "report": rep}
    return PASS, (f"{m} + Blender {rep.get('blender')}: {rep.get('mesh_objects')} meshes, materials "
                  f"{rep.get('materials')}, fix rounds {fixes}, {round(usage['wall_seconds'], 1)} s")


def t4_render(c: Ctx) -> tuple[str, str]:
    from shams_tools.blender import check_blender_import
    s = c.data.get("scene")
    if not s:
        return SKIPPED, "needs test 3"
    rv, rd = assets.check_png(s["render"], {"min_width": 256, "min_std": 4})
    gv, gd = assets.check_glb(s["glb"], {"min_vertices": 24, "min_size_m": 0.5, "max_size_m": 100,
                                         "require_material": True})
    sv, sd = AssetCheckers()._scene_report({"check": {"min_objects": 2, "require_materials": True}},
                                           [{"logical_id": "r", "media_type": "application/json",
                                             "file": str(Path(s["glb"]).with_name("scene_report.json"))}])
    iv, idt = check_blender_import({"check": {}}, [{"logical_id": "g", "media_type": "model/gltf-binary",
                                                   "file": s["glb"]}], c.settings)
    parts = [("render", rv, rd), ("glb", gv, gd), ("scene", sv, sd), ("import", iv, idt)]
    bad = [f"{n}: {d}" for n, v, d in parts if v != PASS]
    detail = "; ".join(f"{n} {v}: {d}" for n, v, d in parts)
    return (FAIL if bad else PASS), detail


def t4b_visual(c: Ctx) -> tuple[str, str]:
    s = c.data.get("scene")
    m = c.model("visual_qc")
    if not m:
        return BLOCKED, "visual_qc role not resolved"
    if not s:
        return SKIPPED, "needs test 3"
    c.free_gpu()
    prov = providers.create("ollama", {"base_url": c.ollama.root, "num_ctx": 8192})
    v, d = make_visual_checker(prov, m)([s["render"]], "The render shows a table with a red ball on top of it.",
                                        {"min_score": 5})
    return (PASS if v == PASS else FAIL if v == FAIL else BLOCKED), d


def _comfy(c: Ctx, workflow: str, key: str) -> tuple[str, str]:
    from shams_tools.comfyui import ComfyUI, MissingResources
    cfg = (c.settings.get("tools") or {}).get("comfyui")
    if not cfg:
        return BLOCKED, "tools.comfyui not configured"
    prov = ComfyUI(**{k: v for k, v in cfg.items() if k in ("base_url", "workflows", "launch", "launch_cwd",
                                                             "autostart", "timeout")})
    try:
        c.free_gpu()
        prov.ensure_running()
    except providers.ProviderError as e:
        return BLOCKED, str(e)
    params = dict(((cfg.get("params") or {}).get(workflow)) or {})
    params.update(prompt="a cozy wooden cabin in a snowy pine forest at dusk, warm window light, detailed",
                  width=768, height=768, seed=42)
    try:
        r = prov.generate(workflow, params)
    except MissingResources as e:
        return BLOCKED, "missing in ComfyUI: " + "; ".join(e.problems)
    except providers.ProviderError as e:
        return FAIL, str(e)[:500]
    finally:
        prov.unload()
    path = c.out / f"t5_{key}.png"
    path.write_bytes(r["images"][0])
    v, d = assets.check_png(path, {"min_width": 512, "min_std": 8})
    c.data[f"image_{key}"] = str(path)
    return v, f"{workflow}: {d}, {r['seconds']} s"


def t5a_sdxl(c: Ctx) -> tuple[str, str]:
    return _comfy(c, "sdxl_lightning_4step", "sdxl")


def t5b_zimage(c: Ctx) -> tuple[str, str]:
    return _comfy(c, "z_image_turbo", "zimage")


def t6a_sf3d_policy(c: Ctx) -> tuple[str, str]:
    from shams_tools.routing import choose_3d_route
    from shams_tools.sf3d import StableFast3DWorker

    class NeverCalled:
        calls = 0

        def generate(self, *a, **k):
            NeverCalled.calls += 1
            raise AssertionError("SF3D must not run")
    basic = {"task_id": "task.room", "title": "Basic room scene", "description": "a table, two chairs and a lamp",
             "acceptance_criteria": []}
    img = [{"logical_id": "ref", "media_type": "image/png", "sha256": "0" * 64, "path": "ref.png"}]
    checks = [choose_3d_route(basic, [], sf3d_available=True)[0] != "image_to_3d",
              choose_3d_route(basic, img, sf3d_available=True)[0] != "image_to_3d",
              choose_3d_route({**basic, "title": "Detailed character statue", "description": "organic"}, img,
                              sf3d_available=True)[0] == "image_to_3d"]
    w = StableFast3DWorker(entry={"worker": {"worker_id": "x"}, "provider_config": {}}, attempts_root=c.out,
                           provider=NeverCalled())
    ctx = {"project_id": "selftest", "attempt": 1, "inputs": [], "task": {**basic, "outputs": [
        {"name": "m", "logical_id": "mesh.room", "media_type": "model/gltf-binary"}]}}
    _, h1 = w.run(ctx, {"worker_id": "x", "kind": "tool"})
    _, h2 = w.run({**ctx, "inputs": img}, {"worker_id": "x", "kind": "tool"})
    ok = all(checks) and h1["status"] == "BLOCKED" and h2["status"] == "BLOCKED" and NeverCalled.calls == 0
    return (PASS if ok else FAIL), (f"basic scene -> procedural; basic + image -> not SF3D; complex + image -> SF3D; "
                                    f"worker refused twice ({h1.get('blocked_reason')[:60]} | "
                                    f"{h2.get('blocked_reason')[:60]}), SF3D calls {NeverCalled.calls}")


def t6b_sf3d_run(c: Ctx) -> tuple[str, str]:
    from shams_tools.sf3d import StableFast3D
    cfg = (c.settings.get("tools") or {}).get("stable_fast_3d")
    if not cfg:
        return BLOCKED, "tools.stable_fast_3d not configured"
    prov = StableFast3D(**{k: v for k, v in cfg.items() if k != "enabled"})
    h = prov.health_report()
    if h["problems"]:
        return BLOCKED, "; ".join(h["problems"])
    ref = Path(cfg["repo"]) / "demo_files" / "examples" / "chair1.png"
    if not ref.exists():
        ref = Path(c.data.get("image_sdxl", ""))
    if not ref.exists():
        return BLOCKED, "no reference image (demo_files/examples/chair1.png missing and test 5a produced none)"
    c.free_gpu()
    try:
        r = prov.generate(ref, c.out / "t6b_sf3d")
    except providers.ProviderError as e:
        msg = str(e)
        if "ModuleNotFoundError" in msg or "No module named" in msg:
            return BLOCKED, "SF3D environment incomplete: " + msg[-300:]
        return FAIL, msg[-500:]
    v, d = assets.check_glb(r["glb"], {"require_uv": True, "require_texture": True, "min_vertices": 100})
    return v, f"{ref.name} -> mesh.glb: {d}, {r['seconds']} s"


def t7_switching(c: Ctx) -> tuple[str, str]:
    order = [r for r in ("planner", "coder", "scene3d", "visual_qc", "coder", "planner") if c.model(r)]
    if len(set(order)) < 2:
        return BLOCKED, "needs at least two resolved Ollama roles"
    c.free_gpu()
    steps = []
    prev = None
    for role in order:
        m = c.model(role)
        try:
            if prev and prev != m:
                c.ollama.unload(prev)
            r = c.ollama.chat([{"role": "user", "content": "Reply with the single word OK."}], model=m,
                              options={"num_ctx": 4096, "num_predict": 64})
            note = ""
            if not r["text"].strip():
                r = c.ollama.chat([{"role": "user", "content": "Reply with the single word OK."}], model=m,
                                  think=True, options={"num_ctx": 4096, "num_predict": 1024})
                note = " (answered only with thinking on)" if r["text"].strip() else " (NO ANSWER)"
            ps = c.ollama.ps()
        except providers.ProviderError as e:
            return FAIL, f"switch to {m} failed: {e}; steps so far: {steps}"
        loaded = {x["name"]: x for x in ps}
        info = loaded.get(m)
        if info is None:
            return FAIL, f"{m} answered but is not listed by /api/ps"
        others = [n for n in loaded if n != m]
        gpu_pct = round(100 * info["size_vram"] / info["size"]) if info["size"] else 0
        steps.append(f"{role}={m.split('/')[-1]} {gpu_pct}% GPU" + (f" (also loaded: {others})" if others else "")
                     + f" '{r['text'].strip()[:10]}'{note}")
        if not r["text"].strip():
            return FAIL, f"{m} loaded but returned no answer: " + "; ".join(steps)
        if others:
            return FAIL, "previous model still loaded after unload: " + "; ".join(steps)
        prev = m
    c.ollama.unload(prev)
    partial = [s for s in steps if "100% GPU" not in s]
    return PASS, ("; ".join(steps) + f"; VRAM now {_vram()}"
                  + (f". Note: partly on CPU: {partial}" if partial else ""))


def t8_pipeline(c: Ctx) -> tuple[str, str]:
    """Real Shams run: 2 coder tasks (typed handoff), QC with pytest, stop after task 1, resume.
    8 checks the machinery (restart does not redo accepted work, the next task gets the accepted artifact,
    QC verdicts route the task); 8b checks that the local model actually finished the work."""
    from shams.orchestration.project import Shams
    from shams.statemachine import S
    if not c.model("coder"):
        return BLOCKED, "coder role not resolved"
    d = c.out / "t8_project"
    if d.exists():
        shutil.rmtree(d)
    Shams.init_project(d, "selftest_pipeline", "temperature converter", mode="autonomous", domain="software",
                       domain_config={"stack": "python"})
    crit = lambda tid: [{"id": f"ac.{tid}_compile", "description": "compiles", "kind": "compile",  # noqa: E731
                         "check": {"type": "python"}},
                        {"id": f"ac.{tid}_tests", "description": "pytest passes", "kind": "unit_test",
                         "check": {"type": "pytest"}}]
    base = {"schema_version": contracts.SCHEMA_VERSION, "project_id": "selftest_pipeline", "max_attempts": 3}
    dag = {"schema_version": contracts.SCHEMA_VERSION, "project_id": "selftest_pipeline", "plan_id": "plan.selftest",
           "plan_version": 1, "goal": "temperature converter", "domain": "software", "tasks": [
               {**base, "task_id": "task.temp_module", "title": "temp.py: c_to_f and f_to_c with pytest tests",
                "description": "temp.py defines c_to_f(c) and f_to_c(f). test_temp.py tests 0C=32F, 100C=212F.",
                "capability": "code.python", "depends_on": [], "inputs": [],
                "outputs": [{"name": "temp", "logical_id": "code.temp", "media_type": "text/x-python"},
                            {"name": "test_temp", "logical_id": "test.temp", "media_type": "text/x-python"}],
                "acceptance_criteria": crit("temp")},
               {**base, "task_id": "task.cli", "title": "cli.py using temp.c_to_f, with pytest tests",
                "description": "cli.py has main(argv) that prints c_to_f(float(argv[0])) using 'from temp import "
                               "c_to_f'. test_cli.py tests main(['100']) prints 212.0 (use capsys).",
                "capability": "code.python", "depends_on": ["task.temp_module"],
                "inputs": [{"name": "temp", "from_task": "task.temp_module", "logical_id": "code.temp"}],
                "outputs": [{"name": "cli", "logical_id": "code.cli", "media_type": "text/x-python"},
                            {"name": "test_cli", "logical_id": "test.cli", "media_type": "text/x-python"}],
                "acceptance_criteria": crit("cli")}]}
    st = Shams(d)
    try:
        st.load_plan(dag)
        for _ in range(200):
            t1 = {t["task_id"]: t for t in st.db.tasks()}["task.temp_module"]
            if S(t1["state"]) in (S.ACCEPTED, S.FAILED, S.HUMAN_REVIEW, S.CANCELED):
                break
            st.step()
        first_state = S(t1["state"]).value
        leases_before = st.db.q1("SELECT COUNT(*) FROM events WHERE type='task.leased' AND subject_id='task.temp_module'")[0]
    finally:
        st.close()
    if first_state != "ACCEPTED":
        return FAIL, f"task 1 ended {first_state} before the restart"
    st = Shams(d)                         # restart from the event store + LangGraph checkpoints
    try:
        summary = st.run()
        tasks = {t["task_id"]: t for t in st.db.tasks()}
        leases_after = st.db.q1("SELECT COUNT(*) FROM events WHERE type='task.leased' AND subject_id='task.temp_module'")[0]
        h = st.db.handoff("task.cli") or {}
        acc = st.artifacts.accepted("code.temp") or {}
        qc = st.db.q("SELECT type, subject_id FROM events WHERE type LIKE 'qc.%'")
        repairs = summary.get("repairs", 0)
    finally:
        st.close()
    states = {k: v["state"] for k, v in tasks.items()}
    handoff_ok = any(i.get("logical_id") == "code.temp" and i.get("sha256") == acc.get("sha256")
                     for i in h.get("inputs", []))
    redone = leases_after != leases_before
    qc_failed = sum(1 for r in qc if r["type"] == "qc.failed")
    routed = states.get("task.cli") in ("ACCEPTED", "HUMAN_REVIEW")
    c.data["pipeline"] = {"states": states, "done": summary["done"], "repairs": repairs}
    detail = (f"task 1 leases before/after restart {leases_before}/{leases_after} (not redone: {not redone}); "
              f"task.cli received code.temp with the accepted hash: {handoff_ok}; QC events {len(qc)} "
              f"({qc_failed} failed); task.cli ended {states.get('task.cli')}")
    return (PASS if handoff_ok and not redone and routed else FAIL), detail


def t8b_pipeline_done(c: Ctx) -> tuple[str, str]:
    p = c.data.get("pipeline")
    if not p:
        return SKIPPED, "needs test 8"
    return (PASS if p["done"] else FAIL), f"states {p['states']}; repairs {p['repairs']}"


TESTS: list[tuple[str, str, Callable[[Ctx], tuple[str, str]]]] = [
    ("1", "Planner model writes a valid Task DAG", t1_planner),
    ("2", "Coding model creates code and fixes a bug (pytest)", t2_coder),
    ("3", "3D Scene model builds a Blender scene (GLB + render)", t3_scene),
    ("4", "Scene render and geometry verified (PNG, GLB, report, Blender import)", t4_render),
    ("4b", "Visual QC by the vision model on the render", t4b_visual),
    ("5a", "SDXL-Lightning 4-step through ComfyUI", t5a_sdxl),
    ("5b", "Z-Image-Turbo through ComfyUI", t5b_zimage),
    ("6a", "Stable Fast 3D is not called for basic scenes (policy)", t6a_sf3d_policy),
    ("6b", "Stable Fast 3D image-to-3D with a reference image (GLB/UV/texture)", t6b_sf3d_run),
    ("7", "Ollama model switching without OOM", t7_switching),
    ("8", "Pipeline: restart from checkpoint, typed handoff, QC routing", t8_pipeline),
    ("8b", "Pipeline: local coder finishes every task (QC accepted)", t8b_pipeline_done),
]


def run(settings: dict, out: Path, only: set[str] | None = None, log: Callable[[str], None] = print) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    c = Ctx(settings, out, log)
    t_all = time.monotonic()
    try:
        env = c.reg.report()
    except Exception as e:  # noqa: BLE001
        env = {"error": str(e)}
    rows = []
    for tid, name, fn in TESTS:
        if only and tid not in only and tid.rstrip("ab") not in only:
            rows.append({"id": tid, "name": name, "status": SKIPPED, "detail": "not selected", "seconds": 0})
            continue
        log(f"[{tid}] {name} ...")
        t0 = time.monotonic()
        try:
            status, detail = fn(c)
        except Exception as e:  # noqa: BLE001 - a crashing test is a FAIL with its traceback
            status, detail = FAIL, f"{type(e).__name__}: {e}"
            (out / f"t{tid}_traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        row = {"id": tid, "name": name, "status": status, "detail": detail, "seconds": round(time.monotonic() - t0, 1)}
        rows.append(row)
        log(f"[{tid}] {status}  {detail[:300]}")
    try:
        c.free_gpu()
    except Exception:  # noqa: BLE001
        pass
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in (PASS, FAIL, BLOCKED, SKIPPED)}
    rep = {"created_at": contracts.now_iso(), "host": platform.node(), "os": platform.platform(),
           "vram": _vram(), "seconds": round(time.monotonic() - t_all, 1), "counts": counts, "tests": rows,
           "environment": env}
    (out / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "report.md").write_text(markdown(rep), encoding="utf-8")
    return rep


def markdown(rep: dict) -> str:
    lines = [f"# SHAMS self-test — {rep['host']}", "", f"{rep['created_at']} · {rep['os']} · {rep['vram']} · "
             f"{rep['seconds']} s", "", "PASS {PASS} · FAIL {FAIL} · BLOCKED {BLOCKED} · SKIPPED {SKIPPED}".format(
                 **rep["counts"]), "", "| # | Test | Result | Time | Detail |", "|---|---|---|---|---|"]
    for r in rep["tests"]:
        lines.append(f"| {r['id']} | {r['name']} | **{r['status']}** | {r['seconds']} s | "
                     f"{r['detail'].replace('|', '/').replace(chr(10), ' ')[:600]} |")
    env = rep.get("environment") or {}
    if env.get("roles"):
        lines += ["", "## Roles", "", "| Role | Wanted | Installed ID | Status |", "|---|---|---|---|"]
        for r in env["roles"]:
            lines.append(f"| {r['role']} | {r['wanted']} | {r['model_id'] or '—'} | {r['status']} {r['how']} |")
    return "\n".join(lines) + "\n"


def default_out(workspace: str | Path | None = None) -> Path:
    base = Path(workspace) if workspace else Path(tempfile.gettempdir())
    return base / "_selftest" / time.strftime("%Y%m%d-%H%M%S")

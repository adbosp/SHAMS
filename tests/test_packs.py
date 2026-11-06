"""Domain Packs (ADR-117): the core knows no product domain; packs add it."""
import json
import pkgutil
import shutil
import re
import subprocess
import sys
from pathlib import Path

import pytest

import shams
from shams import packs
from shams.mock.worker import MockDefect, MockWorker
from shams.orchestration.project import Shams
from shams.planning.validator import instantiate, validate_dag

ROOT = Path(shams.__file__).resolve().parent
BANNED = re.compile(r"unity|unreal|godot|blender|comfyui|three\.js|shark|cá mập", re.I)


def test_core_imports_no_pack():
    mods = [m.name for m in pkgutil.walk_packages([str(ROOT)], "shams.")]
    code = ("import importlib, sys\n"
            f"for m in {mods!r}: importlib.import_module(m)\n"
            "bad = [m for m in sys.modules if m.startswith('shams_packs')]\n"
            "assert not bad, bad\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(ROOT.parent))
    assert r.returncode == 0, r.stderr


def test_core_source_names_no_engine_or_tool():
    offenders = []
    for p in list(ROOT.rglob("*.py")) + list((ROOT / "contracts" / "schemas").glob("*.json")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if BANNED.search(line):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("stack,cap", [("python", "code.python"), ("typescript", "code.web_typescript")])
def test_software_pack_runs_end_to_end(tmp_path, stack, cap):
    d = tmp_path / stack
    Shams.init_project(d, "sw_app", "một công cụ CLI", mode="autonomous", mock_mode=True,
                       domain="software", domain_config={"stack": stack})
    st = Shams(d)
    dag = st.plan_from_template()
    assert dag["domain"] == "software" and dag["template"] == f"software_basic@{stack}"
    assert not [i for i in validate_dag(dag) if i.level == "error"]
    assert any(t["capability"] == cap for t in st.db.tasks())
    s = st.run()
    assert s["done"] and s["counts"] == {"ACCEPTED": 5}
    ctx = st.context.compile("task.core_module", tools_allowed=[])
    assert ctx["domain"] == "software" and ctx["domain_context"]["stack"] == stack


@pytest.mark.parametrize("text,domain,hints", [
    ("Làm website bán hàng bằng React", "software", {"stack": "typescript"}),
    ("Viết script Python xử lý CSV", "software", {"stack": "python"}),
    ("Game platformer 2D bằng Godot", "game", {"engine": "godot"}),
    ("Tạo một trò chơi giống GTA có thể chơi được", "game", {}),
    ("xin chào", None, {}),
])
def test_detect_domain_and_options(text, domain, hints):
    assert packs.detect_domain(text) == domain
    if domain:
        assert packs.get(domain).detect(text) == hints


def test_default_domain_comes_from_goal(tmp_path):
    Shams.init_project(tmp_path / "g", "g_one", "Game đua xe 3D", mock_mode=True)
    Shams.init_project(tmp_path / "s", "s_one", "API backend quản lý kho", mock_mode=True)
    assert Shams(tmp_path / "g").pack.name == "game"
    assert Shams(tmp_path / "s").pack.name == "software"


def test_options_are_validated_per_pack(tmp_path):
    Shams.init_project(tmp_path / "p", "p_one", "API", mock_mode=True, domain="software")
    st = Shams(tmp_path / "p")
    with pytest.raises(KeyError, match="no option"):
        st.configure(engine="godot")
    st.configure(domain="game", engine="ue5")
    assert st.pack.name == "game" and st.domain_config["engine"]["name"] == "unreal"
    assert st.describe() == "Game · Unreal Engine 5"


def test_pre_v2_project_opens_as_game(tmp_path):
    d = tmp_path / "old"
    Shams.init_project(d, "old_game", "g", mock_mode=True, mode="autonomous", engine="godot")
    cfg = json.loads((d / "project.json").read_text(encoding="utf-8"))
    legacy = {k: v for k, v in cfg.items() if k not in ("domain", "domain_config")}
    legacy["engine"] = {"name": "godot", "version": "4.3"}
    (d / "project.json").write_text(json.dumps(legacy), encoding="utf-8")
    st = Shams(d)
    assert st.pack.name == "game" and st.domain_config["engine"]["name"] == "godot"


def test_pack_failure_rules_and_mock_logs(tmp_path):
    for domain, cfg, crit, task in (("game", {"engine": "unity"}, "ac.movement_compiles", "task.player_controller"),
                                    ("software", {"stack": "python"}, "ac.core_compiles", "task.core_module")):
        d = tmp_path / domain
        Shams.init_project(d, f"{domain}_x", "x", mode="autonomous", mock_mode=True, domain=domain, domain_config=cfg)
        st = Shams(d, mock=MockWorker(d / "attempts", defects={task: MockDefect(crit)}))
        st.plan_from_template()
        assert st.run()["done"]
        f = st.db.failures(task)[0]
        assert f["failure_class"] == "compile_error"
        assert ("CS0103" in f["summary"]) == (domain == "game")


class NotesPack(packs.DomainPack):
    name = "notes"
    display = "Notes"
    default_template = "one_note"

    def __init__(self, tdir):
        self.tdir = tdir

    def templates_dir(self):
        return self.tdir

    def template_tokens(self, config):
        return {"fmt": "text/markdown"}

    def match_score(self, text):
        return 10 if "ghi chú" in text.lower() else 0


def test_custom_pack_registered_in_code(tmp_path):
    tdir = tmp_path / "tpl"
    tdir.mkdir()
    (tdir / "one_note.json").write_text(json.dumps({
        "schema_version": "1.0", "project_id": "t", "plan_id": "plan.t", "plan_version": 1, "goal": "g",
        "tasks": [{"task_id": "task.note", "title": "Viết ghi chú", "capability": "write.markdown", "depends_on": [],
                   "inputs": [], "outputs": [{"name": "note", "logical_id": "doc.note", "media_type": "${fmt}"}],
                   "acceptance_criteria": [{"id": "ac.note_exists", "description": "có file", "kind": "exists"}],
                   "workspace_scope": []}]}), encoding="utf-8")
    packs.register_pack(NotesPack(tdir))
    try:
        assert packs.detect_domain("viết ghi chú họp") == "notes"
        Shams.init_project(tmp_path / "n", "notes_p", "viết ghi chú họp", mode="autonomous", mock_mode=True)
        st = Shams(tmp_path / "n")
        assert st.pack.name == "notes"
        st.plan_from_template()
        assert st.run()["counts"] == {"ACCEPTED": 1}
    finally:
        packs._REGISTERED.pop("notes", None)


# ---------------------------------------------------------------- CLI robustness (found on the PC smoke run)
def test_failed_init_leaves_nothing_behind(tmp_path):
    from apps.cli.main import main
    d = tmp_path / "p"
    assert main(["init", str(d), "--project-id", "g", "--goal", "CLI Python", "--mock"]) == 2
    assert not d.exists()
    d.mkdir(); (d / "keep.txt").write_text("x")
    assert main(["init", str(d), "--project-id", "g", "--goal", "CLI Python", "--mock"]) == 2
    assert sorted(p.name for p in d.iterdir()) == ["keep.txt"]


def test_init_refuses_existing_project(tmp_path):
    from apps.cli.main import main
    d = str(tmp_path / "p")
    assert main(["init", d, "--project-id", "notes", "--goal", "CLI Python", "--mock"]) == 0
    assert main(["init", d, "--project-id", "notes", "--goal", "CLI Python", "--mock"]) == 2


def test_run_exit_codes(tmp_path):
    from apps.cli.main import main
    d = str(tmp_path / "p")
    assert main(["init", d, "--project-id", "notes", "--goal", "CLI Python", "--mock", "--mode", "guided"]) == 0
    assert main(["run", d]) == 2                       # no plan yet
    assert main(["plan", d, "--approve", "t"]) == 0
    assert main(["run", d, "--defect", "task.core_module:" + _first_criterion(d, "task.core_module") + ":9"]) == 1
    assert main(["decide", d, "task.core_module", "accept"]) == 0
    assert main(["run", d]) == 0
    a = str(tmp_path / "auto")                         # autonomous: a task out of repairs does not stop the run
    assert main(["init", a, "--project-id", "notes", "--goal", "CLI Python", "--mock", "--mode", "autonomous"]) == 0
    assert main(["plan", a]) == 0
    assert main(["run", a, "--defect", "task.core_module:" + _first_criterion(a, "task.core_module") + ":9"]) == 0


def _first_criterion(d, task_id):
    from shams.orchestration.project import Shams
    st = Shams(d)
    try:
        return st.db.task(task_id)["acceptance_criteria"][0]["id"]
    finally:
        st.close()


def test_version():
    import shams
    assert shams.__version__.startswith("2.")


# --------------------------------------------------------------------------- real QC for the game template
def test_game_template_criteria_have_real_checks():
    for engine, mesh_type in (("web", "glb"), ("unreal", "mesh_unverified")):
        dag = instantiate("vertical_slice_3d", "p", "g", engine=engine)
        t = {x["task_id"]: x for x in dag["tasks"]}
        crit = {c["id"]: c for c in t["task.player_model"]["acceptance_criteria"]}
        assert crit["ac.player_mesh_valid"]["check"]["type"] == mesh_type
        assert crit["ac.player_mesh_valid"]["check"]["max_triangles"] == 20000
        assert any(o["media_type"] == "image/png" for o in t["task.player_model"]["outputs"])
        env = {c["id"]: c for c in t["task.environment_model"]["acceptance_criteria"]}
        assert env["ac.environment_valid"]["check"]["type"] == mesh_type


@pytest.mark.skipif(not shutil.which("node"), reason="needs Node.js")
def test_game_compile_checker_parses_web_code(tmp_path):
    from shams_packs.game import PACK
    chk = PACK.checkers()["compile"]
    ok = tmp_path / "ok.ts"
    ok.write_text('import * as THREE from "three";\nexport class P { speed: number = 3; move(dt: number): void {} }\n')
    bad = tmp_path / "bad.ts"
    bad.write_text("export class P { move(dt: number { }\n")
    cs = tmp_path / "a.cs"
    cs.write_text("class A {}")
    def out(p, mt):
        return [{"logical_id": "code.x", "path": p.name, "media_type": mt, "file": str(p), "role": "output"}]
    assert chk("compile", {}, [], outputs=out(ok, "text/typescript"))[0] == "PASS"
    v, d = chk("compile", {}, [], outputs=out(bad, "text/typescript"))
    assert v == "FAIL" and "compile error" in d
    assert chk("compile", {}, [], outputs=out(cs, "text/x-csharp"))[0] == "NOT_VERIFIED"
    assert chk("compile", {}, [], outputs=[])[0] == "FAIL"


def test_web_game_template_criteria_all_have_checkers(tmp_path):
    """A criterion with no checker stops at HUMAN_REVIEW and blocks everything after it."""
    from shams.verification.checkers import CHECK_TYPES, AssetCheckers
    from shams_packs.game import PACK
    pack = PACK.checkers()
    not_yet = {"unit_test", "screenshot", "integration_test", "runtime_log", "metric"}   # web runners pending
    dag = instantiate("vertical_slice_3d", "p", "g", engine="web")
    missing = []
    for t in dag["tasks"]:
        for c in t["acceptance_criteria"]:
            ctype = (c.get("check") or {}).get("type")
            ok = (ctype and (hasattr(AssetCheckers, f"_{ctype}") or ctype in CHECK_TYPES)) or \
                c["kind"] in pack or c["kind"] == "exists" or c["kind"] in not_yet
            if not ok and t["task_id"] != "task.sfx_core":       # audio has no worker yet: BLOCKED anyway
                missing.append(f"{t['task_id']}:{c['id']}")
    assert not missing
    f = tmp_path / "brief.txt"
    f.write_text("adult woman in her late 20s", encoding="utf-8")
    chk = AssetCheckers().table()["exists"]
    assert chk("exists", {"id": "e", "kind": "exists"}, [], outputs=[
        {"logical_id": "b", "media_type": "text/plain", "file": str(f), "role": "output"}])[0] == "PASS"
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert chk("exists", {"id": "e", "kind": "exists"}, [], outputs=[
        {"logical_id": "b", "media_type": "text/plain", "file": str(tmp_path / "empty.txt"), "role": "output"}])[0] == "FAIL"


def _web_toolchain():
    from shams_packs.game import toolchain
    return toolchain.vitest_entry() if shutil.which("node") else None


def test_web_unit_test_checker_without_toolchain_is_not_verified(tmp_path, monkeypatch):
    from shams_packs.game import PACK
    monkeypatch.setenv("SHAMS_TOOLCHAINS", str(tmp_path / "none"))
    code = tmp_path / "a.ts"
    code.write_text("export const x = 1;\n", encoding="utf-8")
    outs = [{"logical_id": "code.a", "path": "src/game/A.ts", "media_type": "text/typescript", "file": str(code)},
            {"logical_id": "test.a", "path": "src/game/ATests.ts", "media_type": "text/typescript", "file": str(code)}]
    v, d = PACK.checkers()["unit_test"]("unit_test", {"id": "t", "kind": "unit_test"}, [], outputs=outs)
    assert v == "NOT_VERIFIED" and ("toolchain install web" in d or "Node.js" in d)
    v, d = PACK.checkers()["unit_test"]("unit_test", {"id": "t", "kind": "unit_test"}, [], outputs=outs[:1])
    assert v == "FAIL" and "no test file" in d


@pytest.mark.skipif(not _web_toolchain(), reason="needs the web toolchain (shams toolchain install web)")
def test_web_unit_test_checker_runs_vitest(tmp_path):
    from shams_packs.game import PACK
    chk = PACK.checkers()["unit_test"]
    (tmp_path / "up").mkdir()
    scaffold = tmp_path / "up" / "config.ts"
    scaffold.write_text("export const SPEED: number = 2;\n", encoding="utf-8")
    code = tmp_path / "Spawner.ts"
    code.write_text("import * as THREE from 'three';\nimport { SPEED } from '../../config';\n"
                    "export function step(p: THREE.Vector3, dt: number): THREE.Vector3 {\n"
                    "  return p.clone().add(new THREE.Vector3(SPEED * dt, 0, 0));\n}\n", encoding="utf-8")
    good = tmp_path / "SpawnerTests.ts"
    good.write_text("import { describe, it, expect } from 'vitest';\nimport * as THREE from 'three';\n"
                    "import { step } from './Spawner';\n"
                    "describe('step', () => { it('moves', () => { expect(step(new THREE.Vector3(), 1).x).toBe(2); }); });\n",
                    encoding="utf-8")
    outs = [{"logical_id": "project.scaffold", "path": "src/config.ts", "media_type": "text/typescript",
             "file": str(scaffold), "role": "input"},
            {"logical_id": "code.spawner", "path": "src/game/Spawning/Spawner.ts", "media_type": "text/typescript",
             "file": str(code), "role": "output"},
            {"logical_id": "test.spawner", "path": "src/game/Spawning/SpawnerTests.ts",
             "media_type": "text/typescript", "file": str(good), "role": "output"}]
    crit = {"id": "t", "kind": "unit_test", "check": {"test_filter": "SpawnerTests"}}
    v, d = chk("unit_test", crit, [], outputs=outs)
    assert v == "PASS", d
    good.write_text(good.read_text(encoding="utf-8").replace("toBe(2)", "toBe(3)"), encoding="utf-8")
    v, d = chk("unit_test", crit, [], outputs=outs)
    assert v == "FAIL" and "tests failed" in d


@pytest.mark.skipif(not _web_toolchain(), reason="needs the web toolchain (shams toolchain install web)")
def test_web_unit_test_checker_accepts_jest_style_mocks(tmp_path):
    from shams_packs.game import PACK
    code = tmp_path / "Mech.ts"
    code.write_text("export function fire(cb: (n: number) => void) { cb(3); }\n", encoding="utf-8")
    test = tmp_path / "MechTests.ts"
    test.write_text("import { fire } from './Mech';\n"
                    "describe('fire', () => { it('calls back', () => { const cb = jest.fn(); fire(cb); "
                    "expect(cb).toHaveBeenCalledWith(3); }); });\n", encoding="utf-8")
    outs = [{"logical_id": "code.m", "path": "src/game/Mech.ts", "media_type": "text/typescript", "file": str(code)},
            {"logical_id": "test.m", "path": "src/game/MechTests.ts", "media_type": "text/typescript",
             "file": str(test)}]
    v, d = PACK.checkers()["unit_test"]("unit_test", {"id": "t", "kind": "unit_test"}, [], outputs=outs)
    assert v == "PASS", d


def test_web_unit_test_criteria_carry_the_engine_test_hint():
    from shams_packs.game import engines
    hint = engines.ENGINES["web"].test_hint
    assert "vitest" in hint and "jest" in hint
    raw = (Path(__file__).parents[1] / "shams_packs/game/templates/vertical_slice_3d.json").read_text(encoding="utf-8")
    assert raw.count("${test_hint}") == 4
    assert engines.ENGINES["unity"].tokens()["test_hint"] == ""


def _browser():
    from shams_tools import web_build
    return web_build.find_browser()


@pytest.mark.skipif(not _web_toolchain(), reason="needs the web toolchain (shams toolchain install web)")
def test_web_build_and_smoke_in_a_headless_browser(tmp_path):
    from shams_tools import web_build
    scene = tmp_path / "Slice1.ts"
    scene.write_text("""import * as THREE from 'three';
import { SPEED } from '../game/Core/Config';
export function start(container: HTMLElement): void {
  const renderer = new THREE.WebGLRenderer();
  renderer.setSize(640, 360);
  container.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x336699);
  const cam = new THREE.PerspectiveCamera(60, 16 / 9, 0.1, 100);
  cam.position.z = 3;
  const cube = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshNormalMaterial());
  scene.add(cube);
  const tick = () => { cube.rotation.y += SPEED; renderer.render(scene, cam); requestAnimationFrame(tick); };
  tick();
}
""", encoding="utf-8")
    cfg = tmp_path / "Config.ts"
    cfg.write_text("export const SPEED = 0.01;\n", encoding="utf-8")
    files = [("src/scenes/Slice1.ts", scene), ("src/game/Core/Config.ts", cfg)]
    r = web_build.build_web(files)
    try:
        assert r["ok"], r["log"]
        assert (r["dist"] / "index.html").exists() and r["entry"]["src/main.ts"].endswith("scenes/Slice1.ts")
        z = tmp_path / "game.zip"
        web_build.zip_dir(r["dist"], z)
        outs = [{"logical_id": "build.game", "media_type": "application/zip", "file": str(z), "role": "output"}]
        assert web_build.check_web_zip({}, outs)[0] == "PASS"
        if _browser():
            v, d = web_build.check_web_smoke({"check": {"seconds": 4}}, outs)
            assert v == "PASS", d
    finally:
        shutil.rmtree(r["work"], ignore_errors=True)
    broken = tmp_path / "Broken.ts"
    broken.write_text("export function start(el: HTMLElement) { throw new Error('boom at start'); }\n", encoding="utf-8")
    if _browser():
        v, d = web_build.check_web_smoke({"check": {"seconds": 3}}, [
            {"logical_id": "scene", "path": "src/scenes/Broken.ts", "media_type": "text/typescript",
             "file": str(broken), "role": "output"}])
        assert v == "FAIL" and "boom" in d, d

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
    assert main(["init", d, "--project-id", "notes", "--goal", "CLI Python", "--mock", "--mode", "autonomous"]) == 0
    assert main(["run", d]) == 2                       # no plan yet
    assert main(["plan", d]) == 0
    assert main(["run", d, "--defect", "task.core_module:" + _first_criterion(d, "task.core_module") + ":9"]) == 1
    assert main(["decide", d, "task.core_module", "accept"]) == 0
    assert main(["run", d]) == 0


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
        assert crit["ac.player_look"]["check"]["type"] == "vlm"
        assert any(o["media_type"] == "image/png" for o in t["task.player_model"]["outputs"])
        assert "Chỉ dựng MỘT mesh" in t["task.npc_model"]["description"]
        assert crit["ac.player_look"].get("required", True) is True
        assert any(c["id"] == "ac.npc_look" for c in t["task.npc_model"]["acceptance_criteria"])
        tx = t["task.environment_texture"]
        assert "tileable" in tx["description"] and any(c["id"] == "ac.texture_look" for c in tx["acceptance_criteria"])


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

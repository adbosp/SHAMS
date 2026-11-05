"""Multi-engine support (ADR-115): one engine-neutral plan, four engine profiles."""
import pytest

from studio import engines
from studio.mock.worker import MockWorker
from studio.orchestration.studio import Studio
from studio.planning.validator import instantiate, list_templates, validate_dag

ALL = ["unity", "unreal", "godot", "web"]


@pytest.mark.parametrize("engine", ALL)
def test_template_instantiates_valid_plan_for_each_engine(engine):
    p = engines.get(engine)
    d = instantiate("vertical_slice_3d", "game1", "goal", engine=engine)
    assert not [i for i in validate_dag(d) if i.level == "error"]
    assert d["template"] == f"vertical_slice_3d@{engine}"
    assert "${" not in str(d)
    code = [t for t in d["tasks"] if t["capability"].startswith("code.")]
    assert code and all(t["capability"] == p.code_capability for t in code)
    scopes = [s for t in code for s in t.get("workspace_scope", [])]
    assert scopes and all(s.startswith(p.roots["scripts"]) or s.startswith(p.roots["tests"])
                          or s.startswith(p.roots["scenes"]) for s in scopes)
    build = next(t for t in d["tasks"] if t["task_id"] == "task.build_game")
    assert build["capability"] == p.build_capability


def test_engines_produce_distinct_plans():
    caps = {e: {t["capability"] for t in instantiate("vertical_slice_3d", "game1", "x", engine=e)["tasks"]} for e in ALL}
    assert len({frozenset(c) for c in caps.values()}) == 4


def test_legacy_unity_template_alias_still_works():
    assert "unity_3d_vertical_slice" in list_templates()
    d = instantiate("unity_3d_vertical_slice", "game1", "x")
    assert d["template"] == "vertical_slice_3d@unity"


@pytest.mark.parametrize("engine", ALL)
def test_full_mock_run_for_each_engine(tmp_path, engine):
    d = tmp_path / engine
    Studio.init_project(d, "game1", "goal", mode="autonomous", mock_mode=True,
                        engine={"name": engine, "version": engines.get(engine).default_version})
    st = Studio(d, mock=MockWorker(d / "attempts"))
    st.load_plan(instantiate("vertical_slice_3d", "game1", "goal", engine=engine))
    s = st.run()
    assert s["done"] and s["counts"] == {"ACCEPTED": 11}
    assert st.project["engine"]["name"] == engine


@pytest.mark.parametrize("text,expected", [
    ("Game cá mập chạy trên trình duyệt bằng Three.js WebGPU", "web"),
    ("làm bằng UE5 nhé", "unreal"),
    ("Godot 4 platformer", "godot"),
    ("Unity racing game", "unity"),
    ("game cá mập 3D", None),
])
def test_detect_engine_from_chat(text, expected):
    assert engines.detect(text) == expected


def test_aliases_and_unknown_engine():
    assert engines.get("UE5").name == "unreal" and engines.get("three.js").name == "web"
    with pytest.raises(KeyError):
        engines.get("cryengine")


@pytest.mark.parametrize("engine,needle", [("unity", "-runTests"), ("unreal", "BuildCookRun"),
                                           ("godot", "--export-release"), ("web", "build")])
def test_adapter_commands(tmp_path, engine, needle):
    cmds = engines.EngineAdapter(engines.get(engine), tmp_path).commands()
    assert set(cmds) == {"compile", "test", "build", "capture"}
    assert any(needle in a for argv in cmds.values() for a in argv)


def test_set_engine_only_before_plan(tmp_path):
    d = tmp_path / "p"
    Studio.init_project(d, "game1", "goal", mode="autonomous", mock_mode=True)
    st = Studio(d, mock=MockWorker(d / "attempts"))
    st.set_engine("godot")
    assert st.project["engine"]["name"] == "godot"
    st.load_plan(instantiate("vertical_slice_3d", "game1", "goal", engine="godot"))
    with pytest.raises(ValueError):
        st.set_engine("web")


def test_cli_engine_flow(tmp_path, capsys):
    from apps.cli.main import main
    d = str(tmp_path / "w")
    assert main(["init", d, "--project-id", "web_game", "--goal", "g", "--engine", "threejs", "--mode", "autonomous",
                 "--mock"]) == 0
    assert main(["plan", d]) == 0
    assert main(["run", d]) == 0
    assert "web" in capsys.readouterr().out
    assert main(["init", str(tmp_path / "bad"), "--project-id", "bad_game", "--goal", "g", "--engine", "cryengine", "--mock"]) != 0

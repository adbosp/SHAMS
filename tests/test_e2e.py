"""End-to-end Phase 1a acceptance with the Mock Worker (docs/design/06 §5)."""
import pytest

from conftest import PID, dag, task_dict
from studio.mock.worker import MockDefect, MockWorker
from studio.orchestration.studio import Studio
from studio.orchestration.task_runner import SimulatedCrash
from studio.statemachine import S


def test_full_shark_slice_runs_to_done(make_studio):
    st = make_studio()
    s = st.run()
    assert s["done"], s["counts"]
    assert s["counts"] == {"ACCEPTED": 11}
    for t in st.db.tasks():
        h = st.db.handoff(t["task_id"])
        assert h and h["status"] == "READY_FOR_QC"
        for o in h["outputs"]:
            assert st.artifacts.get(o["sha256"])["state"] == "ACCEPTED"
    # events are the source of truth: replaying task events reproduces the projection
    last = {}
    for e in st.db.events():
        if e["subject_kind"] == "task" and e["to_state"]:
            last[e["subject_id"]] = e["to_state"]
    assert last == {t["task_id"]: t["state"] for t in st.db.tasks()}


def test_qc_fail_classify_repair_pass(make_studio):
    st = make_studio({"task.shark_movement": MockDefect("ac.movement_compiles")})
    assert st.run()["done"]
    [f] = st.db.failures("task.shark_movement")
    assert f["failure_class"] == "compile_error"
    assert f["repair_plan"]["strategy"] == "fix_in_place"
    assert st.db.task("task.shark_movement")["attempt"] == 1
    reps = st.db.qc_reports("task.shark_movement")
    assert [r["verdict"] for r in reps] == ["FAIL", "PASS"]
    assert reps[0]["stopped_at_stage"] == 2          # stopped before unit tests (ADR-106)
    skipped = [c for c in reps[0]["criteria"] if c["verdict"] == "SKIPPED"]
    assert {c["criterion_id"] for c in skipped} >= {"ac.movement_tests"}


def test_repeated_failure_goes_to_analysis_then_human_review(make_studio):
    st = make_studio({"task.prey_spawner": MockDefect("ac.spawner_tests", fail_attempts=99)})
    st.run()
    assert st.db.state("task.prey_spawner") == S.HUMAN_REVIEW
    fs = st.db.failures("task.prey_spawner")
    assert fs[0]["root_cause"]["by"] == "classifier"
    assert fs[1]["root_cause"]["by"] == "analysis_agent"          # same signature twice (ADR-107)
    assert fs[-1]["repair_plan"]["strategy"] == "human_review"
    # dependents never start while their input is not ACCEPTED
    assert st.db.state("task.eat_growth") == S.PENDING
    # a human accepts after review -> pipeline continues
    st.mock.defects.clear()
    st.human_decide("task.prey_spawner", "retry")
    assert st.run()["done"]


def test_worker_blocked_and_human_unblock(make_studio):
    st = make_studio({"task.sfx_eat": MockDefect("ac.sfx_valid", blocked=True)})
    st.run()
    assert st.db.state("task.sfx_eat") == S.BLOCKED
    st.mock.defects.clear()
    st.human_decide("task.sfx_eat", "unblock")
    assert st.run()["done"]


def test_crash_before_persist_resumes_without_reexecuting(make_studio, tmp_path):
    st = make_studio()
    st.crash_points.add("persist")
    with pytest.raises(SimulatedCrash):
        st.run()
    crashed = [t for t in st.db.tasks() if t["state"] in ("RUNNING", "LEASED")]
    assert len(crashed) == 1
    tid = crashed[0]["task_id"]
    calls_before = list(st.mock.calls)
    st.close()

    st2 = Studio(tmp_path / "proj", mock=MockWorker(tmp_path / "proj" / "attempts"))
    assert tid in st2.recover()
    assert all(c[0] != tid for c in st2.mock.calls), "execute must not run again after resume"
    assert st2.run()["done"]
    assert calls_before.count((tid, 0)) == 1
    assert any(e["type"] == "task.resumed" for e in st2.db.events())


def test_crash_after_release_runs_qc_on_recovery(make_studio, tmp_path):
    st = make_studio()
    st.crash_points.add("qc")
    with pytest.raises(SimulatedCrash):
        st.run()
    st.close()
    st2 = Studio(tmp_path / "proj", mock=MockWorker(tmp_path / "proj" / "attempts"))
    assert st2.run()["done"]


def test_upstream_change_marks_consumers_stale_then_revalidates(make_studio):
    st = make_studio()
    assert st.run()["done"]
    old = st.artifacts.accepted("code.shark_controller")["sha256"]
    rid = st.repair._create_upstream_repair("task.shark_movement",
                                            {"failure_id": "fail.manual", "summary": "design change"})
    assert st.run()["done"]
    new = st.artifacts.accepted("code.shark_controller")["sha256"]
    assert new != old and st.artifacts.get(old)["state"] == "SUPERSEDED"
    invalidated = {e["subject_id"] for e in st.db.events() if e["type"] == "task.invalidated"}
    assert invalidated == {"task.eat_growth", "task.integration_slice1"}
    revalidated = {e["subject_id"] for e in st.db.events() if e["type"] == "task.revalidate"}
    assert revalidated == invalidated
    # STALE tasks were re-QC'd, not re-executed (ADR-110)
    assert st.db.task("task.eat_growth")["attempt"] == 0
    assert any(i["sha256"] == new for i in st.db.handoff("task.eat_growth")["inputs"])
    assert st.db.state(rid) == S.ACCEPTED


class BlameUpstream:
    def analyze(self, task, failure, history):
        return {"explanation": "SharkController.Mouth missing", "upstream_task_id": "task.shark_movement"}


def test_fix_upstream_creates_repair_task(make_studio):
    st = make_studio({"task.eat_growth": MockDefect("ac.eat_tests", fail_attempts=2)}, analysis=BlameUpstream())
    assert st.run()["done"]
    fs = st.db.failures("task.eat_growth")
    assert fs[1]["repair_plan"]["strategy"] == "fix_upstream"
    assert st.db.state("task.shark_movement.repair1") == S.ACCEPTED
    assert "task.shark_movement.repair1" in st.db.task("task.eat_growth")["depends_on"]


def test_not_verified_guided_vs_autonomous(tmp_path):
    crit = [{"id": "ac.compiles", "description": "c", "kind": "compile"},
            {"id": "ac.looks_right", "description": "v", "kind": "visual"}]
    for mode, expected in (("guided", S.HUMAN_REVIEW), ("autonomous", S.ACCEPTED)):
        d = tmp_path / mode
        Studio.init_project(d, PID, "g", mode=mode, mock_mode=True)
        st = Studio(d)
        st.load_plan(dag([task_dict("task.ui", "image.ui_asset", crit=crit,
                                    outputs=[{"name": "ui", "logical_id": "out.ui", "media_type": "image/png"}])]),
                     approved_by="ropy")
        st.run()
        assert st.db.state("task.ui") == expected


def test_side_effect_ledger_is_idempotent(make_studio):
    st = make_studio()
    calls = []
    for _ in range(3):
        st.db.side_effect("publish:build:a0", "task.build_windows", 0, "publish", lambda: calls.append(1) or {"ok": 1})
    assert calls == [1]

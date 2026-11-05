import pytest

from studio.mock.worker import MockWorker
from studio.orchestration.studio import Studio
from studio.planning.validator import instantiate

PID = "shark_mvp"


@pytest.fixture
def make_studio(tmp_path):
    def _make(defects=None, mode="guided", plan=True, **kw):
        d = tmp_path / "proj"
        if not (d / "project.json").exists():
            Studio.init_project(d, PID, "Game cá mập 3D", mode=mode, mock_mode=True)
        st = Studio(d, mock=MockWorker(d / "attempts", defects=defects or {}), **kw)
        if plan and not st.db.tasks():
            st.load_plan(instantiate("unity_3d_vertical_slice", PID, "Game cá mập 3D"), approved_by="ropy")
        return st
    return _make


def task_dict(tid, cap, deps=(), inputs=(), outputs=None, crit=None, prio=50, scope=None):
    return {
        "schema_version": "1.0", "project_id": PID, "task_id": tid, "title": tid, "capability": cap,
        "depends_on": list(deps), "inputs": list(inputs),
        "outputs": outputs or [{"name": tid.split(".")[-1], "logical_id": f"out.{tid.split('.')[-1]}",
                                "media_type": "text/x-csharp"}],
        "acceptance_criteria": crit or [{"id": f"ac.{tid.split('.')[-1]}", "description": "compiles",
                                         "kind": "compile"}],
        "priority": prio, "workspace_scope": scope or [],
    }


def dag(tasks, plan_id="plan.custom"):
    return {"schema_version": "1.0", "project_id": PID, "plan_id": plan_id, "plan_version": 1,
            "goal": "custom", "tasks": tasks}

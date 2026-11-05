"""Unit tests: contracts, state machine, plan validation, nodes, scheduler affinity."""
import json
from pathlib import Path

import pytest

from conftest import PID, dag, task_dict
from studio import contracts, statemachine
from studio.nodes.registry import NodeRegistry, mock_registry
from studio.orchestration.studio import Studio
from studio.planning.validator import instantiate, validate_dag
from studio.scheduling.scheduler import SchedulerConfig
from studio.statemachine import S

DOCS = Path(__file__).resolve().parent.parent / "docs" / "design" / "examples"


def test_design_examples_validate():
    for name, schema in [("task.shark_movement.json", "task"), ("handoff.shark_movement.json", "handoff"),
                         ("node.controller_cpu_only.json", "node")]:
        assert contracts.errors(schema, json.loads((DOCS / name).read_text(encoding="utf-8"))) == []


def test_template_is_valid_plan():
    assert [i for i in validate_dag(instantiate("unity_3d_vertical_slice", PID, "g")) if i.level == "error"] == []


@pytest.mark.parametrize("frm,to,event,actor", [
    (S.RUNNING, S.ACCEPTED, "qc.passed", "qc"),                   # must go through QC_PENDING
    (S.QC_PENDING, S.ACCEPTED, "qc.passed", "task_runner"),       # worker cannot accept itself (ADR-011)
    (S.HUMAN_REVIEW, S.ACCEPTED, "human.decided", "scheduler"),   # only a human decides
    (S.FAILED, S.READY, "task.unblocked", "human"),               # terminal
])
def test_illegal_transitions(frm, to, event, actor):
    with pytest.raises(statemachine.TransitionError):
        statemachine.check(frm, to, event, actor)


def test_validator_catches_cycle_and_missing_producer():
    a = task_dict("task.a", "code.unity_csharp", deps=["task.b"])
    b = task_dict("task.b", "code.unity_csharp", deps=["task.a"],
                  inputs=[{"name": "x", "logical_id": "out.nowhere"}])
    msgs = [str(i) for i in validate_dag(dag([a, b]))]
    assert any("cycle" in m for m in msgs)
    assert any("no producer" in m for m in msgs)


def test_guided_mode_requires_plan_approval(make_studio):
    st = make_studio(plan=False)
    with pytest.raises(PermissionError):
        st.load_plan(instantiate("unity_3d_vertical_slice", PID, "g"))


def test_lanes_follow_nodes_cpu_only_controller():
    reg = NodeRegistry([{"node_id": "node.controller", "kind": "local", "cpu_slots": 2, "gpus": []}])
    assert [l.lane_id for l in reg.lanes()] == ["cpu:node.controller"]
    reg.add_node({"node_id": "node.box", "kind": "remote", "endpoint": "http://10.0.0.2", "cpu_slots": 0,
                  "gpus": [{"index": 0, "vram_mb": 24576}, {"index": 1, "vram_mb": 8192}]})
    assert {l.lane_id for l in reg.lanes()} == {"cpu:node.controller", "gpu:node.box:0", "gpu:node.box:1"}


def test_missing_capability_blocks_then_unblocks(tmp_path):
    d = tmp_path / "p"
    Studio.init_project(d, PID, "g", mode="autonomous")
    nodes = NodeRegistry([{"node_id": "node.controller", "kind": "local", "cpu_slots": 2, "gpus": []}])
    st = Studio(d, nodes=nodes)
    st.load_plan(dag([task_dict("task.code", "code.unity_csharp")]))
    st.run()
    assert st.db.state("task.code") == S.BLOCKED
    st.human_decide("task.code", "unblock")
    assert st.db.state("task.code") == S.READY


def _two_model_dag(n=4):
    # priorities interleave the two model families: code0=50, img0=49, code1=48, img1=47, ...
    tasks = []
    for i in range(n):
        tasks.append(task_dict(f"task.code{i}", "code.unity_csharp", prio=50 - 2 * i))
        tasks.append(task_dict(f"task.img{i}", "image.texture", prio=49 - 2 * i,
                               outputs=[{"name": f"img{i}", "logical_id": f"out.img{i}", "media_type": "image/png"}]))
    return dag(tasks)


@pytest.mark.parametrize("affinity,max_loads", [(30.0, 2)])
def test_affinity_scheduling_minimises_model_loads(tmp_path, affinity, max_loads):
    loads = {}
    for aff in (affinity, 0.0):
        d = tmp_path / f"p{aff}"
        Studio.init_project(d, PID, "g", mode="autonomous", mock_mode=True)
        st = Studio(d, scheduler_config=SchedulerConfig(affinity_bonus=aff, switch_cost_divisor=1e9))
        st.load_plan(_two_model_dag())
        st.run()
        assert st.status()["done"]
        loads[aff] = st.status()["model_loads"]
    assert loads[affinity] <= max_loads
    assert loads[0.0] == 8          # pure priority order alternates models on every task


def test_mock_registry_has_cpu_controller_and_virtual_gpu():
    reg = mock_registry({"code.unity_csharp", "build.unity"})
    kinds = {l.lane_id: l.kind for l in reg.lanes()}
    assert kinds == {"cpu:node.controller": "cpu", "gpu:node.mock_gpu:0": "gpu"}
    assert reg.candidates("build.unity")[0].lane_kind == "cpu"

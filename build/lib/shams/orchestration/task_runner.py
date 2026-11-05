"""Task Runner Graph (ADR-102): one LangGraph thread per task attempt.

start -> prepare_context -> ensure_loaded -> execute -> persist -> release -> qc -> END

LangGraph checkpoints after every node, so a process crash resumes at the next node without
re-running completed ones (e.g. a model call already done in `execute` is not repeated).
"""
from __future__ import annotations

import operator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from shams import contracts
from shams.statemachine import S

if TYPE_CHECKING:
    from shams.orchestration.project import Studio


class SimulatedCrash(BaseException):
    """Raised by test hooks to emulate the controller process dying (not caught as Exception)."""


class RunState(TypedDict, total=False):
    task_id: str
    attempt: int
    lease_id: str
    lane_id: str
    worker_id: str
    context: dict
    files: list[dict]
    handoff: dict
    outcome: str          # ok | blocked | crashed | worker_failed | oom
    error: str
    trace: Annotated[list[str], operator.add]


def build_graph(studio: "Studio", checkpointer):
    db = studio.db

    def hook(name: str) -> None:
        if name in studio.crash_points:
            studio.crash_points.discard(name)
            raise SimulatedCrash(f"simulated crash before node {name}")

    def start(s: RunState) -> dict:
        hook("start")
        if db.state(s["task_id"]) == S.LEASED:
            db.transition(s["task_id"], S.RUNNING, "task.started", "task_runner",
                          {"lease_id": s["lease_id"], "worker_id": s["worker_id"]})
        return {"trace": ["start"], "outcome": "ok"}

    def prepare_context(s: RunState) -> dict:
        hook("prepare_context")
        try:
            ctx = studio.context.compile(s["task_id"], tools_allowed=studio.tools_for(s["worker_id"]))
        except LookupError as e:
            return {"trace": ["prepare_context"], "outcome": "worker_failed", "error": f"missing output: {e}"}
        return {"trace": ["prepare_context"], "context": ctx}

    def ensure_loaded(s: RunState) -> dict:
        hook("ensure_loaded")
        if s.get("outcome") != "ok":
            return {"trace": ["ensure_loaded:skip"]}
        try:
            studio.lifecycle.ensure_loaded(studio.lane(s["lane_id"]), studio.nodes.workers[s["worker_id"]])
        except MemoryError as e:
            return {"trace": ["ensure_loaded"], "outcome": "oom", "error": f"out of memory: {e}"}
        return {"trace": ["ensure_loaded"]}

    def execute(s: RunState) -> dict:
        hook("execute")
        if s.get("outcome") != "ok":
            return {"trace": ["execute:skip"]}
        worker = studio.nodes.workers[s["worker_id"]]
        impl = studio.worker_impl(worker)
        try:
            files, handoff = impl.run(s["context"], worker.ref)
        except Exception as e:  # worker crash is a task failure, not a controller crash
            return {"trace": ["execute"], "outcome": "crashed", "error": f"{type(e).__name__}: {e}"}
        out = {"trace": ["execute"], "files": files, "handoff": handoff,
               "outcome": "blocked" if handoff["status"] == "BLOCKED"
               else "worker_failed" if handoff["status"] == "FAILED" else "ok"}
        if handoff["status"] == "FAILED":
            out["error"] = handoff.get("blocked_reason") or "; ".join(
                i["issue"] for i in handoff.get("known_issues", [])) or "worker reported FAILED"
        return out

    def persist(s: RunState) -> dict:
        hook("persist")
        tid, attempt = s["task_id"], s["attempt"]
        if db.state(tid) != S.RUNNING:          # already persisted before a crash
            return {"trace": ["persist:skip"]}
        outcome = s.get("outcome", "ok")
        if outcome in {"crashed", "oom", "worker_failed"}:
            studio.record_failure(tid, attempt, s.get("error", outcome),
                                  source={"crashed": "crash", "oom": "crash", "worker_failed": "worker_failed"}[outcome],
                                  event="task.crashed" if outcome != "worker_failed" else "task.worker_failed")
            return {"trace": ["persist:failure"]}
        handoff = dict(s["handoff"])
        worker = studio.nodes.workers[s["worker_id"]]
        refs = [studio.artifacts.put(
                    Path(f["abs"]), logical_id=f["logical_id"], media_type=f["media_type"],
                    contract=f.get("contract"), task_id=tid, attempt=attempt, worker=worker.ref,
                    rel_path=f["rel"], derived_from=handoff["inputs"])
                for f in s.get("files", [])]
        handoff["outputs"] = refs
        handoff["consumers"] = db.dependents(tid)
        handoff["timestamp"] = contracts.now_iso()
        with db.tx():
            db.save_handoff(handoff)        # persist BEFORE releasing the GPU (principle 4)
            if outcome == "blocked":
                db.transition(tid, S.BLOCKED, "task.blocked", "task_runner",
                              {"reason": handoff.get("blocked_reason", "")})
            else:
                db.transition(tid, S.QC_PENDING, "task.submitted", "task_runner", {"attempt": attempt})
        for k in ("tokens_in", "tokens_out", "wall_seconds"):
            if k in handoff.get("usage", {}):
                db.metric(tid, attempt, k, float(handoff["usage"][k]))
        return {"trace": ["persist"], "handoff": handoff}

    def release(s: RunState) -> dict:
        hook("release")
        studio.release(s["lease_id"], s["lane_id"], s["worker_id"])
        return {"trace": ["release"]}

    def qc(s: RunState) -> dict:
        hook("qc")
        if db.state(s["task_id"]) == S.QC_PENDING:
            studio.run_qc(s["task_id"])
        return {"trace": ["qc"]}

    g = StateGraph(RunState)
    for name, fn in [("start", start), ("prepare_context", prepare_context), ("ensure_loaded", ensure_loaded),
                     ("execute", execute), ("persist", persist), ("release", release), ("qc", qc)]:
        g.add_node(name, fn)
    g.add_edge(START, "start")
    for a, b in [("start", "prepare_context"), ("prepare_context", "ensure_loaded"), ("ensure_loaded", "execute"),
                 ("execute", "persist"), ("persist", "release"), ("release", "qc"), ("qc", END)]:
        g.add_edge(a, b)
    return g.compile(checkpointer=checkpointer)


def thread_id(task_id: str, attempt: int) -> str:
    return f"{task_id}#a{attempt}"


def config(task_id: str, attempt: int) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id(task_id, attempt)}}

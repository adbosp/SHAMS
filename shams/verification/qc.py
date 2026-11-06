"""Independent QC, 5 stages cheap-first with early stop (ADR-106, docs/design/04 §2)."""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Callable, Protocol

from shams import contracts
from shams.artifacts.registry import ArtifactRegistry
from shams.events.store import EventStore

STAGE = {"exists": 1, "schema": 1, "compile": 2, "unit_test": 3, "integration_test": 3,
         "runtime_log": 4, "screenshot": 4, "metric": 4, "semantic": 5, "visual": 5, "human": 5}
QC_ACTOR = {"worker_id": "qc.runner", "kind": "tool"}


class Checker(Protocol):
    def __call__(self, kind: str, criterion: dict, contents: list[str]) -> tuple[str, str]: ...


_CHECKERS: dict[str, Checker] = {}


def register_checker(kind: str, fn: Checker) -> None:
    """Plug a real checker for one criterion kind (compile, unit_test, metric, ...) — ADR-116.

    A checker receives (kind, criterion, output_contents) and returns (verdict, detail) with verdict
    PASS | FAIL | NOT_VERIFIED. Kinds without a checker end as NOT_VERIFIED (ADR-010)."""
    if kind not in STAGE:
        raise ValueError(f"unknown criterion kind {kind!r}; one of {sorted(STAGE)}")
    _CHECKERS[kind] = fn


def registered_checkers() -> dict[str, Checker]:
    from shams import plugins
    out = {}
    for name, ep in plugins.entry_points("shams.checkers").items():
        if name in STAGE:
            out[name] = ep.load()
    out.update(_CHECKERS)
    return out


def gate_for(task: dict) -> str:
    cap = task["capability"]
    if cap.startswith("build."):
        return "final_qc"
    if cap.startswith("integration"):
        return "integration_qc"
    return "task_qc"


class QCRunner:
    def __init__(self, store: EventStore, registry: ArtifactRegistry, checkers: dict[str, Checker]):
        self.db = store
        self.reg = registry
        self.checkers = checkers

    def _stage1(self, task: dict, handoff: dict) -> tuple[str, str]:
        if contracts.errors("handoff", handoff):
            return "FAIL", "schema violation: " + "; ".join(contracts.errors("handoff", handoff))
        declared = {o["logical_id"] for o in task["outputs"]}
        produced = {o["logical_id"] for o in handoff["outputs"]}
        if declared - produced:
            return "FAIL", f"missing output: {sorted(declared - produced)}"
        for o in handoff["outputs"]:
            blob = self.reg.blob_path(o["sha256"])
            if not blob.exists() or contracts.sha256_file(blob) != o["sha256"]:
                return "FAIL", f"missing output or hash mismatch: {o['logical_id']}"
            scope = task.get("workspace_scope")
            if scope and o.get("path") and not any(fnmatch.fnmatch(o["path"], g) for g in scope):
                return "FAIL", f"schema violation: {o['path']} outside workspace_scope"
        return "PASS", "outputs exist, hashes match, handoff valid"

    def run(self, task: dict, handoff: dict) -> dict:
        contents = [Path(self.reg.blob_path(o["sha256"])).read_text(encoding="utf-8", errors="replace")
                    for o in handoff["outputs"] if self.reg.blob_path(o["sha256"]).exists()]
        outputs = [{"logical_id": o["logical_id"], "path": o.get("path"), "media_type": o.get("media_type"),
                    "file": str(self.reg.blob_path(o["sha256"])), "role": role}
                   for role, refs in (("output", handoff["outputs"]), ("input", handoff.get("inputs") or []))
                   for o in refs if o.get("sha256") and self.reg.blob_path(o["sha256"]).exists()]
        criteria = sorted(task["acceptance_criteria"], key=lambda c: STAGE[c["kind"]])
        results, stopped, failed = [], None, False
        v1, d1 = self._stage1(task, handoff)
        results.append({"criterion_id": "builtin.stage1", "verdict": v1, "stage": 1, "checker": QC_ACTOR,
                        "detail": d1})
        if v1 == "FAIL":
            failed, stopped = True, 1
        for c in criteria:
            stage = STAGE[c["kind"]]
            if failed:
                results.append({"criterion_id": c["id"], "verdict": "SKIPPED", "stage": stage,
                                "detail": f"stopped at stage {stopped}"})
                continue
            checker = self.checkers.get(c["kind"])
            if checker is None:
                verdict = "NOT_VERIFIED" if c.get("required", True) else "SKIPPED"
                detail = f"no checker for kind '{c['kind']}'"
            else:
                verdict, detail = (checker(c["kind"], c, contents, outputs=outputs)
                                   if getattr(checker, "wants_outputs", False) else checker(c["kind"], c, contents))
            results.append({"criterion_id": c["id"], "verdict": verdict, "stage": stage, "checker": QC_ACTOR,
                            "detail": detail, "_required": c.get("required", True)})
            if verdict == "FAIL" and c.get("required", True):
                failed, stopped = True, stage
        req = [r for r in results if r.get("_required", True)]
        if any(r["verdict"] == "FAIL" for r in req):
            verdict = "FAIL"
        elif any(r["verdict"] == "NOT_VERIFIED" for r in req):
            verdict = "NOT_VERIFIED"
        else:
            verdict = "PASS"
        for r in results:
            r.pop("_required", None)
        rep = {"schema_version": contracts.SCHEMA_VERSION, "project_id": self.db.project_id,
               "task_id": task["task_id"], "attempt": handoff["attempt"], "gate": gate_for(task),
               "verdict": verdict, "criteria": results, "regression": {"ran": False},
               "timestamp": contracts.now_iso()}
        if stopped:
            rep["stopped_at_stage"] = stopped
        return rep

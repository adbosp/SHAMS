"""Mock Worker (ADR-114): runs the full lifecycle without any AI model attached.

Writes deterministic files for every declared output and returns a valid handoff draft.
Defects can be injected per task to exercise QC FAIL -> classify -> repair -> PASS.
"""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from pathlib import Path

from studio import contracts

EXT = {"text/x-csharp": ".cs", "image/png": ".png", "model/gltf-binary": ".glb", "model/fbx": ".fbx",
       "audio/wav": ".wav", "application/json": ".json", "text/markdown": ".md",
       "application/vnd.unity.scene": ".unity", "application/vnd.unity.prefab": ".prefab",
       "application/x-msdownload": ".exe"}

DEFECT_LOGS = {
    "compile": "Assets/Scripts/X.cs(42,13): error CS0103: The name 'swimSpeed' does not exist in the current context",
    "unit_test": "Test failed: {criterion} expected 1.0 but was 0.0",
    "integration_test": "Test failed: {criterion} integration contract mismatch",
    "runtime_log": "NullReferenceException: Object reference not set to an instance of an object",
    "metric": "metric below threshold: fps=12 (required >= 30)",
    "exists": "missing output",
}


@dataclass
class MockDefect:
    criterion_id: str
    fail_attempts: int = 1           # attempts 0..fail_attempts-1 carry the defect
    blocked: bool = False            # worker reports BLOCKED instead
    upstream_task_id: str | None = None  # analysis will blame this upstream task (fix_upstream)


@dataclass
class MockWorker:
    attempts_root: Path
    defects: dict[str, MockDefect] = field(default_factory=dict)
    calls: list[tuple[str, int]] = field(default_factory=list)
    delay_s: float = 0.0             # simulated work time (UI demos)

    @staticmethod
    def rel_path(task: dict, out: dict) -> str:
        name = out["name"] + EXT.get(out["media_type"], ".bin")
        for glob in task.get("workspace_scope", []):
            base = glob.split("*")[0].rstrip("/")
            cand = f"{base}/{name}" if base else name
            if fnmatch.fnmatch(cand, glob):
                return cand
        return f"Generated/{task['task_id']}/{name}"

    def run(self, ctx: dict, worker_ref: dict) -> tuple[list[dict], dict]:
        task, attempt = ctx["task"], ctx["attempt"]
        self.calls.append((task["task_id"], attempt))
        if self.delay_s:
            time.sleep(self.delay_s)
        d = self.defects.get(task["task_id"])
        defective = d is not None and attempt < d.fail_attempts
        adir = self.attempts_root / task["task_id"] / f"a{attempt}"
        files = []
        for out in task["outputs"]:
            rel = self.rel_path(task, out)
            p = adir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            body = [f"// SHAMS mock output", f"// task={task['task_id']} output={out['logical_id']}",
                    "// inputs=" + ",".join(sorted(i["sha256"][:12] for i in ctx["inputs"]))]
            if defective and not d.blocked:
                body.append(f"// MOCK_DEFECT:{d.criterion_id}")
            if ctx["prior_failures"]:
                body.append(f"// repaired_after={len(ctx['prior_failures'])}")
            p.write_text("\n".join(body) + "\n", encoding="utf-8")
            files.append({"abs": str(p), "rel": rel, **out})
        status = "BLOCKED" if (defective and d.blocked) else "READY_FOR_QC"
        handoff = {
            "schema_version": contracts.SCHEMA_VERSION, "project_id": ctx["project_id"],
            "task_id": task["task_id"], "attempt": attempt, "producer": worker_ref, "status": status,
            "inputs": ctx["inputs"], "outputs": [],
            "decisions": [{"decision": f"mock implementation of {task['title']}", "reason": "mock mode"}],
            "known_issues": [], "self_checks": [], "logs": [],
            "next_actions": [], "context_summary": f"[mock] {task['title']}: "
                                                   f"{', '.join(o['logical_id'] for o in task['outputs'])}",
            "usage": {"tokens_in": ctx["token_budget"]["estimated"], "tokens_out": 0, "wall_seconds": 0.0},
            "timestamp": contracts.now_iso(),
        }
        if status == "BLOCKED":
            handoff["blocked_reason"] = "mock: tool unavailable"
        return files, handoff


def mock_check(kind: str, criterion: dict, contents: list[str]) -> tuple[str, str]:
    """Mock checker for deterministic criteria: FAIL iff a defect marker targets this criterion."""
    marker = f"MOCK_DEFECT:{criterion['id']}"
    if any(marker in c for c in contents):
        return "FAIL", DEFECT_LOGS.get(kind, "mock failure").format(criterion=criterion["id"])
    return "PASS", f"mock {kind} ok"

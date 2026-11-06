"""Failure Classifier (rules first, ADR-107) and Repair Router (escalation ladder, 04 §3)."""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Protocol

from shams import contracts
from shams.events.store import EventStore
from shams.nodes.registry import NodeRegistry
from shams.statemachine import S

RULES: list[tuple[str, str, str]] = [
    # regex, failure_class, default strategy
    (r"(?i)\berror [A-Z]{1,4}\d{3,5}\b|compil(e|ation) error|syntax ?error|undefined (name|reference|symbol)",
     "compile_error", "fix_in_place"),
    (r"(?i)out of memory|CUDA error: out of memory|OOM", "resource_oom", "retry_same"),
    (r"(?i)GLB invalid|image invalid|image is blank|no UV|no base colour texture|vertices|scale\?|render evidence",
     "asset_validation", "fix_in_place"),
    (r"(?i)visual check failed|does not match the description", "visual_mismatch", "fix_in_place"),
    (r"(?i)tool unavailable|is not installed|model file .* not found|not usable|is not running|cannot reach|"
     r"program not found",
     "tool_unavailable", "human_review"),
    (r"(?i)HTTP \d{3} from|provider error|ollama:", "provider_error", "retry_same"),
    (r"(?i)test(s)? failed", "test_failure", "fix_in_place"),
    (r"\w+Exception", "runtime_exception", "fix_in_place"),
    (r"missing output|hash mismatch", "missing_output", "retry_with_context"),
    (r"schema violation", "schema_violation", "retry_with_context"),
    (r"integration contract", "integration_contract", "fix_in_place"),
    (r"metric below threshold", "performance", "fix_in_place"),
    (r"tool unavailable|no worker", "tool_unavailable", "human_review"),
]
SEVERITY = {"compile_error": "high", "missing_output": "high", "schema_violation": "medium",
            "test_failure": "medium", "runtime_exception": "high", "resource_oom": "medium",
            "tool_unavailable": "high", "asset_validation": "medium", "visual_mismatch": "medium",
            "provider_error": "medium", "unknown": "medium"}


def normalize(text: str) -> str:
    t = re.sub(r"[A-Za-z]:\\[^\s:]+|/[^\s:]+", "<path>", text)
    t = re.sub(r"\(\d+,\d+\)", "(L)", t)
    t = re.sub(r"\b\d+(\.\d+)?\b", "N", t)
    return t.strip()


class FailureClassifier:
    def __init__(self, extra_rules: list[tuple[str, str, str]] | None = None):
        """extra_rules come from the project's Domain Pack and are checked first (ADR-117)."""
        self.rules = list(extra_rules or []) + RULES

    def classify(self, task: dict, attempt: int, detail: str, source: str, evidence: list[dict]) -> dict:
        fclass, strategy = "unknown", None
        for rx, fc, st in self.rules:
            if re.search(rx, detail):
                fclass, strategy = fc, st
                break
        sig = hashlib.sha256(f"{fclass}|{normalize(detail)}".encode()).hexdigest()[:16]
        rec = {
            "schema_version": contracts.SCHEMA_VERSION, "project_id": task["project_id"],
            "failure_id": f"fail.{uuid.uuid4().hex[:12]}", "task_id": task["task_id"], "attempt": attempt,
            "source": source, "failure_class": fclass, "signature": sig,
            "severity": SEVERITY.get(fclass, "medium"), "summary": detail[:2000],      # the repair prompt needs the real error
            "evidence": evidence, "timestamp": contracts.now_iso(),
        }
        if strategy:
            rec["root_cause"] = {"by": "classifier", "explanation": f"rule matched: {fclass}"}
            rec["repair_plan"] = {"strategy": strategy, "target_task_id": task["task_id"],
                                  "notes": f"route by classifier ({fclass})"}
        return rec


class AnalysisAgent(Protocol):
    def analyze(self, task: dict, failure: dict, history: list[dict]) -> dict:
        """Return {'explanation': str, 'upstream_task_id': str|None}."""


class NullAnalysis:
    """Phase 1a stand-in: no LLM attached. Real Analysis Agent (Deep Agents) plugs in at Phase 1b."""

    def analyze(self, task: dict, failure: dict, history: list[dict]) -> dict:
        return {"explanation": f"repeated {failure['failure_class']} ({len(history)}x); retry with full failure "
                               f"history in context", "upstream_task_id": None}


@dataclass
class RepairPolicy:
    allow_external: bool = False
    max_repairs_factor: float = 2.0    # total repairs per plan <= factor * initial tasks


class RepairRouter:
    def __init__(self, store: EventStore, nodes: NodeRegistry, analysis: AnalysisAgent | None = None,
                 policy: RepairPolicy | None = None, initial_task_count: int = 0):
        self.db = store
        self.nodes = nodes
        self.analysis = analysis or NullAnalysis()
        self.policy = policy or RepairPolicy()
        self.initial = initial_task_count

    def _repairs_so_far(self) -> int:
        return self.db.q1("SELECT COUNT(*) FROM events WHERE type='repair.scheduled'")[0]

    def route(self, failure: dict) -> dict:
        """Decide and apply a repair plan for a task in REPAIR_REQUIRED. Returns the final failure record."""
        task = self.db.task(failure["task_id"])
        history = [f for f in self.db.failures(task["task_id"]) if f["failure_id"] != failure["failure_id"]]
        n_fail = len(history) + 1
        same_sig = sum(1 for f in history if f["signature"] == failure["signature"]) + 1
        plan = failure.get("repair_plan") or {"strategy": "retry_with_context"}

        if failure["failure_class"] == "tool_unavailable":
            plan = {"strategy": "human_review", "notes": "no capable worker/tool"}
        elif failure["failure_class"] == "resource_oom":
            plan = ({"strategy": "human_review", "notes": f"OOM repeated {same_sig}x"} if same_sig >= 3
                    else {"strategy": "retry_same", "notes": "OOM is not counted as an attempt"})
        elif failure["failure_class"] == "unknown" or same_sig >= 2:
            a = self.analysis.analyze(task, failure, history)
            failure["root_cause"] = {"by": "analysis_agent", "explanation": a["explanation"]}
            if a.get("upstream_task_id"):
                failure["root_cause"]["upstream_task_id"] = a["upstream_task_id"]
                plan = {"strategy": "fix_upstream", "target_task_id": a["upstream_task_id"],
                        "notes": a["explanation"]}
            else:
                plan = {"strategy": "retry_with_context", "notes": a["explanation"]}
        if n_fail >= 3 and plan["strategy"] in {"fix_in_place", "retry_with_context"}:
            ranked = self.nodes.ranked(task["capability"])
            current = task.get("provider_hint") or (ranked[0].worker_id if ranked else None)
            tried = {current, ranked[0].worker_id} if ranked else {current}     # specialised one ran first
            alt = [w for w in ranked if w.worker_id not in tried]
            if alt:                     # next best fit, e.g. the general coder after the specialised one
                plan = {"strategy": "switch_worker", "notes": f"try {alt[0].worker_id}"}
                extra = int(getattr(self.policy, "switch_worker_attempts", 2))
                task["max_attempts"] = max(task.get("max_attempts", 3), task["attempt"] + 1 + extra)
                self.db.update_task_json(task["task_id"], {"provider_hint": alt[0].worker_id,
                                                           "max_attempts": task["max_attempts"]})
            elif n_fail >= 4 and self.policy.allow_external:
                plan = {"strategy": "escalate_external", "notes": "local workers exhausted"}
        failure["repair_plan"] = {"target_task_id": task["task_id"], **plan}

        cap = self.policy.max_repairs_factor * max(1, self.initial)
        counts = plan["strategy"] != "retry_same"
        exhausted = (counts and task["attempt"] + 1 >= task.get("max_attempts", 3)) or self._repairs_so_far() >= cap
        if plan["strategy"] == "human_review" or exhausted:
            if exhausted and plan["strategy"] != "human_review":
                failure["repair_plan"] = {"strategy": "human_review", "target_task_id": task["task_id"],
                                          "notes": f"exhausted after {n_fail} failure(s): {plan['strategy']}"}
            self.db.save_failure(failure)
            self.db.transition(task["task_id"], S.HUMAN_REVIEW, "repair.exhausted", "repair_router",
                               {"failure_id": failure["failure_id"]})
            return failure

        self.db.save_failure(failure)
        if plan["strategy"] == "fix_upstream":
            rid = self._create_upstream_repair(plan["target_task_id"], failure)
            self.db.update_task_json(task["task_id"], {"depends_on": sorted(set(task["depends_on"]) | {rid})})
            with self.db.tx() as c:
                c.execute("INSERT OR IGNORE INTO task_deps VALUES (?,?)", (task["task_id"], rid))
                self.db.transition(task["task_id"], S.PENDING, "repair.scheduled", "repair_router",
                                   {"failure_id": failure["failure_id"], "strategy": "fix_upstream",
                                    "repair_task": rid}, attempt_delta=1)
            return failure
        self.db.transition(task["task_id"], S.READY, "repair.scheduled", "repair_router",
                           {"failure_id": failure["failure_id"], "strategy": plan["strategy"]},
                           attempt_delta=0 if not counts else 1)
        return failure

    def _create_upstream_repair(self, upstream_id: str, failure: dict) -> str:
        up = self.db.task(upstream_id)
        rid = f"{upstream_id}.repair{sum(1 for t in self.db.tasks() if t['task_id'].startswith(upstream_id + '.repair')) + 1}"
        t = {k: v for k, v in up.items() if k not in {"state", "attempt"}}
        t.update(task_id=rid, title=f"Repair: {up['title']}", created_by="repair_router",
                 parent_task_id=upstream_id, attempt=0,
                 description=f"Fix upstream cause of {failure['failure_id']}: {failure['summary'][:200]}")
        self.db.create_task(t, plan_id=self.db.task_row(upstream_id)["plan_id"], actor="repair_router")
        return rid

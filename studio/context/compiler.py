"""Context Compiler: deterministic Context Package per attempt (docs/design/05 §2, 01 §11).

Workers never receive previous conversations, only this package.
"""
from __future__ import annotations

from studio import contracts
from studio.artifacts.registry import ArtifactRegistry
from studio.events.store import EventStore

CHARS_PER_TOKEN = 4


class ContextCompiler:
    def __init__(self, store: EventStore, registry: ArtifactRegistry, goal: str,
                 engine: dict | None = None, default_token_limit: int = 32000):
        self.db = store
        self.reg = registry
        self.goal = goal
        self.engine = engine
        self.default_limit = default_token_limit

    def compile(self, task_id: str, tools_allowed: list[str], token_limit: int | None = None) -> dict:
        task = self.db.task(task_id)
        inputs, missing = [], []
        for inp in task.get("inputs", []):
            ref = self.reg.accepted(inp["logical_id"])
            if ref:
                inputs.append(ref)
            elif inp.get("required", True):
                missing.append(inp["logical_id"])
        if missing:
            raise LookupError(f"{task_id}: required inputs not ACCEPTED: {missing}")
        upstream = []
        for dep in task.get("depends_on", []):
            h = self.db.handoff(dep)
            if h:
                upstream.append({"task_id": dep, "context_summary": h["context_summary"],
                                 "decisions": [d["decision"] for d in h.get("decisions", [])]})
        known = [i["issue"] for d in task.get("depends_on", []) for i in (self.db.handoff(d) or {}).get("known_issues", [])]
        prior = [{"failure_id": f["failure_id"], "summary": f["summary"],
                  "tried_fix": (f.get("repair_plan") or {}).get("notes", "")}
                 for f in self.db.failures(task_id)]
        limit = token_limit or task.get("budget", {}).get("max_tokens") or self.default_limit
        pkg = {
            "schema_version": contracts.SCHEMA_VERSION, "project_id": self.db.project_id,
            "task_id": task_id, "attempt": task["attempt"], "goal": self.goal, "task": task,
            "inputs": inputs, "upstream_summaries": upstream,
            "relevant_files": [{"path": i["path"], "sha256": i["sha256"], "inclusion": "path_only",
                                "reason": f"input {i['logical_id']}"} for i in inputs if "path" in i],
            "known_issues": known, "prior_failures": prior, "tools_allowed": tools_allowed,
            "workspace_scope": task.get("workspace_scope", []),
            "output_contract": "Kết thúc bằng handoff hợp lệ theo handoff.schema.json v1.0",
            "token_budget": {"limit": int(limit), "estimated": 0},
        }
        if self.engine:
            pkg["engine"] = self.engine
        pkg["token_budget"]["estimated"] = len(str(pkg)) // CHARS_PER_TOKEN
        # Budget rule (04 §4): package <= 60% of the limit. Drop least important parts first.
        cap = int(limit * 0.6)
        for key in ("known_issues", "upstream_summaries"):
            if pkg["token_budget"]["estimated"] <= cap:
                break
            pkg[key] = pkg[key][-3:] if key == "upstream_summaries" else []
            pkg["token_budget"]["estimated"] = len(str(pkg)) // CHARS_PER_TOKEN
        return contracts.validate("context_package", pkg)

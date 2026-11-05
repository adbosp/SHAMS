"""Plan Validator + DAG templates (ADR-108)."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from studio import contracts
from studio.nodes.registry import NodeRegistry

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


@dataclass
class Issue:
    level: str   # error | warning
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.message}"


def validate_dag(dag: dict, nodes: NodeRegistry | None = None) -> list[Issue]:
    issues = [Issue("error", e) for e in contracts.errors("task_dag", dag)]
    if issues:
        return issues
    tasks = {t["task_id"]: t for t in dag["tasks"]}
    if len(tasks) != len(dag["tasks"]):
        issues.append(Issue("error", "duplicate task_id"))
    producers: dict[str, str] = {}
    for t in dag["tasks"]:
        for o in t["outputs"]:
            if o["logical_id"] in producers:
                issues.append(Issue("error", f"{o['logical_id']} produced by both {producers[o['logical_id']]} "
                                             f"and {t['task_id']}"))
            producers[o["logical_id"]] = t["task_id"]
        if t["project_id"] != dag["project_id"]:
            issues.append(Issue("error", f"{t['task_id']}: project_id mismatch"))
    for t in dag["tasks"]:
        for d in t["depends_on"]:
            if d not in tasks:
                issues.append(Issue("error", f"{t['task_id']}: unknown dependency {d}"))
        for i in t.get("inputs", []):
            p = producers.get(i["logical_id"])
            if p is None:
                issues.append(Issue("error", f"{t['task_id']}: input {i['logical_id']} has no producer"))
            elif p not in t["depends_on"]:
                issues.append(Issue("error", f"{t['task_id']}: input {i['logical_id']} comes from {p}, "
                                             f"which is not in depends_on"))
        if nodes is not None and not nodes.candidates(t["capability"]):
            issues.append(Issue("warning", f"{t['task_id']}: no worker for capability {t['capability']} "
                                           f"(task will be BLOCKED until a node provides it)"))
    # cycle detection (Kahn)
    indeg = {k: 0 for k in tasks}
    for t in tasks.values():
        for d in t["depends_on"]:
            if d in tasks:
                indeg[t["task_id"]] += 1
    queue = [k for k, v in indeg.items() if v == 0]
    seen = 0
    while queue:
        k = queue.pop()
        seen += 1
        for t in tasks.values():
            if k in t["depends_on"]:
                indeg[t["task_id"]] -= 1
                if indeg[t["task_id"]] == 0:
                    queue.append(t["task_id"])
    if seen != len(tasks):
        issues.append(Issue("error", "dependency cycle detected"))
    return issues


def list_templates() -> list[str]:
    return sorted(p.stem for p in TEMPLATE_DIR.glob("*.json"))


def instantiate(template: str, project_id: str, goal: str, plan_id: str | None = None) -> dict:
    raw = json.loads((TEMPLATE_DIR / f"{template}.json").read_text(encoding="utf-8"))
    dag = copy.deepcopy(raw)
    dag["project_id"] = project_id
    dag["plan_id"] = plan_id or f"plan.{project_id}"
    dag["goal"] = goal or raw.get("goal", "")
    dag["template"] = template
    dag["created_at"] = contracts.now_iso()
    for t in dag["tasks"]:
        t["project_id"] = project_id
        t.setdefault("schema_version", contracts.SCHEMA_VERSION)
        t.setdefault("created_by", "template")
    return dag

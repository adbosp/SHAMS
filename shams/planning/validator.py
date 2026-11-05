"""Plan Validator + DAG templates (ADR-108)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from shams import contracts
from shams.nodes.registry import NodeRegistry

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


TEMPLATE_ALIASES = {"unity_3d_vertical_slice": ("vertical_slice_3d", "unity")}


def template_dirs(project_dir: Path | str | None = None) -> list[Path]:
    """Project templates/ first, then $SHAMS_HOME/templates, then entry points, then built-ins (ADR-116)."""
    from shams import plugins
    dirs = plugins.search_dirs("templates", project_dir)
    for ep in plugins.entry_points("shams.templates").values():
        p = Path(str(ep.load()))
        if p.is_dir():
            dirs.append(p)
    return dirs + [TEMPLATE_DIR]


def list_templates(project_dir: Path | str | None = None) -> list[str]:
    names = {p.stem for d in template_dirs(project_dir) for p in d.glob("*.json")}
    return sorted(names) + sorted(TEMPLATE_ALIASES)


def find_template(name: str, project_dir: Path | str | None = None) -> Path:
    p = Path(name)
    if p.suffix == ".json" and p.exists():
        return p
    for d in template_dirs(project_dir):
        if (d / f"{name}.json").exists():
            return d / f"{name}.json"
    raise KeyError(f"template {name!r} not found; available: {list_templates(project_dir)}")


def instantiate(template: str, project_id: str, goal: str, plan_id: str | None = None,
                engine: str | None = None, project_dir: Path | str | None = None) -> dict:
    """Instantiate an engine-neutral template for one engine profile (ADR-115).

    `template` is a name from the search path or a path to a .json file."""
    from shams import engines

    if template in TEMPLATE_ALIASES:
        template, default_engine = TEMPLATE_ALIASES[template]
        engine = engine or default_engine
    profile = engines.get(engine or "unity")
    path = find_template(template, project_dir)
    template = path.stem
    text = path.read_text(encoding="utf-8")
    tokens = profile.tokens()
    missing = sorted(set(re.findall(r"\$\{(\w+)\}", text)) - set(tokens))
    if missing:
        raise KeyError(f"template {template} uses tokens the {profile.name} profile lacks: {missing}")
    text = re.sub(r"\$\{(\w+)\}", lambda m: tokens[m.group(1)], text)
    dag = json.loads(text)
    dag["project_id"] = project_id
    dag["plan_id"] = plan_id or f"plan.{project_id}"
    dag["goal"] = goal or dag.get("goal", "")
    dag["template"] = f"{template}@{profile.name}"
    dag["created_at"] = contracts.now_iso()
    for t in dag["tasks"]:
        t["project_id"] = project_id
        t.setdefault("schema_version", contracts.SCHEMA_VERSION)
        t.setdefault("created_by", "template")
    return dag

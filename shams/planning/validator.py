"""Plan Validator + DAG templates (ADR-108)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from shams import contracts
from shams.nodes.registry import NodeRegistry



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


def template_aliases() -> dict[str, tuple[str, str, dict]]:
    """Old template names kept working: alias -> (template, domain, domain_config), from the packs."""
    from shams import packs
    return {a: (t, p.name, cfg) for p in packs.all_packs() for a, (t, cfg) in p.template_aliases().items()}


def template_dirs(project_dir: Path | str | None = None, pack=None) -> list[Path]:
    """Project templates/ first, then $SHAMS_HOME/templates, entry points, then the pack(s) (ADR-116/117)."""
    from shams import packs, plugins
    dirs = plugins.search_dirs("templates", project_dir)
    for ep in plugins.entry_points("shams.templates").values():
        p = Path(str(ep.load()))
        if p.is_dir():
            dirs.append(p)
    for pk in ([pack] if pack is not None else packs.all_packs()):
        d = pk.templates_dir()
        if d is not None and Path(d).is_dir():
            dirs.append(Path(d))
    return dirs


def list_templates(project_dir: Path | str | None = None) -> list[str]:
    names = {p.stem for d in template_dirs(project_dir) for p in d.glob("*.json")}
    return sorted(names) + sorted(template_aliases())


def find_template(name: str, project_dir: Path | str | None = None, pack=None) -> Path:
    p = Path(name)
    if p.suffix == ".json" and p.exists():
        return p
    for dirs in ((template_dirs(project_dir, pack),) if pack is not None else ()) + (template_dirs(project_dir),):
        for d in dirs:
            if (d / f"{name}.json").exists():
                return d / f"{name}.json"
    raise KeyError(f"template {name!r} not found; available: {list_templates(project_dir)}")


def instantiate(template: str | None, project_id: str, goal: str, plan_id: str | None = None, *,
                domain: str | None = None, config: dict | None = None,
                project_dir: Path | str | None = None, engine: str | None = None) -> dict:
    """Instantiate a template for one Domain Pack configuration (ADR-115, ADR-117).

    `template` is a name on the search path or a path to a .json file; None = the pack's default.
    `engine=` is a shortcut kept for the Game Pack (domain="game", config={"engine": engine}).
    """
    from shams import packs

    config = dict(config or {})
    if engine is not None:
        domain = domain or "game"
        config.setdefault("engine", engine)
    aliases = template_aliases() if template else {}
    if template in aliases:
        template, alias_domain, alias_cfg = aliases[template]
        domain = domain or alias_domain
        config = {**alias_cfg, **config}
    pack = packs.get(domain) if domain else None
    if template is None:
        pack = pack or packs.get(None)
        template = pack.default_template
        if not template:
            raise KeyError(f"domain {pack.name!r} has no default template; pass one explicitly")
    path = find_template(template, project_dir, pack)
    text = path.read_text(encoding="utf-8")
    if pack is None:
        pack = packs.get(json.loads(text).get("domain"))
    pack.activate(project_dir)
    cfg = pack.normalize_config(config, project_dir)
    tokens = pack.template_tokens(cfg)
    missing = sorted(set(re.findall(r"\$\{(\w+)\}", text)) - set(tokens))
    if missing:
        raise KeyError(f"template {path.stem} uses tokens the {pack.name} configuration lacks: {missing}")
    text = re.sub(r"\$\{(\w+)\}", lambda m: tokens[m.group(1)], text)
    dag = json.loads(text)
    dag["project_id"] = project_id
    dag["plan_id"] = plan_id or f"plan.{project_id}"
    dag["goal"] = goal or dag.get("goal", "")
    dag["domain"] = pack.name
    dag["template"] = f"{path.stem}@{pack.template_label(cfg)}"
    dag["created_at"] = contracts.now_iso()
    for t in dag["tasks"]:
        t["project_id"] = project_id
        t.setdefault("schema_version", contracts.SCHEMA_VERSION)
        t.setdefault("created_by", "template")
    return dag

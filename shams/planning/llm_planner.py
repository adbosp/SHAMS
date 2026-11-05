"""LLM Planner (ADR-120): a reasoning model writes the Task DAG, the Plan Validator decides.

The planner model only proposes. Its JSON is normalised (ids, schema fields), validated with
``validate_dag`` (schema, dependencies, producers, capabilities available on this machine) and, when
invalid, sent back with the exact errors for up to ``max_rounds`` attempts. If it still fails the
caller falls back to the Domain Pack template, so planning never depends on one model.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from shams import contracts
from shams.planning.validator import Issue, validate_dag

KINDS = ["exists", "schema", "compile", "unit_test", "integration_test", "runtime_log", "screenshot", "metric",
         "semantic", "visual", "human"]

PROMPT = """You are the Planner of SHAMS, an AI manufacturing pipeline. Break the goal into a small Task DAG.
Rules:
- 2 to {max_tasks} tasks. Each task has ONE capability chosen from this list (a name ending in .* means any
  sub-name, e.g. code.* allows code.python): {capabilities}
- Every output has a unique logical_id: a lowercase dotted id like code.converter or mesh.chair, NEVER a
  file path (no "/" and no file extension). "name" is the file stem, e.g. temp_converter. And a media_type
  (text/x-python, application/json, text/markdown, image/png, model/gltf-binary, ...).
- A task that reads another task's output lists it in inputs ({{"name", "from_task", "logical_id"}}) AND
  puts that task in depends_on. No cycles.
- Each task has 1-3 acceptance_criteria with kind in {kinds}.
- ids: task_id "task.<slug>", criterion id "ac.<slug>". Slugs: lowercase letters, digits, _ . -
Reply with JSON only:
{{"tasks": [{{"task_id": "task.x", "title": "...", "description": "...", "capability": "...",
  "depends_on": [], "inputs": [], "outputs": [{{"name": "...", "logical_id": "...", "media_type": "..."}}],
  "acceptance_criteria": [{{"id": "ac.x", "description": "...", "kind": "compile"}}]}}]}}"""


def _slug(s: str, prefix: str) -> str:
    s = re.sub(r"[^a-z0-9_.-]+", "_", str(s).lower()).strip("_.-") or "x"
    if not s.startswith(prefix + "."):
        s = f"{prefix}.{s}"
    return s[:120]


def _name(o: dict) -> str:
    """Output name = file stem (the worker adds the extension from media_type)."""
    raw = str(o.get("name") or o.get("logical_id") or "output").replace("\\", "/").split("/")[-1]
    return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", raw) or "output"


def logical_id(v: Any) -> str:
    """Models often put a file path where an id belongs ('src/temp.py'): turn it into a stable slug
    ('src.temp_py'). Applied to inputs and outputs alike, so references keep matching."""
    s = str(v or "").strip().lower().replace("\\", "/").replace("/", ".")
    s = re.sub(r"[^a-z0-9_.-]+", "_", s).strip("_.-")
    if not s or not s[0].isalpha():
        s = "out." + s
    return s[:127] if len(s) > 1 else s + "_x"


def normalize(raw: dict, *, project_id: str, goal: str, domain: str | None, plan_id: str | None = None) -> dict:
    """Make a model's plan schema-shaped without changing its intent."""
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, list):
        raise ValueError("reply has no 'tasks' list")
    idmap = {}
    for t in tasks:
        if isinstance(t, dict):
            idmap[t.get("task_id")] = _slug(t.get("task_id") or t.get("title", "task"), "task")
    out = []
    allowed_task = {"task_id", "title", "description", "capability", "depends_on", "inputs", "outputs",
                    "acceptance_criteria", "priority", "budget", "max_attempts", "workspace_scope", "provider_hint"}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        t = {k: v for k, v in t.items() if k in allowed_task}
        t["task_id"] = idmap.get(t.get("task_id"), _slug(t.get("title", "task"), "task"))
        t["title"] = str(t.get("title") or t["task_id"])[:200]
        t["capability"] = re.sub(r"[^a-z0-9_.]", "_", str(t.get("capability", "")).lower()).strip(".")
        t["depends_on"] = list(dict.fromkeys(idmap.get(d, _slug(d, "task")) for d in t.get("depends_on") or []))
        t["inputs"] = [{"name": str(i.get("name") or i.get("logical_id")), "logical_id": logical_id(i.get("logical_id")),
                        **({"from_task": idmap.get(i["from_task"], _slug(i["from_task"], "task"))}
                           if i.get("from_task") else {})}
                       for i in t.get("inputs") or [] if isinstance(i, dict) and i.get("logical_id")]
        t["outputs"] = [{"name": _name(o), "logical_id": logical_id(o.get("logical_id")),
                         "media_type": str(o.get("media_type") or "text/plain")}
                        for o in t.get("outputs") or [] if isinstance(o, dict) and o.get("logical_id")]
        crit = []
        for n, c in enumerate(t.get("acceptance_criteria") or []):
            if isinstance(c, str):
                c = {"description": c}
            if not isinstance(c, dict):
                continue
            kind = c.get("kind") if c.get("kind") in KINDS else "semantic"
            item = {"id": _slug(c.get("id") or f"{t['task_id'][5:]}_{n}", "ac"), "description":
                    str(c.get("description") or kind), "kind": kind}
            if isinstance(c.get("check"), dict):
                item["check"] = c["check"]
            crit.append(item)
        t["acceptance_criteria"] = crit
        t.update(schema_version=contracts.SCHEMA_VERSION, project_id=project_id, created_by="planner")
        t.setdefault("budget", {"max_wall_seconds": 1800})
        t.setdefault("max_attempts", 3)
        out.append(t)
    dag = {"schema_version": contracts.SCHEMA_VERSION, "project_id": project_id,
           "plan_id": plan_id or f"plan.{project_id}", "plan_version": 1, "goal": goal, "tasks": out,
           "template": "llm_planner", "created_at": contracts.now_iso()}
    if domain:
        dag["domain"] = domain
    return dag


def plan(provider: Any, model: str, *, goal: str, project_id: str, capabilities: list[str],
         domain: str | None = None, domain_context: dict | None = None, nodes=None, max_rounds: int = 3,
         max_tasks: int = 8, chat_options: dict | None = None) -> dict:
    """Returns {"dag": dag | None, "issues": [str], "rounds": n, "seconds": s, "raw": last reply}."""
    from shams.workers import extract_json
    system = PROMPT.format(capabilities=", ".join(sorted(capabilities)), kinds=", ".join(KINDS), max_tasks=max_tasks)
    user = json.dumps({"goal": goal, "domain": domain, "domain_context": domain_context or {}}, ensure_ascii=False)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    t0 = time.monotonic()
    issues: list[str] = []
    raw = ""
    opts = dict(chat_options or {})
    if getattr(provider, "name", "") == "ollama":
        opts.setdefault("format", "json")
    for round_ in range(1, max_rounds + 1):
        r = provider.chat(messages, model=model, **opts)
        raw = r.get("text", "")
        try:
            dag = normalize(extract_json(raw), project_id=project_id, goal=goal, domain=domain)
            found = validate_dag(dag, nodes)
        except (ValueError, KeyError, TypeError) as e:
            dag, found = None, [Issue("error", f"unparseable plan: {e}")]
        issues = [str(i) for i in found if i.level == "error"]
        if dag is not None and not issues:
            return {"dag": dag, "issues": [str(i) for i in found], "rounds": round_,
                    "seconds": round(time.monotonic() - t0, 2), "raw": raw}
        messages = messages + [{"role": "assistant", "content": raw[:6000]},
                               {"role": "user", "content": "The plan is invalid. Fix exactly these errors and "
                                                           "reply with the full JSON again:\n" + "\n".join(issues[:20])}]
    return {"dag": None, "issues": issues, "rounds": max_rounds, "seconds": round(time.monotonic() - t0, 2), "raw": raw}

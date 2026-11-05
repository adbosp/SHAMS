"""Worker implementations (ADR-116).

A worker turns a Context Package into output files plus a handoff draft:
``run(ctx, worker_ref) -> (files, handoff)``. The Task Runner persists the files, QC checks them.

Built-in kinds:

* ``LLMWorker`` (``kind = "llm_agent"`` / ``"inference"``): any ChatProvider writes text outputs
  (code, scenes, configs, docs) as a JSON reply. Works with every local server or API.
* ``CommandWorker`` (``kind = "tool"``): any local program reads the context JSON and writes the
  outputs itself. Fits image / 3D / audio generators, Blender scripts, engine CLIs.
* Custom: ``impl = "package.module:Class"`` in nodes.toml, or the ``shams.workers`` entry point group.
  The class is built with ``(entry=..., attempts_root=..., provider=..., read_blob=...)`` and must
  have ``run(ctx, worker_ref)``.

The Mock Worker (shams.mock) uses the same interface.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from shams import contracts, plugins, providers
from shams.mock.worker import MockWorker

TEXT_MEDIA = {"application/json", "application/xml", "application/x-yaml", "application/toml",
              "application/javascript", "application/vnd.unity.scene", "application/vnd.unity.prefab",
              "application/vnd.godot.scene", "image/svg+xml", "model/gltf+json"}
MAX_INPUT_CHARS = 12_000


def is_text(media_type: str) -> bool:
    return media_type.startswith("text/") or media_type in TEXT_MEDIA or media_type.endswith("+json")


def output_plan(ctx: dict, attempts_root: Path) -> tuple[Path, list[dict]]:
    """Where each declared output goes for this attempt (same layout as the Mock Worker)."""
    task = ctx["task"]
    adir = Path(attempts_root) / task["task_id"] / f"a{ctx['attempt']}"
    plan = []
    for out in task["outputs"]:
        rel = MockWorker.rel_path(task, out)
        plan.append({**out, "rel": rel, "abs": str(adir / rel)})
    return adir, plan


def base_handoff(ctx: dict, worker_ref: dict, status: str, summary: str, *, decisions=None, known_issues=None,
                 usage: dict | None = None, blocked_reason: str | None = None) -> dict:
    h = {
        "schema_version": contracts.SCHEMA_VERSION, "project_id": ctx["project_id"],
        "task_id": ctx["task"]["task_id"], "attempt": ctx["attempt"], "producer": worker_ref,
        "status": status, "inputs": ctx["inputs"], "outputs": [],
        "decisions": [d if isinstance(d, dict) else {"decision": str(d)} for d in (decisions or [])],
        "known_issues": [i if isinstance(i, dict) else {"issue": str(i), "severity": "medium"}
                         for i in (known_issues or [])],
        "self_checks": [], "logs": [], "next_actions": [],
        "context_summary": (summary or ctx["task"]["title"])[:2000],
        "usage": {k: v for k, v in (usage or {}).items() if k in ("tokens_in", "tokens_out", "wall_seconds",
                                                                  "peak_vram_mb", "cost_usd")},
        "timestamp": contracts.now_iso(),
    }
    if blocked_reason:
        h["blocked_reason"] = blocked_reason
    return h


# --------------------------------------------------------------------------- LLM worker
SYSTEM_PROMPT = """You are a specialist worker inside SHAMS, an AI manufacturing pipeline that builds software and games.
You receive one task as JSON. Produce every declared output, honouring the engine, language, paths and acceptance criteria.
If prior_failures is not empty, fix exactly what they describe.
Reply with ONE JSON object and nothing else:
{"files": [{"logical_id": "<declared logical_id>", "content": "<full file content>"}],
 "summary": "<2-4 sentences for the next worker>",
 "decisions": ["<design decision>"],
 "known_issues": ["<anything incomplete>"],
 "status": "READY_FOR_QC" | "BLOCKED",
 "blocked_reason": "<only when BLOCKED>"}"""


def extract_json(text: str) -> dict:
    """Find the first JSON object in a model reply (tolerates code fences and chatter)."""
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text, i)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        i = text.find("{", i + 1)
    raise ValueError("no JSON object in model reply")


class LLMWorker:
    def __init__(self, *, entry: dict, attempts_root: Path, provider: Any,
                 read_blob: Callable[[str], Path] | None = None, system_prompt: str | None = None, **_: Any):
        self.entry = entry
        self.model = entry["worker"].get("model") or ""
        self.provider = provider
        self.attempts_root = Path(attempts_root)
        self.read_blob = read_blob
        cfg = entry.get("provider_config", {})
        self.system_prompt = system_prompt or cfg.get("system_prompt") or SYSTEM_PROMPT
        self.chat_options = dict(cfg.get("chat_options", {}))

    def _input_payload(self, ctx: dict) -> list[dict]:
        out = []
        for i in ctx["inputs"]:
            item = {"logical_id": i["logical_id"], "path": i.get("path"), "media_type": i.get("media_type")}
            if self.read_blob and is_text(i.get("media_type", "")):
                p = self.read_blob(i["sha256"])
                if p.exists():
                    item["content"] = p.read_text(encoding="utf-8", errors="replace")[:MAX_INPUT_CHARS]
            out.append(item)
        return out

    def prompt(self, ctx: dict, plan: list[dict]) -> list[dict]:
        task = ctx["task"]
        payload = {
            "goal": ctx.get("goal", ""), "engine": ctx.get("engine"),
            "task": {"task_id": task["task_id"], "title": task["title"], "description": task.get("description", ""),
                     "capability": task["capability"]},
            "outputs": [{"logical_id": o["logical_id"], "path": o["rel"], "media_type": o["media_type"],
                         "contract": o.get("contract")} for o in plan],
            "acceptance_criteria": [{"id": c["id"], "kind": c["kind"], "description": c["description"]}
                                    for c in task["acceptance_criteria"]],
            "inputs": self._input_payload(ctx),
            "upstream_summaries": ctx.get("upstream_summaries", []),
            "known_issues": ctx.get("known_issues", []),
            "prior_failures": ctx.get("prior_failures", []),
            "workspace_scope": ctx.get("workspace_scope", []),
        }
        return [{"role": "system", "content": self.system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)}]

    def run(self, ctx: dict, worker_ref: dict) -> tuple[list[dict], dict]:
        _, plan = output_plan(ctx, self.attempts_root)
        binary = [o["logical_id"] for o in plan if not is_text(o["media_type"])]
        if binary:
            return [], base_handoff(ctx, worker_ref, "BLOCKED", f"cannot produce binary outputs {binary}",
                                    blocked_reason=f"{worker_ref['worker_id']} only writes text; route "
                                                   f"{binary} to a generator (command/plugin) worker")
        r = providers.timed_chat(self.provider, self.prompt(ctx, plan), self.model, **self.chat_options)
        usage = {"tokens_in": r.get("tokens_in", 0), "tokens_out": r.get("tokens_out", 0),
                 "wall_seconds": r.get("wall_seconds", 0.0)}
        try:
            reply = extract_json(r["text"])
        except ValueError as e:
            return [], base_handoff(ctx, worker_ref, "FAILED", f"unparseable reply: {r['text'][:300]}",
                                    known_issues=[{"issue": f"schema violation: {e}", "severity": "high"}],
                                    usage=usage, blocked_reason=f"schema violation: {e}")
        if reply.get("status") == "BLOCKED":
            return [], base_handoff(ctx, worker_ref, "BLOCKED", reply.get("summary", ""),
                                    usage=usage, blocked_reason=str(reply.get("blocked_reason") or "worker blocked"))
        by_id = {f.get("logical_id"): f.get("content", "") for f in reply.get("files", []) if isinstance(f, dict)}
        files = []
        for o in plan:
            if o["logical_id"] not in by_id:
                continue                      # QC stage 1 reports the missing output -> repair loop
            p = Path(o["abs"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(by_id[o["logical_id"]]), encoding="utf-8")
            files.append(o)
        return files, base_handoff(ctx, worker_ref, "READY_FOR_QC", str(reply.get("summary", "")),
                                   decisions=reply.get("decisions"), known_issues=reply.get("known_issues"),
                                   usage=usage)


# --------------------------------------------------------------------------- command worker
class CommandWorker:
    """Runs ``argv``. The program gets SHAMS_CONTEXT (context JSON incl. an "outputs" list with the
    absolute path for each declared output) and SHAMS_OUT_DIR, writes the outputs, and may write
    ``$SHAMS_OUT_DIR/handoff.json`` with {"summary", "decisions", "known_issues", "status", "blocked_reason"}.
    argv items may use {context}, {out_dir}, {model}, {task_id}."""

    def __init__(self, *, entry: dict, attempts_root: Path, read_blob: Callable[[str], Path] | None = None, **_: Any):
        cfg = entry.get("provider_config", {})
        if not cfg.get("argv"):
            raise ValueError(f"{entry['worker']['worker_id']}: command worker needs provider_config.argv")
        self.argv, self.timeout = list(cfg["argv"]), float(cfg.get("timeout", 3600))
        self.env, self.cwd = dict(cfg.get("env", {})), cfg.get("cwd")
        self.model = entry["worker"].get("model") or ""
        self.attempts_root = Path(attempts_root)
        self.read_blob = read_blob

    def run(self, ctx: dict, worker_ref: dict) -> tuple[list[dict], dict]:
        adir, plan = output_plan(ctx, self.attempts_root)
        adir.mkdir(parents=True, exist_ok=True)
        inputs = [{**i, "blob": str(self.read_blob(i["sha256"]))} if self.read_blob else i for i in ctx["inputs"]]
        doc = {**ctx, "inputs": inputs, "outputs": [{k: o[k] for k in ("name", "logical_id", "media_type", "rel", "abs")
                                                     if k in o} for o in plan]}
        cpath = adir / "context.json"
        cpath.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        fmt = {"context": str(cpath), "out_dir": str(adir), "model": self.model, "task_id": ctx["task"]["task_id"]}
        argv = [a.format(**fmt) for a in self.argv]
        t0 = time.monotonic()
        p = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, cwd=self.cwd, encoding="utf-8",
                           env={**os.environ, **self.env, "SHAMS_CONTEXT": str(cpath), "SHAMS_OUT_DIR": str(adir)})
        usage = {"wall_seconds": round(time.monotonic() - t0, 3)}
        extra = {}
        hp = adir / "handoff.json"
        if hp.exists():
            try:
                extra = json.loads(hp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                extra = {}
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip()[-1000:]
            return [], base_handoff(ctx, worker_ref, "FAILED", f"command exited {p.returncode}", usage=usage,
                                    known_issues=[{"issue": err or "non-zero exit", "severity": "high"}],
                                    blocked_reason=err or f"exit {p.returncode}")
        if extra.get("status") == "BLOCKED":
            return [], base_handoff(ctx, worker_ref, "BLOCKED", extra.get("summary", ""), usage=usage,
                                    blocked_reason=str(extra.get("blocked_reason") or "tool blocked"))
        files = [o for o in plan if Path(o["abs"]).exists()]
        return files, base_handoff(ctx, worker_ref, "READY_FOR_QC",
                                   extra.get("summary") or p.stdout.strip()[-500:] or ctx["task"]["title"],
                                   decisions=extra.get("decisions"), known_issues=extra.get("known_issues"),
                                   usage=usage)


# --------------------------------------------------------------------------- factory
_REGISTERED: dict[str, Callable[..., Any]] = {}


def register_worker(name: str, factory: Callable[..., Any]) -> None:
    _REGISTERED[name] = factory


def create(entry: dict, attempts_root: Path, read_blob: Callable[[str], Path] | None = None) -> Any:
    """Build the implementation for a Capability Registry entry (nodes.toml [[worker]])."""
    w = entry["worker"]
    cfg = dict(entry.get("provider_config", {}))
    impl = cfg.pop("impl", None)
    kw = {"entry": entry, "attempts_root": attempts_root, "read_blob": read_blob}
    if impl:
        eps = plugins.entry_points("shams.workers")
        factory = _REGISTERED.get(impl) or (eps[impl].load() if impl in eps else plugins.load_object(impl))
        ptype = w.get("provider")
        if ptype and ptype != "command" and "argv" not in cfg:
            kw["provider"] = providers.create(ptype, _provider_cfg(cfg))
        return factory(**kw)
    if w["kind"] == "tool" or w.get("provider") == "command":
        return CommandWorker(**kw)
    if w["kind"] in ("llm_agent", "inference"):
        if not w.get("provider"):
            raise ValueError(f"{w['worker_id']}: set worker.provider (e.g. ollama, openai, anthropic)")
        return LLMWorker(provider=providers.create(w["provider"], _provider_cfg(cfg)), **kw)
    raise ValueError(f"{w['worker_id']}: no implementation for kind {w['kind']!r}")


_WORKER_ONLY = {"system_prompt", "chat_options", "argv", "env", "cwd"}


def _provider_cfg(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k not in _WORKER_ONLY}

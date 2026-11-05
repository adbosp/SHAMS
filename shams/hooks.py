"""Hooks (ADR-118): run your own commands when something happens in a project.

Settings ``hooks`` map an event pattern to commands::

    "hooks": {
      "task.accepted":  [{"command": "notify-send 'SHAMS' 'task done'"}],
      "qc.failed":      [{"command": ["python", "scripts/on_fail.py"], "timeout": 30}],
      "run.finished":   [{"command": "git add -A && git commit -m 'SHAMS build'"}],
      "*":              [{"command": "python log_everything.py"}]
    }

Patterns are glob patterns over event types (``task.*``, ``qc.*``, ``plan.created``...). The extra event
``run.finished`` fires when a run stops with every task accepted or waiting for a human.
The command gets the event as JSON on stdin and in ``SHAMS_EVENT``, plus ``SHAMS_PROJECT_DIR``.
A failing hook is logged as ``hook.failed`` and never stops the pipeline.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path
from typing import Any


class HookRunner:
    def __init__(self, hooks: dict[str, Any] | None, project_dir: Path, env: dict[str, str] | None = None):
        self.hooks = {p: (h if isinstance(h, list) else [h]) for p, h in (hooks or {}).items()}
        self.dir = Path(project_dir)
        self.env = dict(env or {})
        self.last_seq = 0
        self.results: list[dict] = []

    def __bool__(self) -> bool:
        return bool(self.hooks)

    def matching(self, event_type: str) -> list[dict]:
        return [h for pat, hs in self.hooks.items() if fnmatch.fnmatchcase(event_type, pat) for h in hs]

    def fire(self, event: dict) -> list[dict]:
        out = []
        for h in self.matching(event["type"]):
            cmd = h["command"]
            payload = json.dumps(event, ensure_ascii=False, default=str)
            env = {**os.environ, **self.env, "SHAMS_EVENT": payload, "SHAMS_PROJECT_DIR": str(self.dir)}
            try:
                p = subprocess.run(cmd, shell=isinstance(cmd, str), input=payload, capture_output=True, text=True,
                                   timeout=float(h.get("timeout", 60)), cwd=str(self.dir), env=env,
                                   encoding="utf-8", errors="replace")
                r = {"event": event["type"], "command": cmd, "ok": p.returncode == 0, "code": p.returncode,
                     "output": (p.stdout or p.stderr).strip()[-500:]}
            except (OSError, subprocess.SubprocessError) as e:
                r = {"event": event["type"], "command": cmd, "ok": False, "code": None, "output": str(e)[:500]}
            out.append(r)
        self.results = (self.results + out)[-100:]
        return out

    def dispatch(self, db, extra: list[dict] | None = None) -> list[dict]:
        """Fire hooks for events appended since the last call (plus synthetic `extra` events)."""
        if not self.hooks:
            return []
        out = []
        for e in db.events(self.last_seq):
            self.last_seq = e["seq"]
            if e["type"].startswith("hook."):
                continue
            ev = {"type": e["type"], "seq": e["seq"], "subject_id": e["subject_id"], "ts": e["ts"],
                  "from_state": e["from_state"], "to_state": e["to_state"],
                  "payload": json.loads(e["payload_json"] or "{}")}
            out += self.fire(ev)
        for ev in extra or []:
            out += self.fire(ev)
        for r in out:
            if not r["ok"]:
                db.append("hook.failed", "project", db.project_id, "system",
                          payload={"event": r["event"], "code": r["code"], "output": r["output"][:300]})
        return out

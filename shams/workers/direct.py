"""Direct Mode chat worker (ADR-121): the user's prompt goes to the chosen model as is.

No JSON file protocol, no planner: one prompt in, one reply out (``text/markdown``), with the optional
system prompt and generation parameters from the task's ``params``. When research is enabled and the
provider supports tool calling, the model may use the read-only web tools (and MCP tools of the preset).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from shams import providers
from shams.workers import MAX_INPUT_CHARS, base_handoff, output_plan

PARAM_MAP = {"temperature": "temperature", "max_tokens": "num_predict", "num_ctx": "num_ctx"}


class DirectChatWorker:
    def __init__(self, *, entry: dict, attempts_root: Path, provider: Any,
                 read_blob: Callable[[str], Path] | None = None, mcp: Any = None, max_tool_rounds: int = 8, **_: Any):
        cfg = entry.get("provider_config", {})
        self.entry, self.provider, self.mcp = entry, provider, mcp
        self.model = entry["worker"].get("model") or ""
        self.attempts_root = Path(attempts_root)
        self.research_default = cfg.get("research")
        self.mcp_servers = cfg.get("mcp")
        self.max_tool_rounds = int(cfg.get("max_tool_rounds", max_tool_rounds))
        self.tool_log: list[dict] = []

    def _options(self, params: dict) -> dict:
        opts = {PARAM_MAP[k]: params[k] for k in PARAM_MAP if k in params}
        if getattr(self.provider, "name", "") == "ollama":
            kw: dict[str, Any] = {"options": opts}
            if "think" in params:
                kw["think"] = bool(params["think"])
            return kw
        return {k: v for k, v in {"temperature": params.get("temperature"),
                                  "max_tokens": params.get("max_tokens")}.items() if v is not None}

    def _use_tools(self, params: dict) -> bool:
        research = getattr(getattr(self.mcp, "research", None), "enabled", False)
        want = params.get("research", self.research_default)
        return bool(self.mcp is not None and getattr(self.provider, "supports_tools", False)
                    and ((research and want is not False) or self.mcp_servers))

    def run(self, ctx: dict, worker_ref: dict) -> tuple[list[dict], dict]:
        _, plan = output_plan(ctx, self.attempts_root)
        task = ctx["task"]
        params = dict(task.get("params") or {})
        prompt = params.get("prompt") or task.get("description") or task["title"]
        messages = ([{"role": "system", "content": params["system"]}] if params.get("system") else []) + \
                   [{"role": "user", "content": prompt}]
        t0 = time.monotonic()
        try:
            if self._use_tools(params):
                r = self._chat_with_tools(messages, params)
            else:
                r = self.provider.chat(messages, model=self.model, **self._options(params))
        except providers.ProviderError as e:
            return [], base_handoff(ctx, worker_ref, "FAILED", "provider error", blocked_reason=f"provider error: {e}"[:500],
                                    known_issues=[{"issue": str(e)[:800], "severity": "high"}])
        text = r.get("text", "")
        usage = {"tokens_in": r.get("tokens_in", 0), "tokens_out": r.get("tokens_out", 0),
                 "wall_seconds": round(time.monotonic() - t0, 3)}
        files = []
        for o in plan:
            if o["media_type"] in ("text/markdown", "text/plain"):
                Path(o["abs"]).parent.mkdir(parents=True, exist_ok=True)
                Path(o["abs"]).write_text(text, encoding="utf-8")
                files.append(o)
        decisions = [{"decision": f"used tool {t['tool']}", "reason": t["summary"]} for t in self.tool_log]
        status = "READY_FOR_QC" if text.strip() else "FAILED"
        return files, base_handoff(ctx, worker_ref, status, text[:300] or "empty reply", usage=usage,
                                   decisions=decisions,
                                   blocked_reason=None if text.strip() else "model returned an empty reply")

    def _chat_with_tools(self, messages: list[dict], params: dict) -> dict:
        catalog = self.mcp.tools(self.mcp_servers)
        if params.get("research", self.research_default) is False:
            catalog = [t for t in catalog if t.get("server") != "research"]
        msgs = list(messages)
        tin = tout = 0
        self.tool_log = []
        for round_ in range(self.max_tool_rounds + 1):
            tools = catalog if round_ < self.max_tool_rounds else []
            r = self.provider.chat_tools(msgs, model=self.model, tools=tools)
            tin, tout = tin + r.get("tokens_in", 0), tout + r.get("tokens_out", 0)
            if not r["tool_calls"]:
                return {"text": r["text"], "tokens_in": tin, "tokens_out": tout}
            msgs.append(r["assistant"])
            results = []
            for call in r["tool_calls"]:
                res = self.mcp.call(call["name"], call["arguments"], catalog)
                text = res["text"][:MAX_INPUT_CHARS]
                results.append((call, ("ERROR: " if res["is_error"] else "") + text))
                self.tool_log.append({"tool": call["name"], "summary": (str(call["arguments"]) + " -> " + text[:120])[:200]})
            msgs += self.provider.tool_results(results)
        return {"text": "", "tokens_in": tin, "tokens_out": tout}

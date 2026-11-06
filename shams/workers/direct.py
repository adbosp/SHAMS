"""Direct Mode chat worker (ADR-121): the user's prompt goes to the chosen model as is.

No JSON file protocol, no planner: one prompt in, one reply out (``text/markdown``), with the optional
system prompt and generation parameters from the task's ``params``. When research is enabled and the
provider supports tool calling, the model may use the read-only web tools (and MCP tools of the preset).
"""
from __future__ import annotations

import time
import re
from contextlib import nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from shams import providers
from shams.research import RESEARCH_PROMPT
from shams.workers import MAX_INPUT_CHARS, base_handoff, output_plan

PARAM_MAP = {"temperature": "temperature", "max_tokens": "num_predict", "num_ctx": "num_ctx"}
reply_progress: ContextVar[Callable[[str, str | None], None] | None] = ContextVar("reply_progress", default=None)


class DirectChatWorker:
    def __init__(self, *, entry: dict, attempts_root: Path, provider: Any,
                 read_blob: Callable[[str], Path] | None = None, mcp: Any = None, max_tool_rounds: int = 8, **_: Any):
        cfg = entry.get("provider_config", {})
        self.entry, self.provider, self.mcp = entry, provider, mcp
        self.model = entry["worker"].get("model") or ""
        self.attempts_root = Path(attempts_root)
        self.research_default = cfg.get("research")
        self.model_tools = cfg.get("model_tools", False)
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
        web = "_chat_history" in params
        messages = list(params.get("_chat_history", []))
        if params.get("system") and (not messages or messages[0]["role"] != "system"):
            messages.insert(0, {"role": "system", "content": params["system"]})
        messages.append({"role": "user", "content": prompt})
        t0 = time.monotonic()
        sources = {}
        research = getattr(self.mcp, "research", None)
        options = self._options(params)
        publish = reply_progress.get()
        reset_progress = None
        if web and publish and getattr(self.provider, "supports_progress", False):
            tid = task["task_id"]
            publish(tid, None)
            options["on_delta"] = lambda text: publish(tid, text)
            reset_progress = lambda: publish(tid, None)
        web_tools = []
        if web and research and self.model_tools and params.get("research", self.research_default) is not False:
            web_tools = [t for t in research.tools() if t["name"] in {
                "research__web_search", "research__web_scrape", "research__web_research"}]
        try:
            if web_tools or (not web and self._use_tools(params)):
                session = getattr(research, "session", nullcontext)
                with session() as run:
                    r = self._chat_with_tools(messages, params, options, web_tools if web else None, reset_progress)
                    sources = run.sources if run else {}
            else:
                self.tool_log = []
                r = self.provider.chat(messages, model=self.model, **options)
        except providers.ProviderError as e:
            return [], base_handoff(ctx, worker_ref, "FAILED", "provider error", blocked_reason=f"provider error: {e}"[:500],
                                    known_issues=[{"issue": str(e)[:800], "severity": "high"}])
        text = r.get("text", "")
        if text.strip() and sources and not web:
            text += "\n\n### Trang đã đọc\n" + "\n".join(f"- <{url}>" for url in sources)
        usage = {"tokens_in": r.get("tokens_in", 0), "tokens_out": r.get("tokens_out", 0),
                 "wall_seconds": round(time.monotonic() - t0, 3)}
        files = []
        for o in plan:
            if o["media_type"] in ("text/markdown", "text/plain"):
                Path(o["abs"]).parent.mkdir(parents=True, exist_ok=True)
                Path(o["abs"]).write_text(text, encoding="utf-8")
                files.append(o)
        decisions = [{"decision": f"used tool {t['tool']}", "reason": t["summary"]} for t in self.tool_log]
        decisions.extend({"decision": "read web source", "reason": url} for url in sources)
        status = "READY_FOR_QC" if text.strip() else "FAILED"
        return files, base_handoff(ctx, worker_ref, status, text[:300] or "empty reply", usage=usage,
                                   decisions=decisions,
                                   blocked_reason=None if text.strip() else "model returned an empty reply")

    def _chat_with_tools(self, messages: list[dict], params: dict, options: dict,
                         web_tools: list[dict] | None, reset_progress: Callable[[], None] | None) -> dict:
        catalog = web_tools if web_tools is not None else self.mcp.tools(self.mcp_servers)
        if params.get("research", self.research_default) is False:
            catalog = [t for t in catalog if t.get("server") != "research"]
        msgs = list(messages)
        if any(t.get("server") == "research" for t in catalog):
            if msgs[0]["role"] == "system":
                msgs[0] = {**msgs[0], "content": msgs[0]["content"] + "\n" + RESEARCH_PROMPT}
            else:
                msgs.insert(0, {"role": "system", "content": RESEARCH_PROMPT})
        tin = tout = 0
        self.tool_log = []
        if web_tools is not None:
            self._read_requested_links(msgs, catalog)
        for round_ in range(self.max_tool_rounds + 1):
            tools = catalog if round_ < self.max_tool_rounds else []
            if hasattr(self.mcp, "available"):
                tools = self.mcp.available(tools)
            r = self.provider.chat_tools(msgs, model=self.model, tools=tools, **options)
            tin, tout = tin + r.get("tokens_in", 0), tout + r.get("tokens_out", 0)
            if not r["tool_calls"] and tools and web_tools is None:
                from shams.workers import text_tool_call
                written = text_tool_call(r["text"], {t["name"] for t in tools})
                if written:                  # a tool call written as plain JSON text
                    r = {**r, "tool_calls": [written], "assistant": {"role": "assistant", "content": r["text"]}}
            if not r["tool_calls"]:
                return {"text": r["text"], "tokens_in": tin, "tokens_out": tout}
            if reset_progress:
                reset_progress()
            msgs.append(r["assistant"])
            msgs += self._tool_results(r["tool_calls"], tools)
        return {"text": "", "tokens_in": tin, "tokens_out": tout}

    def _read_requested_links(self, messages: list[dict], catalog: list[dict]) -> None:
        prompt = messages[-1]["content"]
        if not re.search(r"đối chiếu|kiểm chứng|xác minh|đọc|tra cứu|\b(?:read|verify|cross-check|check)\b", prompt, re.I):
            return
        urls = dict.fromkeys(url.rstrip(".,;:!?") for url in re.findall(r"https?://[^\s<>\])]+", prompt))
        name = "research__web_scrape"
        for i, url in enumerate(list(urls)[:2]):
            args = {"url": url}
            messages.append({"role": "assistant", "content": "",
                             "tool_calls": [{"function": {"name": name, "arguments": args}}]})
            messages += self._tool_results([{"name": name, "id": f"requested_source_{i}", "arguments": args}], catalog)

    def _tool_results(self, calls: list[dict], catalog: list[dict]) -> list[dict]:
        results = []
        for call in calls:
            res = self.mcp.call(call["name"], call["arguments"], catalog)
            text = res["text"][:MAX_INPUT_CHARS]
            results.append((call, ("ERROR: " if res["is_error"] else "") + text))
            self.tool_log.append({"tool": call["name"], "summary": (str(call["arguments"]) + " -> " + text[:120])[:200]})
        return self.provider.tool_results(results)

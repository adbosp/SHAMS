"""Model Providers (ADR-116): one interface for any local model server or remote API.

A provider turns ``chat(messages, model=...)`` into a call on some backend. SHAMS ships:

* ``openai_compatible`` — any server speaking the OpenAI Chat Completions API. That covers
  Ollama, llama.cpp ``llama-server``, LM Studio, vLLM, LocalAI, Jan, text-generation-webui,
  and remote APIs such as OpenAI, OpenRouter, Groq, DeepSeek, Mistral, Together, xAI, Gemini.
* ``anthropic`` — the Anthropic Messages API.
* ``ollama`` — OpenAI-compatible chat plus explicit load / unload (``keep_alive``), so the
  Model Lifecycle can free VRAM between tasks (principle: GPU sequential).
* ``command`` — any local program (image / 3D / audio generators, custom scripts).
* anything else via ``register_provider`` or a ``"package.module:Class"`` reference or the
  ``shams.providers`` entry point group.

Secrets never live in config files: a provider reads its key from the environment variable
named by ``api_key_env``.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol, runtime_checkable

from shams import plugins


class ProviderError(RuntimeError):
    pass


@runtime_checkable
class ChatProvider(Protocol):
    name: str

    def chat(self, messages: list[dict], *, model: str, **opts: Any) -> dict:
        """Returns {"text": str, "tokens_in": int, "tokens_out": int}."""


# --------------------------------------------------------------------------- HTTP helper
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req: urllib.request.Request, timeout: float):
    """Local model servers are reached directly, even when the machine has a proxy configured."""
    host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
    local = host in ("localhost", "::1") or host.startswith("127.")
    return (_DIRECT.open if local else urllib.request.urlopen)(req, timeout=timeout)


def _post_json(url: str, body: dict, headers: dict[str, str], timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with _open(req, timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ProviderError(f"HTTP {e.code} from {url}: {detail}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"cannot reach {url}: {e.reason}") from None


def _get(url: str, headers: dict[str, str], timeout: float) -> int:
    req = urllib.request.Request(url, headers=headers)
    try:
        with _open(req, timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        raise ProviderError(f"cannot reach {url}: {e.reason}") from None


def _stream_lines(url: str, body: dict, headers: dict[str, str], timeout: float, cancel: Any = None):
    """POST and yield response lines as they arrive (NDJSON or SSE). Closing early stops generation on the
    server; ``cancel`` is anything with ``is_set()`` (threading.Event) checked between lines."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        r = _open(req, timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ProviderError(f"HTTP {e.code} from {url}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ProviderError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from None
    try:
        for raw in r:
            if cancel is not None and cancel.is_set():
                return
            line = raw.decode("utf-8", "replace").strip()
            if line:
                yield line
    finally:
        r.close()


class _HTTPProvider:
    name = "http"
    default_base_url = ""
    default_key_env: str | None = None

    def __init__(self, base_url: str | None = None, api_key_env: str | None = None,
                 headers: dict[str, str] | None = None, timeout: float = 600.0, **options: Any):
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        if not self.base_url:
            raise ValueError(f"{self.name}: base_url is required")
        self.api_key_env = api_key_env if api_key_env is not None else self.default_key_env
        self.extra_headers = dict(headers or {})
        self.timeout = float(timeout)
        self.options = options          # passed through to the request body (temperature, ...)

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderError(f"{self.name}: environment variable {self.api_key_env} is not set")
        return key


class OpenAICompatible(_HTTPProvider):
    name = "openai_compatible"

    def _headers(self) -> dict[str, str]:
        h = dict(self.extra_headers)
        key = self.api_key()
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def chat(self, messages: list[dict], *, model: str, **opts: Any) -> dict:
        body = {"model": model, "messages": messages, **self.options, **opts}
        r = _post_json(f"{self.base_url}/chat/completions", body, self._headers(), self.timeout)
        try:
            text = r["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"{self.name}: unexpected response {str(r)[:300]}") from None
        u = r.get("usage") or {}
        return {"text": text, "tokens_in": int(u.get("prompt_tokens", 0)),
                "tokens_out": int(u.get("completion_tokens", 0))}

    def health(self) -> str:
        return "ok" if _get(f"{self.base_url}/models", self._headers(), 10) < 500 else "down"

    def list_models(self) -> list[str]:
        req = urllib.request.Request(f"{self.base_url}/models", headers=self._headers())
        try:
            with _open(req, 15) as r:
                data = json.loads(r.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            raise ProviderError(f"{self.name}: cannot list models: {e}") from None
        return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))

    # Tool calling (MCP, ADR-118) --------------------------------------------------------------
    supports_tools = True

    def chat_tools(self, messages: list[dict], *, model: str, tools: list[dict], **opts: Any) -> dict:
        """One turn with tools. Returns {"text", "tool_calls": [{"id", "name", "arguments"}], "assistant",
        "tokens_in", "tokens_out"}; append "assistant" and tool_results(...) to continue the conversation."""
        body = {"model": model, "messages": messages, **self.options, **opts}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                               "parameters": t["input_schema"]}} for t in tools]
        r = _post_json(f"{self.base_url}/chat/completions", body, self._headers(), self.timeout)
        try:
            msg = r["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"{self.name}: unexpected response {str(r)[:300]}") from None
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append({"id": c.get("id") or fn.get("name"), "name": fn.get("name"), "arguments": args})
        u = r.get("usage") or {}
        assistant = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            assistant["tool_calls"] = msg["tool_calls"]
        return {"text": msg.get("content") or "", "tool_calls": calls, "assistant": assistant,
                "tokens_in": int(u.get("prompt_tokens", 0)), "tokens_out": int(u.get("completion_tokens", 0))}

    @staticmethod
    def tool_results(results: list[tuple[dict, str]]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": call["id"], "content": text} for call, text in results]

    stream_tool_results = tool_results

    # Streaming (desktop client, ADR-122) ----------------------------------------------------
    def chat_stream(self, messages: list[dict], *, model: str, tools: list[dict] | None = None, cancel: Any = None,
                    **opts: Any):
        """Yield {"type": "text"|"tool_calls"|"done", ...} from an SSE Chat Completions stream."""
        body = {"model": model, "messages": messages, **self.options, **opts, "stream": True}
        if tools:
            body["tools"] = tool_specs(tools)
        calls: dict[int, dict] = {}
        text, tin, tout = "", 0, 0
        for line in _stream_lines(f"{self.base_url}/chat/completions", body, self._headers(), self.timeout, cancel):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            u = chunk.get("usage") or {}
            tin, tout = int(u.get("prompt_tokens", tin) or tin), int(u.get("completion_tokens", tout) or tout)
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    text += d["content"]
                    yield {"type": "text", "text": d["content"]}
                for tc in d.get("tool_calls") or []:
                    slot = calls.setdefault(int(tc.get("index", len(calls))), {"id": "", "name": "", "args": ""})
                    fn = tc.get("function") or {}
                    slot["id"] = tc.get("id") or slot["id"]
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
        if calls:
            out, raw = [], []
            for i in sorted(calls):
                c = calls[i]
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": c["args"]}
                cid = c["id"] or f"call_{i}"
                out.append({"id": cid, "name": c["name"], "arguments": args})
                raw.append({"id": cid, "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}})
            yield {"type": "tool_calls", "calls": out,
                   "assistant": {"role": "assistant", "content": text, "tool_calls": raw}}
        yield {"type": "done", "tokens_in": tin, "tokens_out": tout}


def _get_json(url: str, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with _open(req, timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ProviderError(f"HTTP {e.code} from {url}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ProviderError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from None
    except json.JSONDecodeError as e:
        raise ProviderError(f"invalid JSON from {url}: {e}") from None


class Ollama(OpenAICompatible):
    """Ollama: native /api/chat (thinking off, JSON mode, num_ctx), tool calls over the OpenAI-compatible
    endpoint, explicit load/unload through keep_alive, discovery (/api/tags, /api/show) and resource
    reporting (/api/ps)."""
    name = "ollama"
    default_base_url = "http://127.0.0.1:11434"

    def __init__(self, base_url: str | None = None, keep_alive: str = "30m", native: bool = True,
                 think: bool | None = False, num_ctx: int | None = None, unload_wait_s: float = 30.0, **kw: Any):
        root = (base_url or self.default_base_url).rstrip("/")
        self.root = root[:-3] if root.endswith("/v1") else root
        super().__init__(base_url=self.root + "/v1", **kw)
        self.keep_alive = keep_alive
        self.native, self.think, self.num_ctx = native, think, num_ctx
        self.unload_wait_s = float(unload_wait_s)

    # chat ------------------------------------------------------------------------------------
    def chat(self, messages: list[dict], *, model: str, images: list[str] | None = None,
             format: Any = None, think: Any = "default", **opts: Any) -> dict:
        """Native /api/chat. ``images`` are base64 strings attached to the last user message."""
        if not self.native and not images and format is None:
            return super().chat(messages, model=model, **opts)
        msgs = [dict(m) for m in messages]
        if images:
            for m in reversed(msgs):
                if m["role"] == "user":
                    m["images"] = list(images)
                    break
        options = {**{k: v for k, v in self.options.items() if k not in ("think",)}, **opts.pop("options", {}), **opts}
        if self.num_ctx and "num_ctx" not in options:
            options["num_ctx"] = self.num_ctx
        body: dict[str, Any] = {"model": model, "messages": msgs, "stream": False, "keep_alive": self.keep_alive,
                                "options": options}
        think = self.think if think == "default" else think
        if think is not None:
            body["think"] = think
        if format is not None:
            body["format"] = format
        r = _post_json(f"{self.root}/api/chat", body, self.extra_headers, self.timeout)
        if "error" in r:
            raise ProviderError(f"ollama: {r['error']}")
        msg = r.get("message") or {}
        return {"text": msg.get("content") or "", "thinking": msg.get("thinking") or "",
                "tokens_in": int(r.get("prompt_eval_count", 0)), "tokens_out": int(r.get("eval_count", 0)),
                "load_seconds": round(r.get("load_duration", 0) / 1e9, 3)}

    def chat_stream(self, messages: list[dict], *, model: str, tools: list[dict] | None = None, cancel: Any = None,
                    images: list[str] | None = None, think: Any = "default", **opts: Any):
        """Native streaming /api/chat. Yields {"type": "text"|"thinking", "text"}, {"type": "tool_calls",
        "calls", "assistant"} and finally {"type": "done", tokens_in, tokens_out, load_seconds}."""
        msgs = [dict(m) for m in messages]
        if images:
            for m in reversed(msgs):
                if m["role"] == "user":
                    m["images"] = list(images)
                    break
        options = {**{k: v for k, v in self.options.items() if k not in ("think",)}, **opts.pop("options", {}), **opts}
        if self.num_ctx and "num_ctx" not in options:
            options["num_ctx"] = self.num_ctx
        body: dict[str, Any] = {"model": model, "messages": msgs, "stream": True, "keep_alive": self.keep_alive,
                                "options": options}
        think = self.think if think == "default" else think
        if think is not None:
            body["think"] = think
        if tools:
            body["tools"] = tool_specs(tools)
        text, calls, last = "", [], {}
        for line in _stream_lines(f"{self.root}/api/chat", body, self.extra_headers, self.timeout, cancel):
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in chunk:
                raise ProviderError(f"ollama: {chunk['error']}")
            m = chunk.get("message") or {}
            if m.get("thinking"):
                yield {"type": "thinking", "text": m["thinking"]}
            if m.get("content"):
                text += m["content"]
                yield {"type": "text", "text": m["content"]}
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                calls.append({"id": c.get("id") or f"call_{len(calls)}", "name": fn.get("name"), "arguments": args or {}})
            if chunk.get("done"):
                last = chunk
        if calls:
            yield {"type": "tool_calls", "calls": calls,
                   "assistant": {"role": "assistant", "content": text,
                                 "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                                for c in calls]}}
        yield {"type": "done", "tokens_in": int(last.get("prompt_eval_count", 0)),
               "tokens_out": int(last.get("eval_count", 0)), "load_seconds": round(last.get("load_duration", 0) / 1e9, 3)}

    @staticmethod
    def stream_tool_results(results: list[tuple[dict, str]]) -> list[dict]:
        """Tool results for the conversation continued with ``chat_stream`` (native /api/chat format)."""
        return [{"role": "tool", "tool_name": call["name"], "content": text} for call, text in results]

    def load_with(self, model: str, keep_alive: Any) -> None:
        """Load (or keep) a model with an explicit keep_alive (Model Manager)."""
        _post_json(f"{self.root}/api/generate", {"model": model, "keep_alive": keep_alive}, self.extra_headers, self.timeout)

    def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        r = _post_json(f"{self.root}/api/embed", {"model": model, "input": list(texts), "keep_alive": self.keep_alive},
                       self.extra_headers, self.timeout)
        if "error" in r:
            raise ProviderError(f"ollama: {r['error']}")
        return r.get("embeddings") or []

    # lifecycle -------------------------------------------------------------------------------
    def load(self, model: str) -> None:
        _post_json(f"{self.root}/api/generate", {"model": model, "keep_alive": self.keep_alive},
                   self.extra_headers, self.timeout)

    def unload(self, model: str) -> None:
        """Ask Ollama to evict the model and wait until /api/ps no longer lists it (VRAM really freed)."""
        _post_json(f"{self.root}/api/generate", {"model": model, "keep_alive": 0}, self.extra_headers, 60)
        deadline = time.monotonic() + self.unload_wait_s
        while time.monotonic() < deadline:
            try:
                if model not in {m["name"] for m in self.ps()}:
                    return
            except ProviderError:
                return
            time.sleep(0.5)
        raise ProviderError(f"ollama: {model} still loaded {self.unload_wait_s:.0f}s after unload")

    def unload_all(self) -> list[str]:
        names = [m["name"] for m in self.ps()]
        for n in names:
            self.unload(n)
        return names

    # discovery / resources ---------------------------------------------------------------------
    def health(self) -> str:
        return "ok" if _get(f"{self.root}/api/tags", self.extra_headers, 10) < 500 else "down"

    def version(self) -> str:
        return _get_json(f"{self.root}/api/version", self.extra_headers, 10).get("version", "")

    def tags(self) -> list[dict]:
        return _get_json(f"{self.root}/api/tags", self.extra_headers, 15).get("models", [])

    def list_models(self) -> list[str]:
        return sorted(m.get("name", "") for m in self.tags() if m.get("name"))

    def ps(self) -> list[dict]:
        """Loaded models: [{"name", "size", "size_vram", "expires_at"}]."""
        return [{"name": m.get("name") or m.get("model"), "size": int(m.get("size", 0)),
                 "size_vram": int(m.get("size_vram", 0)), "expires_at": m.get("expires_at"),
                 "context_length": m.get("context_length")}
                for m in _get_json(f"{self.root}/api/ps", self.extra_headers, 10).get("models", [])]

    def show(self, model: str) -> dict:
        return _post_json(f"{self.root}/api/show", {"model": model}, self.extra_headers, 30)

    def resources(self) -> dict:
        loaded = self.ps()
        return {"loaded": loaded, "vram_mb": sum(m["size_vram"] for m in loaded) // (1024 * 1024)}


class Anthropic(_HTTPProvider):
    name = "anthropic"
    default_base_url = "https://api.anthropic.com"
    default_key_env = "ANTHROPIC_API_KEY"

    def __init__(self, *a: Any, max_tokens: int = 8192, version: str = "2023-06-01", **kw: Any):
        super().__init__(*a, **kw)
        self.max_tokens = max_tokens
        self.version = version

    def chat(self, messages: list[dict], *, model: str, **opts: Any) -> dict:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs = [m for m in messages if m["role"] != "system"]
        body = {"model": model, "max_tokens": self.max_tokens, "messages": msgs, **self.options, **opts}
        if system:
            body["system"] = system
        r = _post_json(f"{self.base_url}/v1/messages", body, self._headers(), self.timeout)
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        u = r.get("usage") or {}
        return {"text": text, "tokens_in": int(u.get("input_tokens", 0)),
                "tokens_out": int(u.get("output_tokens", 0))}

    supports_tools = True

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key() or "", "anthropic-version": self.version, **self.extra_headers}

    def chat_tools(self, messages: list[dict], *, model: str, tools: list[dict], **opts: Any) -> dict:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs = [m for m in messages if m["role"] != "system"]
        body = {"model": model, "max_tokens": self.max_tokens, "messages": msgs, **self.options, **opts}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                              "input_schema": t["input_schema"]} for t in tools]
        r = _post_json(f"{self.base_url}/v1/messages", body, self._headers(), self.timeout)
        blocks = r.get("content", [])
        u = r.get("usage") or {}
        return {"text": "".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
                "tool_calls": [{"id": b["id"], "name": b["name"], "arguments": b.get("input") or {}}
                               for b in blocks if b.get("type") == "tool_use"],
                "assistant": {"role": "assistant", "content": blocks},
                "tokens_in": int(u.get("input_tokens", 0)), "tokens_out": int(u.get("output_tokens", 0))}

    @staticmethod
    def tool_results(results: list[tuple[dict, str]]) -> list[dict]:
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": text}
                                             for call, text in results]}]

    def health(self) -> str:
        return "ok" if _get(f"{self.base_url}/v1/models", self._headers(), 10) < 500 else "down"

    def list_models(self) -> list[str]:
        req = urllib.request.Request(f"{self.base_url}/v1/models", headers=self._headers())
        try:
            with _open(req, 15) as r:
                data = json.loads(r.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            raise ProviderError(f"{self.name}: cannot list models: {e}") from None
        return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))


class Command:
    """Runs a local program. ``argv`` items may use {model}, {prompt}, {prompt_file}.

    The prompt is the last user message; stdout is the reply.
    """
    name = "command"

    def __init__(self, argv: list[str], timeout: float = 3600.0, env: dict[str, str] | None = None,
                 cwd: str | None = None, **_: Any):
        if not argv:
            raise ValueError("command provider needs argv")
        self.argv, self.timeout, self.env, self.cwd = list(argv), float(timeout), dict(env or {}), cwd

    def chat(self, messages: list[dict], *, model: str, **opts: Any) -> dict:
        import tempfile
        prompt = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(prompt)
        try:
            argv = [a.format(model=model, prompt=prompt, prompt_file=f.name) for a in self.argv]
            p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=self.timeout,
                               env={**os.environ, **self.env}, cwd=self.cwd, encoding="utf-8")
        finally:
            os.unlink(f.name)
        if p.returncode != 0:
            raise ProviderError(f"command exited {p.returncode}: {p.stderr.strip()[-500:]}")
        return {"text": p.stdout, "tokens_in": 0, "tokens_out": 0}


# --------------------------------------------------------------------------- presets
# name -> (class, default config). Users can point any preset at another URL with base_url.
PRESETS: dict[str, tuple[type, dict]] = {
    "openai_compatible": (OpenAICompatible, {}),
    "ollama": (Ollama, {}),
    "llamacpp": (OpenAICompatible, {"base_url": "http://127.0.0.1:8080/v1"}),
    "lmstudio": (OpenAICompatible, {"base_url": "http://127.0.0.1:1234/v1"}),
    "vllm": (OpenAICompatible, {"base_url": "http://127.0.0.1:8000/v1"}),
    "localai": (OpenAICompatible, {"base_url": "http://127.0.0.1:8080/v1"}),
    "openai": (OpenAICompatible, {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"}),
    "openrouter": (OpenAICompatible, {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"}),
    "groq": (OpenAICompatible, {"base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY"}),
    "deepseek": (OpenAICompatible, {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"}),
    "mistral": (OpenAICompatible, {"base_url": "https://api.mistral.ai/v1", "api_key_env": "MISTRAL_API_KEY"}),
    "together": (OpenAICompatible, {"base_url": "https://api.together.xyz/v1", "api_key_env": "TOGETHER_API_KEY"}),
    "xai": (OpenAICompatible, {"base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY"}),
    "gemini": (OpenAICompatible, {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                                  "api_key_env": "GEMINI_API_KEY"}),
    "anthropic": (Anthropic, {}),
    "command": (Command, {}),
}

_REGISTERED: dict[str, Callable[..., Any]] = {}


def register_provider(name: str, factory: Callable[..., Any]) -> None:
    """Add a custom provider in code: ``register_provider("my_api", MyProvider)``."""
    _REGISTERED[name] = factory


def available() -> list[str]:
    plugins.load_extensions()
    return sorted(set(PRESETS) | set(_REGISTERED) | set(plugins.entry_points("shams.providers")))


def create(type_: str, config: dict | None = None) -> Any:
    """Build a provider from a preset name, a registered name, an entry point, or 'module:Class'."""
    cfg = dict(config or {})
    plugins.load_extensions()
    if type_ in _REGISTERED:
        return _REGISTERED[type_](**cfg)
    if type_ in PRESETS:
        cls, defaults = PRESETS[type_]
        return cls(**{**defaults, **cfg})
    eps = plugins.entry_points("shams.providers")
    if type_ in eps:
        return eps[type_].load()(**cfg)
    if ":" in type_:
        return plugins.load_object(type_)(**cfg)
    raise KeyError(f"unknown provider {type_!r}; choose one of {available()} or use 'package.module:Class'")


def tool_specs(tools: list[dict]) -> list[dict]:
    """SHAMS/MCP tool descriptors ({name, description, input_schema}) -> OpenAI/Ollama function specs."""
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                               "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in tools]


def stream_chat(provider: Any, messages: list[dict], *, model: str, tools: list[dict] | None = None,
                cancel: Any = None, **opts: Any):
    """Stream from any provider: native ``chat_stream`` when it has one, else one non-streamed turn."""
    if hasattr(provider, "chat_stream"):
        yield from provider.chat_stream(messages, model=model, tools=tools, cancel=cancel, **opts)
        return
    if tools and getattr(provider, "supports_tools", False):
        r = provider.chat_tools(messages, model=model, tools=tools)
        if r.get("text"):
            yield {"type": "text", "text": r["text"]}
        if r.get("tool_calls"):
            yield {"type": "tool_calls", "calls": r["tool_calls"], "assistant": r["assistant"]}
    else:
        r = provider.chat(messages, model=model, **opts)
        if r.get("text"):
            yield {"type": "text", "text": r["text"]}
    yield {"type": "done", "tokens_in": r.get("tokens_in", 0), "tokens_out": r.get("tokens_out", 0)}


def timed_chat(provider: Any, messages: list[dict], model: str, **opts: Any) -> dict:
    t0 = time.monotonic()
    r = provider.chat(messages, model=model, **opts)
    r["wall_seconds"] = round(time.monotonic() - t0, 3)
    return r


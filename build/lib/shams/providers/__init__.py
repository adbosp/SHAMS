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


class Ollama(OpenAICompatible):
    """Ollama: chat over its OpenAI-compatible endpoint, load/unload through keep_alive."""
    name = "ollama"
    default_base_url = "http://127.0.0.1:11434"

    def __init__(self, base_url: str | None = None, keep_alive: str = "30m", **kw: Any):
        root = (base_url or self.default_base_url).rstrip("/")
        self.root = root[:-3] if root.endswith("/v1") else root
        super().__init__(base_url=self.root + "/v1", **kw)
        self.keep_alive = keep_alive

    def load(self, model: str) -> None:
        _post_json(f"{self.root}/api/generate", {"model": model, "keep_alive": self.keep_alive},
                   self.extra_headers, self.timeout)

    def unload(self, model: str) -> None:
        _post_json(f"{self.root}/api/generate", {"model": model, "keep_alive": 0}, self.extra_headers, 60)

    def health(self) -> str:
        return "ok" if _get(f"{self.root}/api/tags", self.extra_headers, 10) < 500 else "down"


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
        headers = {"x-api-key": self.api_key() or "", "anthropic-version": self.version, **self.extra_headers}
        r = _post_json(f"{self.base_url}/v1/messages", body, headers, self.timeout)
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        u = r.get("usage") or {}
        return {"text": text, "tokens_in": int(u.get("input_tokens", 0)),
                "tokens_out": int(u.get("output_tokens", 0))}


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
    return sorted(set(PRESETS) | set(_REGISTERED) | set(plugins.entry_points("shams.providers")))


def create(type_: str, config: dict | None = None) -> Any:
    """Build a provider from a preset name, a registered name, an entry point, or 'module:Class'."""
    cfg = dict(config or {})
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


def timed_chat(provider: Any, messages: list[dict], model: str, **opts: Any) -> dict:
    t0 = time.monotonic()
    r = provider.chat(messages, model=model, **opts)
    r["wall_seconds"] = round(time.monotonic() - t0, 3)
    return r

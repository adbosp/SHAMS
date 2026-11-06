"""Model Registry by role (ADR-120).

A *role* is a job in the pipeline (planner, coder, 3D scene, visual QC, embedding, image, image-to-3D).
Settings say which model the user prefers for each role; the registry discovers what is really
installed (Ollama ``/api/tags`` + ``/api/show`` here; image / 3D tools through backends that extensions
register with ``register_backend``), maps the preferred
name onto the real ID (``qwen3.5-4b-coder:Q8`` -> ``hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0``) and
generates one worker per resolved role. LangGraph and the scheduler only see capabilities: no model ID
is written in code.

Settings::

    "models": {
      "ollama": {"base_url": "http://127.0.0.1:11434", "keep_alive": "10m"},
      "auto_workers": true, "node": "node.local",
      "roles": {"planner": {"model": "qwen3.5:9b", "num_ctx": 16384}, ...},
      "aliases": {"qwen3.5-4b-coder:Q8": "hf.co/ccore/Qwen3.5-4B-3D-Coder:Q8_0"}
    },
    "tools": {"<tool>": {...}}          # read by the backend / probe an extension registered
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# role -> generic definition. Capabilities ending in .* cover a family (see nodes.registry.covers).
ROLE_SPECS: dict[str, dict[str, Any]] = {
    "planner": {"label": "Planner / Reasoning", "backend": "ollama", "kind": "llm_agent",
                "capabilities": ["plan.*", "architect.*", "debug_analysis", "semantic_review"],
                "needs": ["completion"], "num_ctx": 16384},
    "coder": {"label": "Coding Specialist", "backend": "ollama", "kind": "llm_agent",
              "capabilities": ["code.*", "integration", "integration.*"], "needs": ["completion"],
              "num_ctx": 16384},
    "scene3d": {"label": "3D Scene / Procedural", "backend": "ollama", "kind": "llm_agent", "impl": "scene3d",
                "capabilities": ["scene.*", "mesh.procedural", "mesh.text_to_3d"],
                "needs": ["completion"], "num_ctx": 16384},
    "visual_qc": {"label": "Visual QC", "backend": "ollama", "kind": "inference",
                  "capabilities": ["visual_review", "visual_review.*"], "needs": ["vision"], "num_ctx": 8192},
    "embedding": {"label": "Embedding / Retrieval", "backend": "ollama", "kind": "inference", "worker": False,
                  "capabilities": ["embed.text", "retrieval.*"], "needs": ["embedding"]},
    # backends for these two come from extensions (settings: "backend": "<name>")
    "image": {"label": "Image generation", "backend": None, "kind": "tool", "capabilities": ["image.*"]},
    "image_to_3d": {"label": "Image-to-3D", "backend": None, "kind": "tool", "capabilities": ["mesh.image_to_3d"]},
    "build": {"label": "Build & run tools", "backend": None, "kind": "tool", "capabilities": ["build.*"]},
}

# role -> config used when the user's settings do not mention the role (extensions add tools that need no
# setup, e.g. a build tool that runs from a SHAMS toolchain folder); "enabled": false in settings turns it off
DEFAULT_ROLES: dict[str, dict[str, Any]] = {}


def register_default_role(role: str, cfg: dict[str, Any]) -> None:
    DEFAULT_ROLES[role] = dict(cfg)

WORKER_PREFIX = "worker.role."


# --------------------------------------------------------------------------- model name matching
_QUANT = re.compile(r"^(q\d.*|f16|fp16|bf16|f32|int8|int4|fp8.*)$")


def _tokens(name: str) -> list[str]:
    n = name.lower()
    if n.startswith("hf.co/") or n.count("/") >= 1:
        n = n.rsplit("/", 1)[-1]                      # drop registry/user prefix
    n = n.replace(":latest", "")
    return [t for t in re.split(r"[:\-_/\s]+", n) if t]


def _tok_match(want: str, have: str) -> bool:
    if want == have:
        return True
    if _QUANT.match(want) and _QUANT.match(have):
        return have.startswith(want) or have.replace("_", "").startswith(want.replace("_", ""))
    return False


def match_model(wanted: str, available: list[str], aliases: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Map a preferred model name onto an installed ID. Returns (model_id | None, how).

    Order: explicit alias -> exact -> case-insensitive -> every token of the wanted name present in the
    installed name (quantization ``q8`` matches ``q8_0``; size tokens like ``9b`` must match exactly).
    Ambiguous matches pick the candidate with the fewest extra tokens; ties are reported, not guessed."""
    aliases = aliases or {}
    if wanted in aliases and aliases[wanted] in available:
        return aliases[wanted], "alias"
    if wanted in available:
        return wanted, "exact"
    low = {a.lower(): a for a in available}
    if wanted.lower() in low:
        return low[wanted.lower()], "case"
    if wanted.lower() + ":latest" in low:
        return low[wanted.lower() + ":latest"], "tag"
    wt = _tokens(wanted)
    scored = []
    for a in available:
        at = _tokens(a)
        if all(any(_tok_match(w, h) for h in at) for w in wt):
            scored.append((len(at) - len(wt), a))
    if not scored:
        return None, "missing"
    scored.sort()
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, "ambiguous: " + ", ".join(a for _, a in scored[:4])
    return scored[0][1], "fuzzy"


# --------------------------------------------------------------------------- discovery
_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_SECONDS = 30.0


def discover_ollama(base_url: str | None = None, timeout_show: bool = True, fresh: bool = False) -> dict:
    """Cached for CACHE_SECONDS (the UI and the registry ask often).
    {"status": "ok"|"down", "version", "models": [{name, size_mb, family, parameters, quantization,
    capabilities, context_length}], "loaded": [...], "error"}"""
    import time
    key = f"{base_url}|{timeout_show}"
    hit = _CACHE.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    out = _discover(base_url, timeout_show)
    _CACHE[key] = (time.monotonic(), out)
    return out


def _discover(base_url: str | None, timeout_show: bool) -> dict:
    from shams import providers
    prov = providers.create("ollama", {"base_url": base_url} if base_url else {})
    out: dict[str, Any] = {"base_url": prov.root, "status": "down", "models": [], "loaded": []}
    try:
        out["version"] = prov.version()
        tags = prov.tags()
        out["loaded"] = prov.ps()
    except providers.ProviderError as e:
        out["error"] = str(e)
        return out
    out["status"] = "ok"
    for t in tags:
        d = t.get("details") or {}
        info = {"name": t.get("name"), "size_mb": int(t.get("size", 0)) // (1024 * 1024),
                "family": d.get("family"), "parameters": d.get("parameter_size"),
                "quantization": d.get("quantization_level"), "capabilities": [], "context_length": None}
        if timeout_show:
            try:
                s = prov.show(info["name"])
                info["capabilities"] = list(s.get("capabilities") or [])
                mi = s.get("model_info") or {}
                info["context_length"] = next((v for k, v in mi.items() if k.endswith(".context_length")), None)
            except providers.ProviderError:
                pass
        out["models"].append(info)
    return out


# --------------------------------------------------------------------------- extension points
# backend name -> object with bind(registry, role, cfg) -> RoleBinding and worker(registry, binding) -> dict
BACKENDS: dict[str, Any] = {}
# role -> fn(registry, worker_dict) that adds tool config (e.g. the path of a 3D program)
ROLE_HOOKS: dict[str, list] = {}
# name -> fn(settings) -> dict, shown in `shams models` and the UI
PROBES: dict[str, Any] = {}


def register_backend(name: str, backend: Any) -> None:
    BACKENDS[name] = backend


def register_role_hook(role: str, fn) -> None:
    ROLE_HOOKS.setdefault(role, []).append(fn)


def register_probe(name: str, fn) -> None:
    PROBES[name] = fn


# --------------------------------------------------------------------------- registry
@dataclass
class RoleBinding:
    role: str
    wanted: str | None
    model_id: str | None
    status: str                    # ok | missing | ambiguous | down | not_configured | no_<capability>
    how: str = ""
    backend: str = ""
    info: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        return {"role": self.role, "label": ROLE_SPECS.get(self.role, {}).get("label", self.role),
                "wanted": self.wanted, "model_id": self.model_id, "status": self.status, "how": self.how,
                "backend": self.backend, "capabilities": role_capabilities(self.role, self.config),
                "size_mb": self.info.get("size_mb"), "model_capabilities": self.info.get("capabilities", [])}


def role_capabilities(role: str, cfg: dict | None = None) -> list[str]:
    cfg = cfg or {}
    return list(cfg.get("capabilities") or ROLE_SPECS.get(role, {}).get("capabilities", []))


def estimate_vram_mb(size_mb: int, num_ctx: int | None) -> int:
    """Weights + KV cache/compute buffers. Conservative so the scheduler never over-commits one GPU."""
    ctx = int(num_ctx or 8192)
    return int(size_mb * 1.1) + 300 + ctx // 16


class ModelRegistry:
    def __init__(self, settings: dict, ollama: dict | None = None):
        from shams import plugins
        plugins.load_extensions()
        self.settings = settings
        self.cfg = settings.get("models") or {}
        self.tools = settings.get("tools") or {}
        self._ollama = ollama

    @property
    def ollama_url(self) -> str | None:
        return (self.cfg.get("ollama") or {}).get("base_url")

    def ollama(self) -> dict:
        if self._ollama is None:
            roles = self.cfg.get("roles") or {}
            def backend(r, c):
                return (c.get("backend") if isinstance(c, dict) else None) or ROLE_SPECS.get(r, {}).get("backend")
            if not any(backend(r, c) == "ollama" for r, c in roles.items()):
                self._ollama = {"status": "not_configured", "models": [], "loaded": []}
            else:
                self._ollama = discover_ollama(self.ollama_url)
        return self._ollama

    def bindings(self) -> list[RoleBinding]:
        out = []
        aliases = dict(self.cfg.get("aliases") or {})
        roles = dict(self.cfg.get("roles") or {})
        for role, dcfg in DEFAULT_ROLES.items():
            roles.setdefault(role, dict(dcfg))
        for role, rcfg in roles.items():
            rcfg = dict(rcfg or {}) if not isinstance(rcfg, str) else {"model": rcfg}
            spec = ROLE_SPECS.get(role, {"backend": rcfg.get("backend", "ollama")})
            backend = rcfg.get("backend") or spec.get("backend")
            if rcfg.get("enabled") is False:
                out.append(RoleBinding(role, rcfg.get("model"), None, "disabled", backend=backend, config=rcfg))
                continue
            if backend == "ollama":
                out.append(self._bind_ollama(role, rcfg, spec, aliases))
            elif backend in BACKENDS:
                out.append(BACKENDS[backend].bind(self, role, rcfg))
            elif not backend:
                out.append(RoleBinding(role, rcfg.get("model"), None, "not_configured",
                                       how="set models.roles.<role>.backend", config=rcfg))
            else:
                out.append(RoleBinding(role, rcfg.get("model"), None, "unknown_backend",
                                       how=f"no backend {backend!r}; known: {sorted(BACKENDS) + ['ollama']}",
                                       backend=backend, config=rcfg))
        return out

    def _bind_ollama(self, role: str, rcfg: dict, spec: dict, aliases: dict) -> RoleBinding:
        wanted = rcfg.get("model")
        disc = self.ollama()
        if disc.get("status") != "ok":
            return RoleBinding(role, wanted, None, "down", how=disc.get("error", ""), backend="ollama", config=rcfg)
        names = [m["name"] for m in disc["models"]]
        model_id, how = match_model(wanted or "", names, aliases) if wanted else (None, "no model set")
        if model_id is None:
            return RoleBinding(role, wanted, None, "ambiguous" if how.startswith("ambiguous") else "missing",
                               how=how, backend="ollama", config=rcfg)
        info = next(m for m in disc["models"] if m["name"] == model_id)
        caps = set(info.get("capabilities") or [])
        for need in spec.get("needs", []):
            if caps and need not in caps:
                return RoleBinding(role, wanted, model_id, f"no_{need}", how=how, backend="ollama", info=info,
                                   config=rcfg)
        return RoleBinding(role, wanted, model_id, "ok", how=how, backend="ollama", info=info, config=rcfg)

    def binding(self, role: str) -> RoleBinding | None:
        return next((b for b in self.bindings() if b.role == role), None)

    # ------------------------------------------------------------------ workers for the scheduler
    def workers(self) -> list[dict]:
        """nodes.toml short-form workers for every resolved role (auto_workers)."""
        if self.cfg.get("auto_workers", True) is False:
            return []
        node = self.cfg.get("node", "node.local")
        o = self.cfg.get("ollama") or {}
        out = []
        for b in self.bindings():
            spec = ROLE_SPECS.get(b.role, {})
            if not b.ok or spec.get("worker", True) is False or b.config.get("worker") is False:
                continue
            w: dict[str, Any] = {"id": WORKER_PREFIX + b.role, "node": node, "lane": b.config.get("lane", "gpu"),
                                 "capabilities": role_capabilities(b.role, b.config)}
            cfg: dict[str, Any] = {}
            if b.backend == "ollama":
                num_ctx = int(b.config.get("num_ctx") or spec.get("num_ctx") or 8192)
                w.update(kind=spec.get("kind", "llm_agent"), provider="ollama", model=b.model_id,
                         vram_mb=int(b.config.get("vram_mb") or estimate_vram_mb(b.info.get("size_mb", 0), num_ctx)))
                cfg = {k: v for k, v in {"base_url": o.get("base_url"), "keep_alive": b.config.get("keep_alive",
                                                                                                 o.get("keep_alive")),
                                         "num_ctx": num_ctx}.items() if v}
                if b.config.get("think") is not None:
                    cfg["think"] = b.config["think"]
                if b.config.get("chat_options"):
                    cfg["chat_options"] = b.config["chat_options"]
            elif b.backend in BACKENDS:
                extra = BACKENDS[b.backend].worker(self, b)
                cfg = dict(extra.pop("config", {}))
                w.update(extra)
            if spec.get("impl") and "impl" not in cfg:
                cfg["impl"] = spec["impl"]
            w["config"] = cfg
            for hook in ROLE_HOOKS.get(b.role, []):
                hook(self, w)
            out.append(w)
        return out

    def report(self) -> dict:
        d = self.ollama()
        tools = {}
        for name, probe in PROBES.items():
            try:
                tools[name] = probe(self.settings)
            except Exception as e:  # noqa: BLE001 - a probe must never break the report
                tools[name] = {"status": "error", "error": str(e)}
        return {"ollama": {k: d.get(k) for k in ("base_url", "status", "version", "error") if d.get(k) is not None}
                | {"models": d.get("models", []), "loaded": d.get("loaded", [])},
                "roles": [b.as_dict() for b in self.bindings()], "tools": tools}


def merge_role_workers(settings: dict, registry: ModelRegistry | None = None) -> list[dict]:
    """settings workers + generated role and preset workers (a user worker with the same id wins)."""
    from shams import settings as settings_mod
    user = settings_mod.resolve_workers(settings)
    m = settings.get("models") or {}
    if not m.get("roles") and not m.get("presets"):
        return user
    reg = registry or ModelRegistry(settings)
    have = {w.get("id") for w in user}
    gen = reg.workers() + [p["worker"] for p in reg.presets() if p.get("worker")]
    return user + [w for w in gen if w["id"] not in have]


# --------------------------------------------------------------------------- presets (Direct Mode, ADR-121)
CHAT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["prompt"], "properties": {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 100000},
    "system": {"type": "string", "maxLength": 20000},
    "temperature": {"type": "number", "minimum": 0, "maximum": 2},
    "max_tokens": {"type": "integer", "minimum": 1, "maximum": 65536},
    "num_ctx": {"type": "integer", "minimum": 512, "maximum": 262144},
    "think": {"type": "boolean"},
    "research": {"type": "boolean", "description": "allow read-only web research tools"}}}
IMAGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["prompt"], "properties": {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
    "negative": {"type": "string", "maxLength": 4000},
    "width": {"type": "integer", "minimum": 256, "maximum": 2048, "multipleOf": 16},
    "height": {"type": "integer", "minimum": 256, "maximum": 2048, "multipleOf": 16},
    "steps": {"type": "integer", "minimum": 1, "maximum": 100},
    "cfg": {"type": "number", "minimum": 0, "maximum": 30},
    "seed": {"type": "integer", "minimum": -1},
    "lora_strength": {"type": "number", "minimum": 0, "maximum": 2}}}
SCENE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["prompt"], "properties": {
    "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
    "builder": {"type": "string", "pattern": "^[a-z0-9_]{1,32}$",
                "description": "auto (default), or a builder offered by the scene worker"}}}
KIND_IO = {
    "chat": (CHAT_SCHEMA, [{"name": "response", "media_type": "text/markdown"}]),
    "image": (IMAGE_SCHEMA, [{"name": "image", "media_type": "image/png"}]),
    "scene3d": (SCENE_SCHEMA, [{"name": "scene", "media_type": "model/gltf-binary"},
                               {"name": "render", "media_type": "image/png"},
                               {"name": "scene_report", "media_type": "application/json"}]),
}
ROLE_PRESET_KIND = {"planner": "chat", "coder": "chat", "visual_qc": "chat", "scene3d": "scene3d", "image": "image"}
DIRECT_PREFIX = "worker.direct."


def preset_slug(pid: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", pid.lower()).strip("_") or "preset"


def direct_capability(pid: str) -> str:
    return "direct." + preset_slug(pid)


def _preset_entries(cfg: dict) -> list[tuple[str, dict]]:
    raw = cfg.get("presets") or {}
    if isinstance(raw, list):
        return [(str(p.get("id")), dict(p)) for p in raw if isinstance(p, dict) and p.get("id")]
    return [(str(k), dict(v or {})) for k, v in raw.items()]


def _presets(self) -> list[dict]:
    """Every model / preset the user can pick in Direct Mode, with its contract:
    capability, provider, resources, input schema, outputs, execution settings, content policy, status.
    Resolved roles become presets automatically; ``models.presets`` adds more (registry only, never code)."""
    out: list[dict] = []
    aliases = dict(self.cfg.get("aliases") or {})
    allow_adult = bool((self.settings.get("general") or {}).get("allow_adult_content", False))
    for b in self.bindings():
        kind = ROLE_PRESET_KIND.get(b.role)
        if kind is None:
            continue
        out.append(self._make_preset(b.role, {"label": ROLE_SPECS[b.role]["label"], "kind": kind,
                                              "capabilities": role_capabilities(b.role, b.config),
                                              "execution_modes": ["direct", "autonomous"],
                                              "autonomous_default": False, "source": "role"}, b, allow_adult))
    for pid, pc in _preset_entries(self.cfg):
        provider = pc.get("provider") or pc.get("backend") or "ollama"
        kind = pc.get("kind") or getattr(BACKENDS.get(provider), "default_kind", "chat")
        if provider == "ollama":
            b = self._bind_ollama(pid, pc, {"needs": ["completion"] if kind == "chat" else []}, aliases)
        elif provider in BACKENDS:
            b = BACKENDS[provider].bind(self, pid, pc)
        else:
            b = RoleBinding(pid, pc.get("model"), None, "unknown_backend", how=f"no provider {provider!r}",
                            backend=provider, config=pc)
        out.append(self._make_preset(pid, {**pc, "kind": kind, "source": "preset"}, b, allow_adult))
    return out


def _make_preset(self, pid: str, pc: dict, b: RoleBinding, allow_adult: bool) -> dict:
    kind = pc.get("kind", "chat")
    schema, outputs = KIND_IO.get(kind, KIND_IO["chat"])
    policy = dict(pc.get("content_policy") or {})
    modes = list(pc.get("execution_modes") or ["direct"])
    auto_default = bool(pc.get("autonomous_default", False)) and not policy.get("adult")
    status = b.status
    if b.ok and policy.get("adult") and not allow_adult:
        status = "disabled_adult"          # opt-in: general.allow_adult_content
    p = {"id": pid, "label": pc.get("label") or pid, "description": pc.get("description", ""), "kind": kind,
         "provider": b.backend or pc.get("provider"), "wanted": b.wanted, "model": b.model_id, "status": status,
         "how": b.how, "capabilities": list(pc.get("capabilities") or []), "direct_capability": direct_capability(pid),
         "execution_modes": modes, "autonomous_default": auto_default, "source": pc.get("source", "preset"),
         "input_schema": pc.get("input_schema") or schema, "outputs": pc.get("outputs") or outputs,
         "params": dict(pc.get("params") or {}), "content_policy": policy,
         "resources": {"lane": pc.get("lane", "gpu"), "vram_mb": None}}
    if status != "ok" or "direct" not in modes:
        return p
    caps = [p["direct_capability"]] + (p["capabilities"] if auto_default and "autonomous" in modes else [])
    w: dict[str, Any] = {"id": DIRECT_PREFIX + preset_slug(pid), "node": self.cfg.get("node", "node.local"),
                         "lane": p["resources"]["lane"], "capabilities": caps}
    o = self.cfg.get("ollama") or {}
    if b.backend == "ollama":
        num_ctx = int(pc.get("num_ctx") or ROLE_SPECS.get(pid, {}).get("num_ctx") or 8192)
        w.update(kind="llm_agent", provider="ollama", model=b.model_id,
                 vram_mb=int(pc.get("vram_mb") or estimate_vram_mb(b.info.get("size_mb", 0), num_ctx)))
        cfg = {k: v for k, v in {"base_url": o.get("base_url"), "keep_alive": pc.get("keep_alive", o.get("keep_alive")),
                                 "num_ctx": num_ctx}.items() if v}
        cfg["impl"] = "scene3d" if kind == "scene3d" else "direct_chat"
    elif b.backend in BACKENDS:
        extra = BACKENDS[b.backend].worker(self, b)
        cfg = dict(extra.pop("config", {}))
        w.update({k: v for k, v in extra.items() if k not in ("id", "capabilities")})
        if pc.get("vram_mb"):
            w["vram_mb"] = int(pc["vram_mb"])
    else:
        return p
    cfg["preset"] = pid
    if policy:
        cfg["content_policy"] = {**policy, "allowed": allow_adult or not policy.get("adult")}
    if pc.get("research") is not None:
        cfg["research"] = bool(pc["research"])
    w["config"] = cfg
    if kind == "scene3d":
        for hook in ROLE_HOOKS.get("scene3d", []):
            hook(self, w)
    p["resources"]["vram_mb"] = w.get("vram_mb")
    p["worker"] = w
    return p


def _preset(self, pid: str) -> dict | None:
    return next((p for p in self.presets() if p["id"] == pid), None)


ModelRegistry.presets = _presets
ModelRegistry._make_preset = _make_preset
ModelRegistry.preset = _preset

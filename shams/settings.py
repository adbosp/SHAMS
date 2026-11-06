"""Layered settings (ADR-118), in the spirit of Claude Code's settings.json.

Layers, later wins:

1. built-in defaults
2. user:          ``$SHAMS_HOME/settings.json``            (default ``~/.shams/settings.json``)
3. project:       ``<project>/.shams/settings.json``       (share with the team, commit it)
4. project local: ``<project>/.shams/settings.local.json`` (this machine only, do not commit)

Compatible inputs that are merged too: ``nodes.toml`` (ADR-116) and ``.mcp.json`` (``mcpServers``,
the format Claude Code and other MCP clients use).

Keys::

    general      language, default_mode, default_domain, mock ("auto" | "on" | "off"), open_browser, open_game,
                 max_tool_rounds
    permissions  mode ("default" | "full_access" | "read_only"), allow [...], deny [...]
                 patterns match tool names like "mcp__github__create_issue" or "mcp__fs__*"
    connections  {name: {provider, base_url, api_key_env, headers, ...}}  reusable model/API connections
    nodes        [{id, kind, gpus, ...}]                                    compute nodes (nodes.toml form)
    workers      [{id, connection | provider, model, node, lane, capabilities, mcp, config}]
    mcp_servers  {name: {type: "stdio" | "http", command, args, env, url, headers, enabled}}
    hooks        {event_pattern: [{command, timeout}]}   e.g. "task.accepted", "qc.failed", "run.finished"
    env          {NAME: value}   non-secret environment for workers, hooks and MCP servers

Secrets are never written to any of these files: use ``api_key_env`` / ``${VAR}`` references. A
key typed in the UI is kept only in the memory of the running process (``set_session_secret``).
"""
from __future__ import annotations

import copy
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from shams.plugins import shams_home

SCOPES = ("user", "project", "local")
PERMISSION_MODES = ("default", "full_access", "read_only")
MOCK_MODES = ("auto", "on", "off")

DEFAULTS: dict[str, Any] = {
    "general": {"language": "vi", "default_mode": "autonomous", "default_domain": None, "mock": "auto",
                "open_browser": True, "open_game": True, "max_tool_rounds": 8, "allow_adult_content": False},
    "permissions": {"mode": "default", "allow": [], "deny": []},
    "connections": {},
    "nodes": [],
    "workers": [],
    "mcp_servers": {},
    "hooks": {},
    "env": {},
    "models": {"ollama": {"base_url": "http://127.0.0.1:11434", "keep_alive": "10m"}, "auto_workers": True,
               "node": "node.local", "roles": {}, "aliases": {}},
    "tools": {},
    "research": {"enabled": False},
}

SECRET_KEY = re.compile(r"(?i)(^|_)(api_?key|token|secret|password|passwd|authorization)$")
SECRET_ENV = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD)")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SettingsError(ValueError):
    pass


# --------------------------------------------------------------------------- files
def path_for(scope: str, project_dir: Path | str | None = None) -> Path:
    if scope == "user":
        return shams_home() / "settings.json"
    if project_dir is None:
        raise SettingsError(f"scope {scope!r} needs a project folder")
    name = "settings.json" if scope == "project" else "settings.local.json"
    return Path(project_dir) / ".shams" / name


def read_scope(scope: str, project_dir: Path | str | None = None) -> dict:
    p = path_for(scope, project_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise SettingsError(f"{p}: invalid JSON ({e})") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{p}: top level must be an object")
    return data


def _by_id(base: list, extra: list) -> list:
    out = {item.get("id"): item for item in base if isinstance(item, dict)}
    for item in extra:
        if isinstance(item, dict):
            out[item.get("id")] = {**out.get(item.get("id"), {}), **item}
    return list(out.values())


def merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if k in ("nodes", "workers") and isinstance(v, list):
            out[k] = _by_id(out.get(k, []), v)
        elif k in ("allow", "deny") and isinstance(v, list):
            out[k] = list(dict.fromkeys([*out.get(k, []), *v]))
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _nodes_toml(project_dir: Path | None) -> dict:
    for p in ([project_dir / "nodes.toml"] if project_dir else []) + [shams_home() / "nodes.toml"]:
        if p.exists():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            return {"nodes": [dict(n) for n in data.get("node", [])], "workers": [dict(w) for w in data.get("worker", [])]}
    return {}


def _mcp_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SettingsError(f"{path}: invalid JSON ({e})") from None
    servers = {}
    for name, s in (data.get("mcpServers") or {}).items():
        s = dict(s)
        s.setdefault("type", "http" if s.get("url") else "stdio")
        if s["type"] in ("streamable-http", "streamable_http", "sse"):
            s["type"] = "http"
        servers[name] = s
    return {"mcp_servers": servers}


def load(project_dir: Path | str | None = None) -> dict:
    """Merged settings for a project (or user-only when project_dir is None)."""
    d = Path(project_dir) if project_dir else None
    s = merge(DEFAULTS, _nodes_toml(d))
    s = merge(s, _mcp_json(shams_home() / "mcp.json"))
    s = merge(s, read_scope("user"))
    if d is not None:
        s = merge(s, _mcp_json(d / ".mcp.json"))
        s = merge(s, read_scope("project", d))
        s = merge(s, read_scope("local", d))
    return s


def sources(project_dir: Path | str | None = None) -> dict[str, str]:
    """Which files exist, for the UI."""
    d = Path(project_dir) if project_dir else None
    out = {"user": str(path_for("user"))}
    if d is not None:
        out.update(project=str(path_for("project", d)), local=str(path_for("local", d)))
        for name in ("nodes.toml", ".mcp.json"):
            if (d / name).exists():
                out[name] = str(d / name)
    return out


# --------------------------------------------------------------------------- validation
def find_secrets(obj: Any, where: str = "") -> list[str]:
    """Paths of literal secrets (e.g. "api_key": "sk-...") — those must be env references instead."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{where}.{k}" if where else str(k)
            if isinstance(v, str) and v and SECRET_KEY.search(str(k)) and not ENV_REF.fullmatch(v.strip()):
                found.append(p)
            elif k == "env" and isinstance(v, dict):
                found += [f"{p}.{n}" for n, val in v.items()
                          if SECRET_ENV.search(n) and isinstance(val, str) and val and not ENV_REF.fullmatch(val)]
            elif k == "headers" and isinstance(v, dict):
                found += [f"{p}.{n}" for n, val in v.items()
                          if re.search(r"(?i)authorization|api-?key|token", n) and isinstance(val, str)
                          and not ENV_REF.search(val)]
            else:
                found += find_secrets(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += find_secrets(v, f"{where}[{i}]")
    return found


def validate(s: dict) -> list[str]:
    errs = [f"{p}: secrets must not be stored in settings; use api_key_env or ${{VAR}}" for p in find_secrets(s)]
    g = s.get("general", {})
    if g.get("default_mode") not in (None, "collaborative", "guided", "autonomous"):
        errs.append("general.default_mode: collaborative | guided | autonomous")
    if g.get("mock", "auto") not in MOCK_MODES:
        errs.append(f"general.mock: one of {MOCK_MODES}")
    if s.get("permissions", {}).get("mode", "default") not in PERMISSION_MODES:
        errs.append(f"permissions.mode: one of {PERMISSION_MODES}")
    for name, m in (s.get("mcp_servers") or {}).items():
        t = m.get("type", "stdio")
        if t == "stdio" and not m.get("command"):
            errs.append(f"mcp_servers.{name}: stdio server needs command")
        elif t == "http" and not m.get("url"):
            errs.append(f"mcp_servers.{name}: http server needs url")
        elif t not in ("stdio", "http"):
            errs.append(f"mcp_servers.{name}.type: stdio | http")
    conns = s.get("connections") or {}
    for w in s.get("workers") or []:
        if w.get("connection") and w["connection"] not in conns:
            errs.append(f"workers.{w.get('id')}: unknown connection {w['connection']!r}")
    from shams.models import ROLE_SPECS
    for role, r in ((s.get("models") or {}).get("roles") or {}).items():
        if role not in ROLE_SPECS and not (isinstance(r, dict) and r.get("backend") and r.get("capabilities")):
            errs.append(f"models.roles.{role}: unknown role; known: {sorted(ROLE_SPECS)} "
                        f"(a custom role needs backend and capabilities)")
    from shams.models import _preset_entries
    for pid, pr in _preset_entries(s.get("models") or {}):
        cp = pr.get("content_policy") or {}
        if cp.get("adult") and pr.get("autonomous_default"):
            errs.append(f"models.presets.{pid}: an adult-only preset cannot be autonomous_default")
        if cp.get("adult") and "autonomous" in (pr.get("execution_modes") or []):
            errs.append(f"models.presets.{pid}: an adult-only preset can only run in direct mode")
    for pat, hooks in (s.get("hooks") or {}).items():
        for h in hooks if isinstance(hooks, list) else [hooks]:
            if not isinstance(h, dict) or not h.get("command"):
                errs.append(f"hooks.{pat}: each hook needs a command")
    return errs


def save(scope: str, data: dict, project_dir: Path | str | None = None) -> Path:
    """Replace one scope's file with `data` after validation. Returns the path written."""
    if scope not in SCOPES:
        raise SettingsError(f"scope must be one of {SCOPES}")
    errs = validate(merge(DEFAULTS, data))
    if errs:
        raise SettingsError("; ".join(errs))
    p = path_for(scope, project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if scope == "local":
        gi = p.parent / ".gitignore"
        if not gi.exists():
            gi.write_text("settings.local.json\n", encoding="utf-8")
    return p


def update(scope: str, patch: dict, project_dir: Path | str | None = None) -> Path:
    """Merge `patch` into one scope's file. A value of None deletes the key."""
    cur = read_scope(scope, project_dir)

    def apply(dst: dict, src: dict) -> dict:
        for k, v in src.items():
            if v is None:
                dst.pop(k, None)
            elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                apply(dst[k], v)
            else:
                dst[k] = v
        return dst
    return save(scope, apply(cur, patch), project_dir)


# --------------------------------------------------------------------------- secrets & env
def set_session_secret(name: str, value: str) -> None:
    """Make a key available to this process only (never written to disk)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise SettingsError(f"invalid environment variable name {name!r}")
    os.environ[name] = value


def env_status(s: dict) -> dict[str, bool]:
    """Every env var the settings refer to -> whether it is set now."""
    names = set()

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "api_key_env" and isinstance(v, str):
                    names.add(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            names.update(ENV_REF.findall(o))
    walk({k: v for k, v in s.items() if k != "env"})
    from shams import providers
    for c in list((s.get("connections") or {}).values()) + list(s.get("workers") or []):
        preset = providers.PRESETS.get(c.get("provider", ""))
        if preset and preset[1].get("api_key_env"):
            names.add(preset[1]["api_key_env"])
    return {n: bool(os.environ.get(n)) for n in sorted(names)}


def expand(value: Any, env: dict[str, str] | None = None) -> Any:
    """Replace ${VAR} in strings (recursively) from `env` then os.environ."""
    if isinstance(value, str):
        return ENV_REF.sub(lambda m: (env or {}).get(m.group(1)) or os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------- workers
def resolve_workers(s: dict) -> list[dict]:
    """Workers in nodes.toml short form, with `connection` and `mcp` folded into provider/config."""
    conns = s.get("connections") or {}
    out = []
    for w in s.get("workers") or []:
        w = dict(w)
        if w.get("enabled") is False:
            continue
        cfg = dict(w.get("config") or {})
        conn = w.pop("connection", None)
        if conn:
            c = dict(conns.get(conn) or {})
            w.setdefault("provider", c.pop("provider", None))
            c.pop("models", None)
            c.pop("label", None)
            cfg = {**c, **cfg}
        mcp = w.pop("mcp", None)
        if mcp:
            cfg["mcp"] = mcp
        if cfg:
            w["config"] = cfg
        out.append(w)
    return out

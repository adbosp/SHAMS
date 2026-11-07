"""Ready-made MCP server presets (ADR-123).

SHAMS already speaks MCP (shams/mcp.py, ADR-118). This catalog only saves typing: each preset is a
normal ``mcp_servers`` entry that ``shams mcp add <preset>`` (or the API) copies into settings, where
the user can edit it like any other server. Nothing here is started until a worker or chat uses it,
and every call still goes through ``permissions`` (deny > allow > readOnlyHint).

Packages run with ``npx`` / ``uvx`` on first use. Pin a version in settings once a server is known
to work on your machine (SHAMS rule: reuse OSS, pin versions).
"""
from __future__ import annotations

import copy
from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "searxng": {
        "title": "Web search via local SearXNG (Google and other engines)",
        "requires": "SearXNG running (Docker Desktop), SEARXNG_URL e.g. http://localhost:8888; Node.js",
        "server": {"type": "stdio", "command": "npx", "args": ["-y", "mcp-searxng"],
                   "env": {"SEARXNG_URL": "${SEARXNG_URL}"}},
        "allow": ["mcp__searxng__*"],
    },
    "browser": {
        "title": "Browser automation (Microsoft Playwright MCP)",
        "requires": "Node.js; the first run downloads a browser",
        "server": {"type": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp@latest", "--headless"]},
        "allow": ["mcp__browser__browser_snapshot", "mcp__browser__browser_navigate"],
    },
    "github": {
        "title": "GitHub (official remote server: repos, issues, PRs)",
        "requires": "GITHUB_TOKEN env var (fine-grained token)",
        "server": {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
                   "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}},
        "allow": [],
    },
    "blender": {
        "title": "Blender editor control (blender-mcp)",
        "requires": "uv (uvx); Blender with the blender-mcp addon enabled and its server started",
        "server": {"type": "stdio", "command": "uvx", "args": ["blender-mcp"]},
        "allow": ["mcp__blender__get_scene_info", "mcp__blender__get_object_info"],
        "gpu": True,
    },
    "unity": {
        "title": "Unity editor control (MCP for Unity, HTTP transport)",
        "requires": "Unity project with the MCP for Unity package; its HTTP server on port 8080",
        "server": {"type": "http", "url": "${UNITY_MCP_URL}"},
        "env_defaults": {"UNITY_MCP_URL": "http://localhost:8080/mcp"},
        "allow": [],
        "gpu": True,
    },
    "filesystem": {
        "title": "Read/write files inside one folder",
        "requires": "Node.js; set SHAMS_MCP_ROOT to the folder to expose",
        "server": {"type": "stdio", "command": "npx",
                   "args": ["-y", "@modelcontextprotocol/server-filesystem", "${SHAMS_MCP_ROOT}"]},
        "allow": ["mcp__filesystem__read_*", "mcp__filesystem__list_*", "mcp__filesystem__search_*"],
    },
}


def list_presets() -> list[dict]:
    return [{"id": k, "title": v["title"], "requires": v["requires"], "type": v["server"]["type"],
             "gpu": bool(v.get("gpu"))} for k, v in PRESETS.items()]


def settings_patch(preset_id: str, name: str | None = None, *, allow: bool = True,
                   current: dict | None = None) -> dict:
    """The settings patch that adds a preset as server `name` (default: the preset id).

    `allow` adds the preset's read-mostly allow patterns to permissions.allow (merged with `current`
    settings so existing rules are kept). Write actions always stay behind explicit permission.
    """
    if preset_id not in PRESETS:
        raise KeyError(f"unknown MCP preset {preset_id!r}; known: {', '.join(PRESETS)}")
    p = PRESETS[preset_id]
    name = name or preset_id
    server = copy.deepcopy(p["server"])
    server["preset"] = preset_id
    patch: dict[str, Any] = {"mcp_servers": {name: server}}
    defaults = p.get("env_defaults") or {}
    cur_env = (current or {}).get("env") or {}
    missing = {k: v for k, v in defaults.items() if k not in cur_env}
    if missing:
        patch["env"] = missing
    if allow and p.get("allow"):
        rules = [r.replace(f"mcp__{preset_id}__", f"mcp__{name}__") for r in p["allow"]]
        have = list(((current or {}).get("permissions") or {}).get("allow") or [])
        patch["permissions"] = {"allow": have + [r for r in rules if r not in have]}
    return patch


def gpu_servers(servers: dict[str, dict]) -> set[str]:
    """Servers whose tools drive a GPU app (Blender/Unity): callers hold the GPU gate around them."""
    return {n for n, s in (servers or {}).items() if PRESETS.get(s.get("preset", ""), {}).get("gpu") or s.get("gpu")}

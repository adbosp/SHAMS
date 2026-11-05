"""Model Context Protocol client (ADR-118).

Lets workers use tools from any MCP server (file systems, GitHub, databases, browsers, engine
editors...). No extra dependency: JSON-RPC 2.0 over

* ``stdio``  — SHAMS starts the server program (``command`` + ``args``) and talks over stdin/stdout;
* ``http``   — Streamable HTTP (one endpoint, JSON or SSE replies, ``Mcp-Session-Id``).

Servers come from settings ``mcp_servers`` or a Claude-Code style ``.mcp.json`` (``mcpServers``).
Every tool call passes through ``Permissions`` first (allow / deny patterns, read-only mode).
Tool names are exposed to models as ``mcp__<server>__<tool>``.
"""
from __future__ import annotations

import fnmatch
import itertools
import json
import os
import queue
import re
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any

from shams import settings as settings_mod

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "shams", "version": "2"}


class MCPError(RuntimeError):
    pass


def qualified(server: str, tool: str) -> str:
    """mcp__<server>__<tool>, restricted to [A-Za-z0-9_-] and 64 chars (OpenAI tool-name rules)."""
    clean = lambda s: re.sub(r"[^A-Za-z0-9_-]", "_", s)
    return f"mcp__{clean(server)}__{clean(tool)}"[:64]


# --------------------------------------------------------------------------- transports
class StdioTransport:
    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 cwd: str | None = None):
        flags = 0x08000000 if os.name == "nt" else 0
        import shutil
        exe = shutil.which(command) or command      # Windows: finds npx.cmd / uvx.exe on PATH
        try:
            self.proc = subprocess.Popen([exe, *(args or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env={**os.environ, **(env or {})}, cwd=cwd,
                                         text=True, encoding="utf-8", bufsize=1, creationflags=flags)
        except OSError as e:
            raise MCPError(f"cannot start {command!r}: {e}") from None
        self.inbox: queue.Queue = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                self.inbox.put(json.loads(line))
            except json.JSONDecodeError:
                continue                       # servers sometimes log to stdout; ignore non-JSON
        self.inbox.put(None)

    def _read_err(self) -> None:
        for line in self.proc.stderr:  # type: ignore[union-attr]
            self.stderr.append(line.rstrip())
            del self.stderr[:-50]

    def send(self, msg: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]
        except (OSError, ValueError) as e:
            raise MCPError(f"server closed: {e}; stderr: {' | '.join(self.stderr[-3:])}") from None

    def request(self, msg: dict, timeout: float) -> dict:
        self.send(msg)
        while True:
            try:
                r = self.inbox.get(timeout=timeout)
            except queue.Empty:
                raise MCPError(f"timeout after {timeout}s waiting for {msg['method']}") from None
            if r is None:
                raise MCPError(f"server exited; stderr: {' | '.join(self.stderr[-3:])}")
            if r.get("id") == msg["id"]:
                return r
            # server->client requests (sampling, roots...) are not supported: answer with an error
            if "method" in r and "id" in r:
                self.send({"jsonrpc": "2.0", "id": r["id"], "error": {"code": -32601, "message": "not supported"}})

    def close(self) -> None:
        try:
            self.proc.stdin.close()  # type: ignore[union-attr]
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class HTTPTransport:
    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url, self.headers = url, dict(headers or {})
        self.session: str | None = None

    def _post(self, msg: dict, timeout: float) -> tuple[int, str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL_VERSION, **self.headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=h, method="POST")
        from shams.providers import _open
        try:
            with _open(req, timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session = sid
                return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise MCPError(f"HTTP {e.code} from {self.url}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise MCPError(f"cannot reach {self.url}: {e.reason}") from None

    def send(self, msg: dict) -> None:
        self._post(msg, 30)

    def request(self, msg: dict, timeout: float) -> dict:
        _, ctype, body = self._post(msg, timeout)
        if "text/event-stream" in ctype:
            for block in body.split("\n\n"):
                data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
                if not data:
                    continue
                try:
                    r = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict) and r.get("id") == msg["id"]:
                    return r
            raise MCPError(f"no response to {msg['method']} in event stream")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise MCPError(f"invalid JSON from {self.url}: {body[:200]}") from None

    def close(self) -> None:
        if self.session:
            try:
                req = urllib.request.Request(self.url, method="DELETE",
                                             headers={"Mcp-Session-Id": self.session, **self.headers})
                urllib.request.urlopen(req, timeout=5).close()
            except Exception:
                pass


# --------------------------------------------------------------------------- client
class MCPClient:
    def __init__(self, name: str, config: dict, env: dict[str, str] | None = None, timeout: float = 60.0):
        self.name, self.timeout = name, float(config.get("timeout", timeout))
        cfg = settings_mod.expand(config, env)
        t = cfg.get("type") or ("http" if cfg.get("url") else "stdio")
        if t == "stdio":
            self.transport: Any = StdioTransport(cfg["command"], cfg.get("args"), {**(env or {}), **cfg.get("env", {})},
                                                 cfg.get("cwd"))
        elif t == "http":
            self.transport = HTTPTransport(cfg["url"], cfg.get("headers"))
        else:
            raise MCPError(f"{name}: unknown transport {t!r}")
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.server_info: dict = {}
        self._tools: list[dict] | None = None
        init = self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                         "clientInfo": CLIENT_INFO})
        self.server_info = init.get("serverInfo", {})
        self.transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        with self._lock:
            msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
            r = self.transport.request(msg, timeout or self.timeout)
        if "error" in r:
            e = r["error"]
            raise MCPError(f"{self.name}.{method}: {e.get('message')} ({e.get('code')})")
        return r.get("result") or {}

    def list_tools(self, refresh: bool = False) -> list[dict]:
        if self._tools is None or refresh:
            tools, cursor = [], None
            while True:
                r = self._call("tools/list", {"cursor": cursor} if cursor else {})
                tools += r.get("tools", [])
                cursor = r.get("nextCursor")
                if not cursor:
                    break
            self._tools = tools
        return self._tools

    def call_tool(self, tool: str, arguments: dict | None = None) -> dict:
        """Returns {"text": str, "is_error": bool, "raw": result}."""
        r = self._call("tools/call", {"name": tool, "arguments": arguments or {}})
        parts = []
        for c in r.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                parts.append(json.dumps(c.get("resource", {}), ensure_ascii=False)[:4000])
            else:
                parts.append(f"[{c.get('type')} content]")
        if not parts and r.get("structuredContent") is not None:
            parts.append(json.dumps(r["structuredContent"], ensure_ascii=False))
        return {"text": "\n".join(parts), "is_error": bool(r.get("isError")), "raw": r}

    def close(self) -> None:
        self.transport.close()


# --------------------------------------------------------------------------- permissions
class Permissions:
    """Claude-Code style rules for tool calls.

    * ``deny`` patterns always win;
    * ``full_access``: everything else is allowed;
    * ``default``: allowed when it matches ``allow`` or the tool declares ``readOnlyHint``;
    * ``read_only``: only tools that declare ``readOnlyHint`` (and are not denied).
    """

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.mode = cfg.get("mode", "default")
        self.allow = list(cfg.get("allow", []))
        self.deny = list(cfg.get("deny", []))

    @staticmethod
    def _match(name: str, patterns: list[str]) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in patterns)

    def check(self, name: str, tool: dict | None = None) -> tuple[bool, str]:
        read_only = bool(((tool or {}).get("annotations") or {}).get("readOnlyHint"))
        if self._match(name, self.deny):
            return False, f"{name} is denied by permissions.deny"
        if self.mode == "full_access":
            return True, "full_access"
        if self.mode == "read_only":
            return (True, "read-only tool") if read_only else (False, f"{name}: read_only mode allows only "
                                                                      "tools marked readOnlyHint")
        if read_only or self._match(name, self.allow):
            return True, "allowed"
        return False, f"{name} is not in permissions.allow (add it in Settings → Permissions)"


# --------------------------------------------------------------------------- manager
class MCPManager:
    """Lazily connects to the configured servers and routes qualified tool calls."""

    def __init__(self, servers: dict[str, dict], permissions: Permissions | None = None,
                 env: dict[str, str] | None = None):
        self.servers = {k: v for k, v in (servers or {}).items() if v.get("enabled", True)}
        self.permissions = permissions or Permissions()
        self.env = dict(env or {})
        self.clients: dict[str, MCPClient] = {}
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, s: dict) -> "MCPManager":
        return cls(s.get("mcp_servers") or {}, Permissions(s.get("permissions")), s.get("env"))

    def client(self, name: str) -> MCPClient:
        with self._lock:
            if name not in self.clients:
                if name not in self.servers:
                    raise MCPError(f"unknown or disabled MCP server {name!r}")
                try:
                    self.clients[name] = MCPClient(name, self.servers[name], self.env)
                    self.errors.pop(name, None)
                except Exception as e:
                    self.errors[name] = str(e)
                    raise
            return self.clients[name]

    def tools(self, names: list[str] | str | None = None) -> list[dict]:
        """[{name: qualified, server, tool, description, input_schema, annotations}] for the selected servers."""
        selected = list(self.servers) if names in (None, "all", ["all"]) else (
            [names] if isinstance(names, str) else list(names))
        out = []
        for s in selected:
            try:
                for t in self.client(s).list_tools():
                    out.append({"name": qualified(s, t["name"]), "server": s, "tool": t["name"],
                                "description": t.get("description", ""), "annotations": t.get("annotations", {}),
                                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}}})
            except Exception as e:  # one broken server must not take the others down
                self.errors[s] = str(e)
        return out

    def call(self, qualified_name: str, arguments: dict, catalog: list[dict] | None = None) -> dict:
        tool = next((t for t in (catalog or self.tools()) if t["name"] == qualified_name), None)
        if tool is None:
            return {"text": f"unknown tool {qualified_name}", "is_error": True, "denied": False}
        ok, why = self.permissions.check(qualified_name, tool)
        if not ok:
            return {"text": f"permission denied: {why}", "is_error": True, "denied": True}
        try:
            r = self.client(tool["server"]).call_tool(tool["tool"], arguments)
        except Exception as e:
            return {"text": f"tool error: {e}", "is_error": True, "denied": False}
        return {**r, "denied": False}

    def status(self) -> list[dict]:
        out = []
        for name, cfg in self.servers.items():
            row = {"name": name, "type": cfg.get("type", "stdio"), "ok": False, "tools": [], "error": None}
            try:
                c = self.client(name)
                row.update(ok=True, server=c.server_info,
                           tools=[{"name": t["name"], "qualified": qualified(name, t["name"]),
                                   "read_only": bool((t.get("annotations") or {}).get("readOnlyHint")),
                                   "allowed": self.permissions.check(qualified(name, t["name"]), t)[0],
                                   "description": (t.get("description") or "")[:200]}
                                  for t in c.list_tools(refresh=True)])
            except Exception as e:
                row["error"] = str(e)
            out.append(row)
        return out

    def close(self) -> None:
        for c in self.clients.values():
            c.close()
        self.clients.clear()

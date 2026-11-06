"""Workspace tools for the coding agent + permission policy (ADR-122).

The agent loop (``agent.py``) offers these tools to a tool-calling model. Every call goes through
``PermissionPolicy.decide``, which returns ``allow``, ``ask`` (the user must approve in the client) or
``deny``. Three modes, chosen per conversation:

* ``ask``   — Ask Before Action: reads inside the workspace are free; every write and every command asks.
* ``safe``  — Allow Safe Workspace Actions: writes inside the workspace and known-safe commands (tests,
  lint, status, build) run; anything else asks.
* ``auto``  — Autonomous With Guardrails: everything inside the workspace runs, except destructive or
  sensitive external actions, which always ask.

In every mode: paths outside the workspace are refused, destructive operations (delete, ``rm -rf``,
``git push``, ``git reset --hard``, installs, killing processes...) ask, and every write first saves a
checkpoint so the change can be reverted.
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

MODES = ("ask", "safe", "auto")
MODE_LABELS = {"ask": "Ask Before Action", "safe": "Allow Safe Workspace Actions", "auto": "Autonomous With Guardrails"}
MAX_READ = 200_000
MAX_OUT = 20_000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist",
             "build", "target", ".next", ".idea", ".vs"}

DESTRUCTIVE = [r"\brm\s+-[a-z]*r", r"\brm\s+-[a-z]*f", r"\brmdir\b", r"\brd\s+/s", r"\bdel\s+/[sq]", r"\bdel\b.*\*",
               r"remove-item\b", r"\bformat\s+[a-z]:", r"\bmkfs", r"\bdd\s+if=", r"git\s+push", r"git\s+reset\s+--hard",
               r"git\s+clean\s+-[a-z]*f", r"git\s+checkout\s+--\s", r"git\s+branch\s+-D", r"git\s+rebase",
               r"\bshutdown\b", r"\breboot\b", r"\btaskkill\b", r"stop-process", r"\bkill(all)?\b", r"\bpkill\b",
               r"\bchmod\s+-R", r"\bchown\s+-R", r"\breg\s+(add|delete)", r"set-executionpolicy", r"\bsudo\b",
               r"\brunas\b", r">\s*/dev/sd", r"curl[^|]*\|\s*(sh|bash|powershell|iex)", r"\biex\b", r"invoke-expression",
               r"\bnpm\s+publish", r"\btwine\s+upload", r"\bdocker\s+(rm|rmi|system\s+prune)"]
SENSITIVE = [r"\bpip\s+install", r"\bnpm\s+(install|i|add)\b", r"\byarn\s+add", r"\bpnpm\s+add", r"\bwinget\b",
             r"\bchoco\b", r"\bapt(-get)?\s+install", r"\bcurl\b", r"\bwget\b", r"invoke-webrequest", r"\bssh\b",
             r"\bscp\b", r"ollama\s+(pull|rm|push)", r"\bgit\s+(clone|fetch|pull)"]
SAFE_COMMANDS = [r"^(python(3)?|py)(\s+-m)?\s+pytest\b", r"^pytest\b", r"^(npm|pnpm|yarn)\s+(test|run\s+(test|lint|build|typecheck))\b",
                 r"^npx\s+(tsc|eslint|vitest|prettier\s+--check)\b", r"^cargo\s+(check|test|build|clippy|fmt\s+--check)\b",
                 r"^go\s+(test|build|vet)\b", r"^git\s+(status|diff|log|show|branch)\b", r"^(ls|dir|pwd|echo|type|cat)\b",
                 r"^(python(3)?|py)\s+-m\s+(ruff|mypy|black\s+--check|compileall)\b", r"^ruff\b", r"^mypy\b",
                 r"^dotnet\s+(build|test)\b", r"^node\s+--version", r"^python(3)?\s+--version"]

TOOL_SPECS = [
    {"name": "list_dir", "description": "List files and folders in a workspace directory (relative path; '.' is the root).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer", "minimum": 1, "maximum": 4}}}},
    {"name": "read_file", "description": "Read a text file in the workspace. Optional 1-based line range.",
     "input_schema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}}},
    {"name": "search_files", "description": "Search workspace text files for a regular expression. Returns path:line: text.",
     "input_schema": {"type": "object", "required": ["pattern"], "properties": {
         "pattern": {"type": "string"}, "glob": {"type": "string", "description": "e.g. *.py"}}}},
    {"name": "write_file", "description": "Create or overwrite a text file in the workspace with the full new content.",
     "input_schema": {"type": "object", "required": ["path", "content"], "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}}},
    {"name": "edit_file", "description": "Replace an exact, unique piece of text in a workspace file.",
     "input_schema": {"type": "object", "required": ["path", "old_text", "new_text"], "properties": {
         "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}}},
    {"name": "delete_file", "description": "Delete a file in the workspace (always needs the user's approval).",
     "input_schema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}},
    {"name": "run_command", "description": "Run a shell command in the workspace folder (tests, builds, git status...). "
                                           "Returns exit code and output.",
     "input_schema": {"type": "object", "required": ["command"], "properties": {
         "command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1, "maximum": 1800}}}},
]
READ_TOOLS = {"list_dir", "read_file", "search_files"}
WRITE_TOOLS = {"write_file", "edit_file"}


class ToolError(Exception):
    pass


def _match_any(patterns: list[str], text: str) -> str | None:
    low = text.lower()
    return next((p for p in patterns if re.search(p, low)), None)


def classify_command(cmd: str) -> str:
    """'destructive' | 'sensitive' | 'safe' | 'normal' — chained commands take the worst part."""
    parts = [p.strip() for p in re.split(r"&&|\|\||;|\n", cmd) if p.strip()]
    worst = "safe"
    order = ["safe", "normal", "sensitive", "destructive"]
    for p in parts or [cmd]:
        if _match_any(DESTRUCTIVE, p):
            c = "destructive"
        elif _match_any(SENSITIVE, p):
            c = "sensitive"
        elif _match_any(SAFE_COMMANDS, p.lower()):
            c = "safe"
        else:
            c = "normal"
        if order.index(c) > order.index(worst):
            worst = c
    return worst


class PermissionPolicy:
    def __init__(self, mode: str = "ask", allow: list[str] | None = None, deny: list[str] | None = None):
        if mode not in MODES:
            raise ValueError(f"permission mode must be one of {MODES}")
        self.mode = mode
        self.allow = list(allow or [])          # extra command globs the user always allows
        self.deny = list(deny or [])            # command globs never allowed

    def decide(self, tool: str, args: dict) -> tuple[str, str]:
        """Returns (allow|ask|deny, reason)."""
        if tool in READ_TOOLS:
            return "allow", "read inside the workspace"
        if tool == "delete_file":
            return "ask", "deleting a file always needs approval"
        if tool in WRITE_TOOLS:
            if self.mode == "ask":
                return "ask", "mode Ask Before Action: every change needs approval"
            return "allow", "change inside the workspace (checkpoint saved)"
        if tool == "run_command":
            cmd = str(args.get("command", ""))
            if any(fnmatch.fnmatch(cmd, g) for g in self.deny):
                return "deny", "command matches a deny rule"
            kind = classify_command(cmd)
            if kind == "destructive":
                return "ask", "destructive command: always needs approval"
            if kind == "sensitive":
                return "ask", "installs software or reaches the network: needs approval"
            if any(fnmatch.fnmatch(cmd, g) for g in self.allow):
                return "allow", "command matches an allow rule"
            if self.mode == "ask":
                return "ask", "mode Ask Before Action: every command needs approval"
            if self.mode == "safe":
                return ("allow", "known-safe command") if kind == "safe" else ("ask", "not on the safe list")
            return "allow", "mode Autonomous With Guardrails"
        return "ask", f"unknown tool {tool}"


class Workspace:
    """Tools bound to one folder. Writes save a checkpoint under ``checkpoint_root/<job_id>``."""

    def __init__(self, root: Path | str, *, env: dict | None = None, shell: str | None = None,
                 python: str | None = None, checkpoint_root: Path | None = None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ToolError(f"workspace folder does not exist: {self.root}")
        self.env = {str(k): str(v) for k, v in (env or {}).items()}
        self.shell, self.python = shell, python
        self.checkpoint_root = Path(checkpoint_root) if checkpoint_root else None

    # ------------------------------------------------------------------ paths
    def resolve(self, rel: str | None) -> Path:
        rel = (rel or ".").strip()
        p = (self.root / rel).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path outside the workspace refused: {rel}")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    # ------------------------------------------------------------------ checkpoints
    def checkpoint(self, job_id: str, p: Path) -> str | None:
        if self.checkpoint_root is None:
            return None
        d = self.checkpoint_root / job_id
        d.mkdir(parents=True, exist_ok=True)
        manifest = d / "manifest.txt"
        rel = self.rel(p)
        seen = manifest.read_text(encoding="utf-8").splitlines() if manifest.exists() else []
        if any(line.split("\t", 1)[1] == rel for line in seen if "\t" in line):
            return str(d)                         # first version of this file in this job is already saved
        if p.exists():
            dst = d / "files" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            state = "existed"
        else:
            state = "new"
        with manifest.open("a", encoding="utf-8") as f:
            f.write(f"{state}\t{rel}\n")
        return str(d)

    def revert(self, job_id: str) -> list[str]:
        """Restore every file a job changed to its state before the job. Returns the paths restored."""
        if self.checkpoint_root is None:
            return []
        d = self.checkpoint_root / job_id
        manifest = d / "manifest.txt"
        if not manifest.exists():
            return []
        out = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            state, rel = line.split("\t", 1)
            target = self.resolve(rel)
            if state == "existed":
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(d / "files" / rel, target)
            elif target.exists():
                target.unlink()
            out.append(rel)
        return out

    # ------------------------------------------------------------------ previews (shown in approvals)
    def preview(self, tool: str, args: dict) -> dict:
        try:
            if tool == "write_file":
                p = self.resolve(args.get("path"))
                old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
                return {"path": self.rel(p), "diff": unified(old, str(args.get("content", "")), self.rel(p)), "new": not p.exists()}
            if tool == "edit_file":
                p = self.resolve(args.get("path"))
                old = p.read_text(encoding="utf-8", errors="replace")
                new = old.replace(str(args.get("old_text", "")), str(args.get("new_text", "")), 1)
                return {"path": self.rel(p), "diff": unified(old, new, self.rel(p))}
            if tool == "delete_file":
                return {"path": self.rel(self.resolve(args.get("path")))}
            if tool == "run_command":
                return {"command": args.get("command"), "cwd": str(self.root), "class": classify_command(str(args.get("command", "")))}
        except (ToolError, OSError) as e:
            return {"error": str(e)}
        return {}

    # ------------------------------------------------------------------ tools
    def call(self, tool: str, args: dict, *, job_id: str = "manual", cancel: Any = None) -> dict:
        """Returns {"ok", "text", ...extra (diff, path, exit_code)}. Raises nothing for tool errors."""
        try:
            fn = getattr(self, "t_" + tool)
        except AttributeError:
            return {"ok": False, "text": f"unknown tool {tool}"}
        try:
            return fn(job_id=job_id, cancel=cancel, **{k: v for k, v in args.items() if not k.startswith("_")})
        except TypeError as e:
            return {"ok": False, "text": f"bad arguments for {tool}: {e}"}
        except (ToolError, OSError, UnicodeDecodeError) as e:
            return {"ok": False, "text": str(e)}

    def t_list_dir(self, path: str = ".", depth: int = 1, **_: Any) -> dict:
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"not a folder: {path}")
        lines: list[str] = []

        def walk(d: Path, level: int) -> None:
            for c in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
                if c.name in SKIP_DIRS:
                    continue
                lines.append("  " * (level - 1) + c.name + ("/" if c.is_dir() else ""))
                if len(lines) > 800:
                    return
                if c.is_dir() and level < int(depth or 1):
                    walk(c, level + 1)
        walk(base, 1)
        return {"ok": True, "text": "\n".join(lines) or "(empty)"}

    def t_read_file(self, path: str, start_line: int | None = None, end_line: int | None = None, **_: Any) -> dict:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        text = p.read_text(encoding="utf-8", errors="replace")
        if start_line or end_line:
            lines = text.splitlines()
            a, b = max(1, int(start_line or 1)), min(len(lines), int(end_line or len(lines)))
            text = "\n".join(f"{i}: {lines[i - 1]}" for i in range(a, b + 1))
        if len(text) > MAX_READ:
            text = text[:MAX_READ] + f"\n... [truncated, {len(text)} chars total]"
        return {"ok": True, "text": text, "path": self.rel(p)}

    def t_search_files(self, pattern: str, glob: str | None = None, **_: Any) -> dict:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"bad pattern: {e}")
        hits: list[str] = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if glob and not fnmatch.fnmatch(f, glob):
                    continue
                fp = Path(dirpath) / f
                try:
                    if fp.stat().st_size > 2_000_000:
                        continue
                    for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if rx.search(line):
                            hits.append(f"{self.rel(fp)}:{i}: {line.strip()[:200]}")
                            if len(hits) >= 300:
                                return {"ok": True, "text": "\n".join(hits) + "\n... [first 300 matches]"}
                except OSError:
                    continue
        return {"ok": True, "text": "\n".join(hits) or "no matches"}

    def t_write_file(self, path: str, content: str, job_id: str = "manual", **_: Any) -> dict:
        p = self.resolve(path)
        existed = p.exists()
        old = p.read_text(encoding="utf-8", errors="replace") if existed else ""
        ck = self.checkpoint(job_id, p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "text": f"wrote {self.rel(p)} ({len(content)} chars)", "path": self.rel(p),
                "checkpoint": ck, **patch_details(old, content, self.rel(p), created=not existed)}

    def t_edit_file(self, path: str, old_text: str, new_text: str, job_id: str = "manual", **_: Any) -> dict:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        old = p.read_text(encoding="utf-8", errors="replace")
        n = old.count(old_text)
        if n != 1:
            raise ToolError(f"old_text must occur exactly once in {path} (found {n})")
        new = old.replace(old_text, new_text, 1)
        ck = self.checkpoint(job_id, p)
        p.write_text(new, encoding="utf-8")
        return {"ok": True, "text": f"edited {self.rel(p)}", "path": self.rel(p), "checkpoint": ck,
                **patch_details(old, new, self.rel(p))}

    def t_delete_file(self, path: str, job_id: str = "manual", **_: Any) -> dict:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        old = p.read_text(encoding="utf-8", errors="replace")
        ck = self.checkpoint(job_id, p)
        p.unlink()
        return {"ok": True, "text": f"deleted {self.rel(p)}", "path": self.rel(p), "checkpoint": ck,
                **patch_details(old, "", self.rel(p), deleted=True)}

    def command_env(self) -> dict:
        env = dict(os.environ)
        env.update(self.env)
        if self.python:
            py = Path(self.python)
            bindir = py.parent if py.is_file() else py
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            if bindir.name.lower() in ("scripts", "bin"):
                env["VIRTUAL_ENV"] = str(bindir.parent)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def t_run_command(self, command: str, timeout: int = 120, cancel: Any = None, **_: Any) -> dict:
        argv: Any
        if self.shell == "powershell":
            argv, shell = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command], False
        elif self.shell == "bash":
            argv, shell = ["bash", "-lc", command], False
        else:
            argv, shell = command, True
        flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
        t0 = time.monotonic()
        proc = subprocess.Popen(argv, shell=shell, cwd=self.root, env=self.command_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=flags,
                                start_new_session=(os.name != "nt"))
        chunks: list[bytes] = []
        reader = threading.Thread(target=lambda: chunks.extend(iter(lambda: proc.stdout.read(4096), b"")), daemon=True)
        reader.start()
        status = "exited"
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                status = "canceled"
                _stop_tree(proc)
                break
            if time.monotonic() - t0 > int(timeout):
                status = "timeout"
                _stop_tree(proc)
                break
            time.sleep(0.1)
        reader.join(timeout=5)
        out = b"".join(chunks).decode("utf-8", errors="replace")
        if len(out) > MAX_OUT:
            out = out[:MAX_OUT // 2] + "\n... [output truncated] ...\n" + out[-MAX_OUT // 2:]
        code = proc.returncode
        head = f"exit code {code}" if status == "exited" else f"{status} after {time.monotonic() - t0:.0f}s"
        return {"ok": status == "exited" and code == 0, "text": f"$ {command}\n{head}\n{out}", "exit_code": code,
                "status": status, "seconds": round(time.monotonic() - t0, 2)}


def _stop_tree(proc: subprocess.Popen) -> None:
    """Stop only the process this tool started (and its children) — never anything else."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15,
                           creationflags=0x08000000)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


def _unified_lines(old: str, new: str, path: str):
    return difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                               fromfile=f"a/{path}", tofile=f"b/{path}")


def _diff_text(lines) -> str:
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)[:60_000]


def unified(old: str, new: str, path: str) -> str:
    return _diff_text(_unified_lines(old, new, path))


def patch_details(old: str, new: str, path: str, *, created: bool = False, deleted: bool = False) -> dict:
    lines = list(_unified_lines(old, new, path))
    action = "created" if created else "deleted" if deleted else "edited" if old != new else None
    return {"diff": _diff_text(lines), "file_change": action,
            "added": sum(line.startswith("+") for line in lines[2:]),
            "removed": sum(line.startswith("-") for line in lines[2:])}

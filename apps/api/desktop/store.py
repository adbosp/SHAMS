"""Desktop client persistence (ADR-122): conversations, messages, jobs, approvals, local workspaces.

One SQLite file per SHAMS workspace (``<workspace>/_desktop/desktop.db``). Autonomous projects keep their own
event store; this file only holds what the desktop client adds on top: chat history, job history,
tool-approval records and the user's local folders ("Projects": a folder + environment on this PC).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'chat', preset TEXT,
  workspace_id TEXT, permission_mode TEXT NOT NULL DEFAULT 'ask', pinned INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, blocks TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'done', model TEXT, preset TEXT, job_id TEXT, usage TEXT NOT NULL DEFAULT '{}',
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, created);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, conversation_id TEXT, message_id TEXT, preset TEXT, model TEXT,
  status TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '{}', error TEXT,
  created REAL NOT NULL, started REAL, finished REAL
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, conversation_id TEXT, tool TEXT NOT NULL, arguments TEXT NOT NULL,
  reason TEXT, preview TEXT, status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, decided REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, env TEXT NOT NULL DEFAULT '{}',
  shell TEXT, python TEXT, notes TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, conversation_id TEXT, tool TEXT NOT NULL,
  arguments TEXT NOT NULL, decision TEXT NOT NULL, result TEXT, checkpoint TEXT, ts REAL NOT NULL
);
"""

JSON_COLS = {"blocks", "usage", "params", "result", "arguments", "env", "meta", "preview"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class DesktopStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # jobs that were running when the process stopped never finished: say so instead of showing "running"
        self.conn.execute("UPDATE jobs SET status='interrupted', finished=? WHERE status IN ('queued','running','waiting')",
                          (time.time(),))
        self.conn.execute("UPDATE messages SET status='interrupted' WHERE status='streaming'")
        self.conn.execute("UPDATE approvals SET status='expired' WHERE status='pending'")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_COLS & d.keys():
            if isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except json.JSONDecodeError:
                    pass
        return d

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self.conn.execute(sql, args).fetchall()]

    def q1(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            return self._row(self.conn.execute(sql, args).fetchone())

    def x(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self.conn.execute(sql, args)

    @staticmethod
    def _j(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False)

    # ------------------------------------------------------------------ conversations
    def create_conversation(self, title: str = "Cuộc trò chuyện mới", mode: str = "chat", preset: str | None = None,
                            workspace_id: str | None = None, permission_mode: str = "ask", meta: dict | None = None) -> dict:
        cid, now = new_id("conv"), time.time()
        self.x("INSERT INTO conversations (id,title,mode,preset,workspace_id,permission_mode,created,updated,meta) "
               "VALUES (?,?,?,?,?,?,?,?,?)", (cid, title, mode, preset, workspace_id, permission_mode, now, now,
                                              self._j(meta or {})))
        return self.conversation(cid)

    def conversation(self, cid: str) -> dict | None:
        return self.q1("SELECT * FROM conversations WHERE id=?", (cid,))

    def conversations(self, query: str = "", include_archived: bool = False, limit: int = 200) -> list[dict]:
        sql = "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count FROM conversations c"
        where, args = [], []
        if not include_archived:
            where.append("archived=0")
        if query:
            where.append("(title LIKE ? OR id IN (SELECT conversation_id FROM messages WHERE blocks LIKE ?))")
            args += [f"%{query}%", f"%{query}%"]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY pinned DESC, updated DESC LIMIT ?"
        return self.q(sql, tuple(args) + (limit,))

    def update_conversation(self, cid: str, **fields: Any) -> dict | None:
        allowed = {"title", "mode", "preset", "workspace_id", "permission_mode", "pinned", "archived", "meta"}
        sets = {k: (self._j(v) if k == "meta" else v) for k, v in fields.items() if k in allowed}
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self.x(f"UPDATE conversations SET {cols}, updated=? WHERE id=?", tuple(sets.values()) + (time.time(), cid))
        return self.conversation(cid)

    def delete_conversation(self, cid: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
            self.conn.execute("DELETE FROM conversations WHERE id=?", (cid,))

    def touch(self, cid: str) -> None:
        self.x("UPDATE conversations SET updated=? WHERE id=?", (time.time(), cid))

    # ------------------------------------------------------------------ messages
    def add_message(self, cid: str, role: str, blocks: list[dict], *, status: str = "done", model: str | None = None,
                    preset: str | None = None, job_id: str | None = None) -> dict:
        mid = new_id("msg")
        self.x("INSERT INTO messages (id,conversation_id,role,blocks,status,model,preset,job_id,created) "
               "VALUES (?,?,?,?,?,?,?,?,?)", (mid, cid, role, self._j(blocks), status, model, preset, job_id, time.time()))
        self.touch(cid)
        return self.message(mid)

    def message(self, mid: str) -> dict | None:
        return self.q1("SELECT * FROM messages WHERE id=?", (mid,))

    def messages(self, cid: str) -> list[dict]:
        return self.q("SELECT * FROM messages WHERE conversation_id=? ORDER BY created", (cid,))

    def update_message(self, mid: str, *, blocks: list[dict] | None = None, status: str | None = None,
                       usage: dict | None = None, model: str | None = None) -> None:
        sets: dict[str, Any] = {}
        if blocks is not None:
            sets["blocks"] = self._j(blocks)
        if status is not None:
            sets["status"] = status
        if usage is not None:
            sets["usage"] = self._j(usage)
        if model is not None:
            sets["model"] = model
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self.x(f"UPDATE messages SET {cols} WHERE id=?", tuple(sets.values()) + (mid,))

    # ------------------------------------------------------------------ jobs
    def create_job(self, kind: str, *, conversation_id: str | None = None, message_id: str | None = None,
                   preset: str | None = None, model: str | None = None, params: dict | None = None) -> dict:
        jid = new_id("job")
        self.x("INSERT INTO jobs (id,kind,conversation_id,message_id,preset,model,status,params,created) "
               "VALUES (?,?,?,?,?,?,?,?,?)", (jid, kind, conversation_id, message_id, preset, model, "queued",
                                              self._j(params or {}), time.time()))
        return self.job(jid)

    def job(self, jid: str) -> dict | None:
        return self.q1("SELECT * FROM jobs WHERE id=?", (jid,))

    def jobs(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        if kind:
            return self.q("SELECT * FROM jobs WHERE kind=? ORDER BY created DESC LIMIT ?", (kind, limit))
        return self.q("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))

    def update_job(self, jid: str, **fields: Any) -> None:
        allowed = {"status", "result", "error", "started", "finished", "model"}
        sets = {k: (self._j(v) if k == "result" else v) for k, v in fields.items() if k in allowed}
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self.x(f"UPDATE jobs SET {cols} WHERE id=?", tuple(sets.values()) + (jid,))

    # ------------------------------------------------------------------ approvals / tool log
    def create_approval(self, job_id: str, conversation_id: str | None, tool: str, arguments: dict, reason: str,
                        preview: Any = None) -> dict:
        aid = new_id("appr")
        self.x("INSERT INTO approvals (id,job_id,conversation_id,tool,arguments,reason,preview,created) VALUES (?,?,?,?,?,?,?,?)",
               (aid, job_id, conversation_id, tool, self._j(arguments), reason, self._j(preview), time.time()))
        return self.approval(aid)

    def approval(self, aid: str) -> dict | None:
        return self.q1("SELECT * FROM approvals WHERE id=?", (aid,))

    def approvals(self, status: str | None = "pending") -> list[dict]:
        if status:
            return self.q("SELECT * FROM approvals WHERE status=? ORDER BY created", (status,))
        return self.q("SELECT * FROM approvals ORDER BY created DESC LIMIT 200")

    def decide_approval(self, aid: str, status: str, note: str = "") -> None:
        self.x("UPDATE approvals SET status=?, decided=?, note=? WHERE id=? AND status='pending'",
               (status, time.time(), note, aid))

    def log_tool(self, job_id: str | None, cid: str | None, tool: str, arguments: dict, decision: str,
                 result: str | None = None, checkpoint: str | None = None) -> None:
        self.x("INSERT INTO tool_log (job_id,conversation_id,tool,arguments,decision,result,checkpoint,ts) VALUES (?,?,?,?,?,?,?,?)",
               (job_id, cid, tool, self._j(arguments), decision, (result or "")[:4000], checkpoint, time.time()))

    def tool_log(self, conversation_id: str | None = None, limit: int = 200) -> list[dict]:
        if conversation_id:
            return self.q("SELECT * FROM tool_log WHERE conversation_id=? ORDER BY seq DESC LIMIT ?", (conversation_id, limit))
        return self.q("SELECT * FROM tool_log ORDER BY seq DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ workspaces (local folders)
    def create_workspace(self, name: str, path: str, env: dict | None = None, shell: str | None = None,
                         python: str | None = None, notes: str = "") -> dict:
        wid, now = new_id("ws"), time.time()
        self.x("INSERT INTO workspaces (id,name,path,env,shell,python,notes,created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
               (wid, name, path, self._j(env or {}), shell, python, notes, now, now))
        return self.workspace(wid)

    def workspace(self, wid: str) -> dict | None:
        return self.q1("SELECT * FROM workspaces WHERE id=?", (wid,))

    def workspaces(self) -> list[dict]:
        return self.q("SELECT * FROM workspaces ORDER BY updated DESC")

    def update_workspace(self, wid: str, **fields: Any) -> dict | None:
        allowed = {"name", "path", "env", "shell", "python", "notes"}
        sets = {k: (self._j(v) if k == "env" else v) for k, v in fields.items() if k in allowed and v is not None}
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self.x(f"UPDATE workspaces SET {cols}, updated=? WHERE id=?", tuple(sets.values()) + (time.time(), wid))
        return self.workspace(wid)

    def delete_workspace(self, wid: str) -> None:
        """Forget the folder (files on disk are never touched)."""
        self.x("DELETE FROM workspaces WHERE id=?", (wid,))
        self.x("UPDATE conversations SET workspace_id=NULL WHERE workspace_id=?", (wid,))

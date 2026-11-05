"""Event Store: append-only event log is the single source of truth (ADR-104).

Projections (tasks, artifacts, leases, ...) are updated in the SAME transaction as the
event that changes them. Only the Studio Core process writes this database.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from studio import contracts, statemachine
from studio.statemachine import S

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
  type TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, actor_json TEXT NOT NULL,
  from_state TEXT, to_state TEXT, payload_json TEXT, idempotency_key TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(project_id TEXT PRIMARY KEY, goal TEXT, mode TEXT, engine TEXT,
  engine_version TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS plans(plan_id TEXT, project_id TEXT, plan_version INTEGER, template TEXT,
  dag_json TEXT, approved_by TEXT, created_at TEXT, PRIMARY KEY(plan_id, plan_version));
CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, project_id TEXT, plan_id TEXT, state TEXT NOT NULL,
  capability TEXT, priority INTEGER, attempt INTEGER, max_attempts INTEGER, task_json TEXT NOT NULL,
  ready_since TEXT, updated_seq INTEGER);
CREATE TABLE IF NOT EXISTS task_deps(task_id TEXT, depends_on_task_id TEXT, PRIMARY KEY(task_id, depends_on_task_id));
CREATE TABLE IF NOT EXISTS artifacts(sha256 TEXT, project_id TEXT, logical_id TEXT, version INTEGER, state TEXT,
  media_type TEXT, contract TEXT, storage TEXT, store_path TEXT, produced_task_id TEXT, produced_attempt INTEGER,
  artifact_json TEXT, created_at TEXT, PRIMARY KEY(sha256, project_id));
CREATE TABLE IF NOT EXISTS handoffs(task_id TEXT, attempt INTEGER, status TEXT, handoff_json TEXT, ts TEXT,
  PRIMARY KEY(task_id, attempt));
CREATE TABLE IF NOT EXISTS handoff_inputs(task_id TEXT, attempt INTEGER, sha256 TEXT);
CREATE TABLE IF NOT EXISTS qc_reports(task_id TEXT, attempt INTEGER, gate TEXT, verdict TEXT,
  stopped_at_stage INTEGER, report_json TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS failures(failure_id TEXT PRIMARY KEY, task_id TEXT, attempt INTEGER, failure_class TEXT,
  signature TEXT, severity TEXT, record_json TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS leases(lease_id TEXT PRIMARY KEY, task_id TEXT, attempt INTEGER, node_id TEXT, lane TEXT,
  worker_id TEXT, granted_at TEXT, expires_at TEXT, released_at TEXT);
CREATE TABLE IF NOT EXISTS models(lane TEXT PRIMARY KEY, worker_id TEXT, model TEXT, loaded_at TEXT);
CREATE TABLE IF NOT EXISTS side_effects(idempotency_key TEXT PRIMARY KEY, task_id TEXT, attempt INTEGER,
  action TEXT, status TEXT, result_json TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS metrics(task_id TEXT, attempt INTEGER, key TEXT, value REAL, ts TEXT);
"""


def actor_ref(actor: str, kind: str = "tool") -> dict:
    return {"worker_id": actor, "kind": kind}


class EventStore:
    def __init__(self, path: Path | str, project_id: str):
        self.path = str(path)
        self.project_id = project_id
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._lock = threading.RLock()
        self._depth = 0

    # ---------------------------------------------------------------- transactions
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def q1(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- events
    def append(self, type_: str, subject_kind: str, subject_id: str, actor: str, *,
               from_state: str | None = None, to_state: str | None = None,
               payload: dict | None = None, idempotency_key: str | None = None) -> int:
        ev = {
            "seq": 1, "event_id": str(uuid.uuid4()), "project_id": self.project_id, "type": type_,
            "subject": {"kind": subject_kind, "id": subject_id}, "actor": actor_ref(actor),
            "payload": payload or {}, "timestamp": contracts.now_iso(),
        }
        if from_state:
            ev["from_state"] = from_state
        if to_state:
            ev["to_state"] = to_state
        if idempotency_key:
            ev["idempotency_key"] = idempotency_key
        contracts.validate("event", ev)
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO events(event_id, project_id, type, subject_kind, subject_id, actor_json, from_state,"
                " to_state, payload_json, idempotency_key, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ev["event_id"], self.project_id, type_, subject_kind, subject_id, json.dumps(ev["actor"]),
                 from_state, to_state, json.dumps(ev["payload"], ensure_ascii=False), idempotency_key,
                 ev["timestamp"]))
            return cur.lastrowid

    def events(self, since_seq: int = 0, subject_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE seq > ?", [since_seq]
        if subject_id:
            sql += " AND subject_id = ?"
            args.append(subject_id)
        return [dict(r) for r in self.q(sql + " ORDER BY seq", tuple(args))]

    # ---------------------------------------------------------------- tasks
    def create_task(self, task: dict, plan_id: str, actor: str) -> None:
        contracts.validate("task", task)
        deps = task.get("depends_on", [])
        with self.tx() as c:
            state = S.READY if all(self._state(d) == S.ACCEPTED for d in deps) else S.PENDING
            statemachine.check(None, state, "task.created", actor)
            task = {**task, "state": str(state)}
            seq = self.append("task.created", "task", task["task_id"], actor, to_state=state,
                              payload={"plan_id": plan_id})
            c.execute(
                "INSERT INTO tasks(task_id, project_id, plan_id, state, capability, priority, attempt, max_attempts,"
                " task_json, ready_since, updated_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task["task_id"], self.project_id, plan_id, state, task["capability"], task.get("priority", 50),
                 task.get("attempt", 0), task.get("max_attempts", 3), json.dumps(task, ensure_ascii=False),
                 contracts.now_iso() if state == S.READY else None, seq))
            c.executemany("INSERT OR IGNORE INTO task_deps VALUES (?,?)", [(task["task_id"], d) for d in deps])

    def _state(self, task_id: str) -> S | None:
        r = self.q1("SELECT state FROM tasks WHERE task_id=?", (task_id,))
        return S(r["state"]) if r else None

    def state(self, task_id: str) -> S:
        st = self._state(task_id)
        if st is None:
            raise KeyError(task_id)
        return st

    def transition(self, task_id: str, to: S, event_type: str, actor: str, payload: dict | None = None,
                   *, expect: S | set[S] | None = None, attempt_delta: int = 0) -> int:
        with self.tx() as c:
            frm = self.state(task_id)
            if expect is not None and frm not in (expect if isinstance(expect, set) else {expect}):
                raise statemachine.TransitionError(f"{task_id}: expected {expect}, is {frm}")
            statemachine.check(frm, to, event_type, actor)
            seq = self.append(event_type, "task", task_id, actor, from_state=frm, to_state=to, payload=payload)
            c.execute(
                "UPDATE tasks SET state=?, attempt=attempt+?, updated_seq=?,"
                " ready_since=CASE WHEN ?='READY' THEN ? ELSE ready_since END WHERE task_id=?",
                (to, attempt_delta, seq, to, contracts.now_iso(), task_id))
            return seq

    def task(self, task_id: str) -> dict:
        r = self.q1("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if r is None:
            raise KeyError(task_id)
        t = json.loads(r["task_json"])
        t["state"] = r["state"]
        t["attempt"] = r["attempt"]
        return t

    def tasks(self, states: set[S] | None = None) -> list[dict]:
        rows = self.q("SELECT task_id FROM tasks ORDER BY rowid")
        out = [self.task(r["task_id"]) for r in rows]
        return [t for t in out if states is None or S(t["state"]) in states]

    def task_row(self, task_id: str) -> sqlite3.Row:
        return self.q1("SELECT * FROM tasks WHERE task_id=?", (task_id,))

    def deps(self, task_id: str) -> list[str]:
        return [r[0] for r in self.q("SELECT depends_on_task_id FROM task_deps WHERE task_id=?", (task_id,))]

    def dependents(self, task_id: str) -> list[str]:
        return [r[0] for r in self.q("SELECT task_id FROM task_deps WHERE depends_on_task_id=?", (task_id,))]

    def update_task_json(self, task_id: str, patch: dict) -> None:
        t = self.task(task_id)
        t.update(patch)
        contracts.validate("task", t)
        with self.tx() as c:
            c.execute("UPDATE tasks SET task_json=?, priority=? WHERE task_id=?",
                      (json.dumps(t, ensure_ascii=False), t.get("priority", 50), task_id))

    # ---------------------------------------------------------------- handoffs / qc / failures
    def save_handoff(self, h: dict) -> None:
        contracts.validate("handoff", h)
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO handoffs VALUES (?,?,?,?,?)",
                      (h["task_id"], h["attempt"], h["status"], json.dumps(h, ensure_ascii=False), h["timestamp"]))
            c.execute("DELETE FROM handoff_inputs WHERE task_id=? AND attempt=?", (h["task_id"], h["attempt"]))
            c.executemany("INSERT INTO handoff_inputs VALUES (?,?,?)",
                          [(h["task_id"], h["attempt"], i["sha256"]) for i in h["inputs"]])
            self.append("handoff.saved", "task", h["task_id"], "task_runner",
                        payload={"attempt": h["attempt"], "status": h["status"]})

    def handoff(self, task_id: str, attempt: int | None = None) -> dict | None:
        if attempt is None:
            r = self.q1("SELECT handoff_json FROM handoffs WHERE task_id=? ORDER BY attempt DESC LIMIT 1", (task_id,))
        else:
            r = self.q1("SELECT handoff_json FROM handoffs WHERE task_id=? AND attempt=?", (task_id, attempt))
        return json.loads(r[0]) if r else None

    def save_qc(self, rep: dict) -> None:
        contracts.validate("qc_report", rep)
        with self.tx() as c:
            c.execute("INSERT INTO qc_reports VALUES (?,?,?,?,?,?,?)",
                      (rep["task_id"], rep["attempt"], rep["gate"], rep["verdict"], rep.get("stopped_at_stage"),
                       json.dumps(rep, ensure_ascii=False), rep["timestamp"]))

    def qc_reports(self, task_id: str) -> list[dict]:
        return [json.loads(r[0]) for r in
                self.q("SELECT report_json FROM qc_reports WHERE task_id=? ORDER BY rowid", (task_id,))]

    def save_failure(self, f: dict) -> None:
        contracts.validate("failure_record", f)
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO failures VALUES (?,?,?,?,?,?,?,?)",
                      (f["failure_id"], f["task_id"], f["attempt"], f["failure_class"], f["signature"],
                       f["severity"], json.dumps(f, ensure_ascii=False), f["timestamp"]))

    def failures(self, task_id: str | None = None) -> list[dict]:
        if task_id:
            rows = self.q("SELECT record_json FROM failures WHERE task_id=? ORDER BY rowid", (task_id,))
        else:
            rows = self.q("SELECT record_json FROM failures ORDER BY rowid")
        return [json.loads(r[0]) for r in rows]

    def metric(self, task_id: str, attempt: int, key: str, value: float) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO metrics VALUES (?,?,?,?,?)", (task_id, attempt, key, value, contracts.now_iso()))

    # ---------------------------------------------------------------- side-effect ledger (ADR-111)
    def side_effect(self, key: str, task_id: str, attempt: int, action: str, fn) -> Any:
        r = self.q1("SELECT status, result_json FROM side_effects WHERE idempotency_key=?", (key,))
        if r and r["status"] == "COMPLETED":
            return json.loads(r["result_json"])
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO side_effects VALUES (?,?,?,?,?,?,?)",
                      (key, task_id, attempt, action, "STARTED", None, contracts.now_iso()))
            self.append("side_effect.started", "side_effect", key, "task_runner", idempotency_key=key)
        result = fn()
        with self.tx() as c:
            c.execute("UPDATE side_effects SET status='COMPLETED', result_json=? WHERE idempotency_key=?",
                      (json.dumps(result), key))
            self.append("side_effect.completed", "side_effect", key, "task_runner", idempotency_key=key)
        return result

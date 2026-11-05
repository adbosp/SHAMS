"""Content-addressed Artifact Registry (ADR-105) and Dependency Impact Analyzer (ADR-110)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from studio import contracts
from studio.events.store import EventStore
from studio.statemachine import S


class ArtifactRegistry:
    def __init__(self, store: EventStore, root: Path):
        self.db = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def blob_path(self, sha: str) -> Path:
        return self.root / "sha256" / sha[:2] / sha

    def put(self, src: Path, *, logical_id: str, media_type: str, contract: str | None, task_id: str,
            attempt: int, worker: dict, rel_path: str | None = None,
            derived_from: list[dict] | None = None, metadata: dict | None = None) -> dict:
        sha = contracts.sha256_file(src)
        dst = self.blob_path(sha)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(".tmp")
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
        # Identity is (content hash, logical_id): two outputs may legitimately have identical bytes.
        existing = self.db.q1("SELECT artifact_json FROM artifacts WHERE sha256=? AND project_id=? AND logical_id=?",
                              (sha, self.db.project_id, logical_id))
        if existing:
            return self.ref(json.loads(existing[0]), rel_path)
        version = (self.db.q1("SELECT COALESCE(MAX(version),0) FROM artifacts WHERE logical_id=?",
                              (logical_id,))[0]) + 1
        art = {
            "schema_version": contracts.SCHEMA_VERSION, "project_id": self.db.project_id,
            "logical_id": logical_id, "version": version, "sha256": sha, "media_type": media_type,
            "size_bytes": dst.stat().st_size, "storage": "artifact_store",
            "store_path": str(dst.relative_to(self.root)), "state": "CANDIDATE",
            "produced_by": {"task_id": task_id, "attempt": attempt, "worker": worker},
            "derived_from": derived_from or [], "metadata": {**(metadata or {}), "path": rel_path},
            "created_at": contracts.now_iso(),
        }
        if contract:
            art["contract"] = contract
        contracts.validate("artifact", art)
        with self.db.tx() as c:
            c.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sha, self.db.project_id, logical_id, version, "CANDIDATE", media_type, contract,
                       "artifact_store", art["store_path"], task_id, attempt, json.dumps(art, ensure_ascii=False),
                       art["created_at"]))
            self.db.append("artifact.created", "artifact", sha, "task_runner", to_state="CANDIDATE",
                           payload={"logical_id": logical_id, "version": version})
        return self.ref(art, rel_path)

    @staticmethod
    def ref(art: dict, rel_path: str | None = None) -> dict:
        r = {"logical_id": art["logical_id"], "sha256": art["sha256"], "media_type": art["media_type"]}
        p = rel_path or art.get("metadata", {}).get("path")
        if p:
            r["path"] = p
        return r

    def get(self, sha: str, logical_id: str | None = None) -> dict | None:
        if logical_id is None:
            r = self.db.q1("SELECT artifact_json, state FROM artifacts WHERE sha256=? ORDER BY rowid DESC", (sha,))
        else:
            r = self.db.q1("SELECT artifact_json, state FROM artifacts WHERE sha256=? AND logical_id=?",
                           (sha, logical_id))
        if not r:
            return None
        a = json.loads(r[0])
        a["state"] = r[1]
        return a

    def _set_state(self, sha: str, state: str, actor: str, logical_id: str | None = None) -> None:
        a = self.get(sha, logical_id)
        with self.db.tx() as c:
            c.execute("UPDATE artifacts SET state=? WHERE sha256=? AND logical_id=?", (state, sha, a["logical_id"]))
            self.db.append(f"artifact.{state.lower()}", "artifact", sha, actor, from_state=a["state"], to_state=state)

    def accepted(self, logical_id: str) -> dict | None:
        r = self.db.q1("SELECT artifact_json FROM artifacts WHERE logical_id=? AND state='ACCEPTED'", (logical_id,))
        return self.ref(json.loads(r[0])) if r else None

    def accept(self, sha: str, logical_id: str | None = None) -> list[str]:
        """Mark ACCEPTED; returns hashes it superseded (same logical_id)."""
        a = self.get(sha, logical_id)
        if a["state"] == "ACCEPTED":
            return []
        lid = a["logical_id"]
        superseded = [r[0] for r in self.db.q(
            "SELECT sha256 FROM artifacts WHERE logical_id=? AND state IN ('ACCEPTED','STALE') AND sha256<>?",
            (lid, sha))]
        with self.db.tx():
            for old in superseded:
                self._set_state(old, "SUPERSEDED", "qc", lid)
            self._set_state(sha, "ACCEPTED", "qc", lid)
        return superseded

    def reject(self, sha: str, logical_id: str | None = None) -> None:
        if self.get(sha, logical_id)["state"] == "CANDIDATE":
            self._set_state(sha, "REJECTED", "qc", logical_id)

    def mark_stale(self, sha: str, logical_id: str | None = None) -> None:
        if self.get(sha, logical_id)["state"] == "ACCEPTED":
            self._set_state(sha, "STALE", "impact_analyzer", logical_id)


class ImpactAnalyzer:
    """ADR-110: tasks that consumed a superseded hash become STALE (revalidate before rework)."""

    def __init__(self, store: EventStore, registry: ArtifactRegistry):
        self.db = store
        self.reg = registry

    def on_superseded(self, old_hashes: list[str]) -> list[str]:
        stale: list[str] = []
        for old in old_hashes:
            rows = self.db.q("SELECT DISTINCT task_id, attempt FROM handoff_inputs WHERE sha256=?", (old,))
            for r in rows:
                tid = r["task_id"]
                if self.db.state(tid) != S.ACCEPTED or self.db.task(tid)["attempt"] != r["attempt"]:
                    continue
                with self.db.tx():
                    self.db.transition(tid, S.STALE, "task.invalidated", "impact_analyzer",
                                       {"superseded_input": old})
                    h = self.db.handoff(tid, r["attempt"])
                    for o in (h or {}).get("outputs", []):
                        self.reg.mark_stale(o["sha256"], o["logical_id"])
                stale.append(tid)
        return stale

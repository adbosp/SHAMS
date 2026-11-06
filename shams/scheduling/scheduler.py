"""Studio Scheduler: deterministic, no LLM (ADR-102, ADR-103, docs/design/04 §1)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from shams.events.store import EventStore
from shams.nodes.registry import Lane, NodeRegistry, Worker
from shams.statemachine import S


@dataclass
class SchedulerConfig:
    affinity_bonus: float = 30.0
    critical_path_max_bonus: float = 15.0
    max_wait_bonus_minutes: float = 60.0
    switch_cost_divisor: float = 10.0
    affinity_max_streak: int = 6
    max_wait_minutes: float = 20.0
    lease_ttl_factor: float = 1.5
    default_wall_seconds: int = 1200


@dataclass
class Assignment:
    task_id: str
    attempt: int
    worker: Worker
    lane: Lane
    lease_id: str
    score: float


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Scheduler:
    def __init__(self, store: EventStore, nodes: NodeRegistry, config: SchedulerConfig | None = None,
                 clock=lambda: datetime.now(timezone.utc)):
        self.db = store
        self.nodes = nodes
        self.cfg = config or SchedulerConfig()
        self.clock = clock
        self._streak: dict[str, tuple[str | None, int]] = {}  # lane -> (model, consecutive count)

    # ------------------------------------------------------------------ bookkeeping
    def loaded_model(self, lane_id: str) -> str | None:
        r = self.db.q1("SELECT model FROM models WHERE lane=?", (lane_id,))
        return r[0] if r else None

    def active_leases(self) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT * FROM leases WHERE released_at IS NULL")]

    def lane_usage(self) -> dict[str, int]:
        use: dict[str, int] = {}
        for l in self.active_leases():
            use[l["lane"]] = use.get(l["lane"], 0) + 1
        return use

    def promote_ready(self) -> list[str]:
        promoted = []
        for t in self.db.tasks({S.PENDING}):
            if all(self.db.state(d) == S.ACCEPTED for d in t.get("depends_on", [])):
                self.db.transition(t["task_id"], S.READY, "task.unblocked", "scheduler")
                promoted.append(t["task_id"])
        return promoted

    def expired_leases(self) -> list[dict]:
        now = self.clock()
        return [l for l in self.active_leases() if _parse(l["expires_at"]) < now]

    def release(self, lease_id: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE leases SET released_at=? WHERE lease_id=? AND released_at IS NULL",
                      (_iso(self.clock()), lease_id))
            self.db.append("lease.released", "lease", lease_id, "scheduler")

    # ------------------------------------------------------------------ scoring
    def _descendants(self, task_id: str, seen: set[str] | None = None) -> int:
        seen = seen if seen is not None else set()
        for d in self.db.dependents(task_id):
            if d not in seen:
                seen.add(d)
                self._descendants(d, seen)
        return len(seen)

    def score(self, task: dict, worker: Worker, lane: Lane, ready: list[dict]) -> float:
        now = self.clock()
        row = self.db.task_row(task["task_id"])
        # whole minutes only: sub-minute timing jitter must not reorder tasks (deterministic scheduling)
        wait_min = max(0, int((now - (_parse(row["ready_since"]) or now)).total_seconds() // 60))
        s = float(task.get("priority", 50)) + min(wait_min, self.cfg.max_wait_bonus_minutes)
        s += min(self.cfg.critical_path_max_bonus, 3.0 * self._descendants(task["task_id"]))
        s += 10.0 * (worker.quality(task["capability"]) - 0.5)
        if lane.kind == "gpu" and worker.model:
            loaded = self.loaded_model(lane.lane_id)
            if loaded == worker.model:
                model, streak = self._streak.get(lane.lane_id, (None, 0))
                starving = any(
                    (now - (_parse(self.db.task_row(o["task_id"])["ready_since"]) or now)).total_seconds() / 60
                    > self.cfg.max_wait_minutes
                    for o in ready if o["task_id"] != task["task_id"])
                if not (streak >= self.cfg.affinity_max_streak and starving):
                    s += self.cfg.affinity_bonus
            elif loaded is not None or worker.switch_cost_s:
                s -= worker.switch_cost_s / self.cfg.switch_cost_divisor
        return s

    # ------------------------------------------------------------------ main entry
    def tick(self) -> list[Assignment]:
        """Promote PENDING->READY, then lease as many READY tasks as lanes allow."""
        self.promote_ready()
        ready = self.db.tasks({S.READY})
        usage = self.lane_usage()
        capacity = {l.lane_id: l.capacity for l in self.nodes.lanes()}
        options = []
        for t in ready:
            for w in self.nodes.preferred(t):
                for lane in self.nodes.lanes_for(w):
                    options.append((self.score(t, w, lane, ready), t, w, lane))
        options.sort(key=lambda o: (-o[0], o[1]["task_id"]))
        out: list[Assignment] = []
        taken: set[str] = set()
        for score, t, w, lane in options:
            if t["task_id"] in taken or usage.get(lane.lane_id, 0) >= capacity.get(lane.lane_id, 0):
                continue
            out.append(self._lease(t, w, lane, score))
            taken.add(t["task_id"])
            usage[lane.lane_id] = usage.get(lane.lane_id, 0) + 1
        return out

    def _lease(self, t: dict, w: Worker, lane: Lane, score: float) -> Assignment:
        now = self.clock()
        wall = t.get("budget", {}).get("max_wall_seconds", self.cfg.default_wall_seconds)
        lease_id = f"lease-{uuid.uuid4().hex[:12]}"
        with self.db.tx() as c:
            self.db.transition(t["task_id"], S.LEASED, "task.leased", "scheduler",
                               {"lease_id": lease_id, "lane": lane.lane_id, "worker_id": w.worker_id,
                                "score": round(score, 2)}, expect=S.READY)
            c.execute("INSERT INTO leases VALUES (?,?,?,?,?,?,?,?,NULL)",
                      (lease_id, t["task_id"], t["attempt"], lane.node_id, lane.lane_id, w.worker_id, _iso(now),
                       _iso(now + timedelta(seconds=wall * self.cfg.lease_ttl_factor))))
            self.db.append("lease.granted", "lease", lease_id, "scheduler",
                           payload={"task_id": t["task_id"], "lane": lane.lane_id})
        if lane.kind == "gpu" and w.model:
            model, streak = self._streak.get(lane.lane_id, (None, 0))
            self._streak[lane.lane_id] = (w.model, streak + 1 if model == w.model else 1)
        return Assignment(t["task_id"], t["attempt"], w, lane, lease_id, score)

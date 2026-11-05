"""Unified task state machine (ADR-101, docs/design/02_State_Machine.md).

Every transition must be listed here; the Event Store refuses anything else.
"""
from __future__ import annotations

from enum import StrEnum


class S(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    QC_PENDING = "QC_PENDING"
    ACCEPTED = "ACCEPTED"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


TERMINAL = {S.FAILED, S.CANCELED}
ACTIVE = {S.LEASED, S.RUNNING}

# (from, to) -> (allowed event types, allowed actors)
_T: dict[tuple[S | None, S], tuple[set[str], set[str]]] = {
    (None, S.PENDING): ({"task.created"}, {"planner", "repair_router", "human"}),
    (None, S.READY): ({"task.created"}, {"planner", "repair_router", "human"}),
    (S.PENDING, S.READY): ({"task.unblocked"}, {"scheduler"}),
    (S.READY, S.PENDING): ({"task.reblocked"}, {"scheduler"}),
    (S.READY, S.LEASED): ({"task.leased"}, {"scheduler"}),
    (S.READY, S.BLOCKED): ({"task.blocked"}, {"scheduler"}),  # no node/worker provides the capability
    (S.LEASED, S.RUNNING): ({"task.started"}, {"task_runner"}),
    (S.LEASED, S.READY): ({"task.lease_released"}, {"scheduler", "task_runner"}),
    (S.RUNNING, S.QC_PENDING): ({"task.submitted"}, {"task_runner"}),
    (S.RUNNING, S.BLOCKED): ({"task.blocked"}, {"task_runner"}),
    (S.RUNNING, S.REPAIR_REQUIRED): ({"task.crashed", "task.worker_failed"}, {"scheduler", "task_runner"}),
    (S.LEASED, S.REPAIR_REQUIRED): ({"task.crashed"}, {"scheduler", "task_runner"}),
    (S.RUNNING, S.READY): ({"task.lease_released"}, {"scheduler"}),  # node lost; not counted as attempt
    (S.QC_PENDING, S.ACCEPTED): ({"qc.passed"}, {"qc"}),
    (S.QC_PENDING, S.REPAIR_REQUIRED): ({"qc.failed"}, {"qc"}),
    (S.QC_PENDING, S.HUMAN_REVIEW): ({"qc.not_verified"}, {"qc"}),
    (S.REPAIR_REQUIRED, S.READY): ({"repair.scheduled"}, {"repair_router"}),
    (S.REPAIR_REQUIRED, S.PENDING): ({"repair.scheduled"}, {"repair_router"}),
    (S.REPAIR_REQUIRED, S.HUMAN_REVIEW): ({"repair.exhausted"}, {"repair_router"}),
    (S.REPAIR_REQUIRED, S.BLOCKED): ({"repair.blocked"}, {"repair_router"}),
    (S.ACCEPTED, S.STALE): ({"task.invalidated"}, {"impact_analyzer"}),
    (S.STALE, S.QC_PENDING): ({"task.revalidate"}, {"scheduler"}),
    (S.BLOCKED, S.READY): ({"task.unblocked"}, {"scheduler", "human"}),
    (S.BLOCKED, S.PENDING): ({"task.unblocked"}, {"scheduler", "human"}),
}
for _to in (S.READY, S.ACCEPTED, S.FAILED, S.CANCELED):
    _T[(S.HUMAN_REVIEW, _to)] = ({"human.decided"}, {"human"})
for _from in S:
    if _from not in TERMINAL:
        _T.setdefault((_from, S.CANCELED), (set(), set()))
        _T[(_from, S.CANCELED)][0].add("task.canceled")
        _T[(_from, S.CANCELED)][1].update({"human", "planner"})


class TransitionError(RuntimeError):
    pass


def check(frm: S | str | None, to: S | str, event_type: str, actor: str) -> None:
    key = (S(frm) if frm else None, S(to))
    rule = _T.get(key)
    if rule is None:
        raise TransitionError(f"illegal transition {frm} -> {to}")
    events, actors = rule
    if event_type not in events:
        raise TransitionError(f"{frm} -> {to} requires event {sorted(events)}, got {event_type!r}")
    if actor not in actors:
        raise TransitionError(f"actor {actor!r} may not do {frm} -> {to} (allowed: {sorted(actors)})")


def allowed() -> dict[tuple[S | None, S], tuple[set[str], set[str]]]:
    return dict(_T)

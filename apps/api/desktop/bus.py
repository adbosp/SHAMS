"""In-memory event bus for streamed jobs (ADR-122).

Every job (a chat reply, an agent run, a media generation) publishes numbered events. Clients read them
over Server-Sent Events and may reconnect with ``after=<seq>`` without losing anything; the full result is
also written to SQLite when the job ends, so history survives a restart even though the bus does not.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterator

KEEP_JOBS = 200


class JobChannel:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.events: list[dict] = []
        self.closed = False
        self.cond = threading.Condition()
        self.cancel = threading.Event()
        self.created = time.time()

    def publish(self, type_: str, **data: Any) -> dict:
        with self.cond:
            ev = {"seq": len(self.events) + 1, "type": type_, "job_id": self.job_id, "ts": time.time(), **data}
            self.events.append(ev)
            self.cond.notify_all()
            return ev

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def read(self, after: int = 0, timeout: float = 15.0) -> tuple[list[dict], bool]:
        """Events with seq > after; waits up to ``timeout`` when there are none yet."""
        with self.cond:
            if len(self.events) <= after and not self.closed:
                self.cond.wait(timeout)
            return self.events[after:], self.closed


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self.channels: dict[str, JobChannel] = {}

    def open(self, job_id: str) -> JobChannel:
        with self._lock:
            ch = JobChannel(job_id)
            self.channels[job_id] = ch
            if len(self.channels) > KEEP_JOBS:
                for old in sorted(self.channels.values(), key=lambda c: c.created)[:len(self.channels) - KEEP_JOBS]:
                    if old.closed:
                        self.channels.pop(old.job_id, None)
            return ch

    def get(self, job_id: str) -> JobChannel | None:
        return self.channels.get(job_id)

    def sse(self, job_id: str, after: int = 0, heartbeat: float = 15.0) -> Iterator[str]:
        ch = self.get(job_id)
        if ch is None:
            yield f"event: gone\ndata: {json.dumps({'job_id': job_id})}\n\n"
            return
        seq = after
        while True:
            evs, closed = ch.read(seq, heartbeat)
            for ev in evs:
                seq = ev["seq"]
                yield f"id: {seq}\nevent: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if closed and seq >= len(ch.events):
                yield "event: end\ndata: {}\n\n"
                return
            if not evs:
                yield ": keep-alive\n\n"

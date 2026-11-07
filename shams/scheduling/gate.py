"""Process-wide GPU gate (ADR-122).

Each project has its own Scheduler + Model Lifecycle (one event store per project). When one process serves
several clients at once — Autonomous runs, Direct Mode tasks and streamed desktop chats — they all share the
same physical GPU. On a single-GPU machine memory-heavy jobs must run one after another, so every GPU user
takes this gate first. On several GPUs (or when the user turns it off) the gate never blocks and only
reports who is using the GPU.

The gate never kills processes: switching models is done through the backends' own APIs (Ollama
``keep_alive: 0``, an image server's free endpoint) by whoever holds it.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any


class GpuGate:
    def __init__(self, serialize: bool = True):
        self.serialize = serialize
        self._cond = threading.Condition(threading.RLock())
        self._holder: dict | None = None
        self._depth = 0
        self._owner_thread: int | None = None
        self.waiting: list[dict] = []
        self.history: list[dict] = []            # last holders, newest last (resource monitor)

    def configure(self, serialize: bool) -> None:
        with self._cond:
            self.serialize = bool(serialize)
            self._cond.notify_all()

    @property
    def holder(self) -> dict | None:
        return dict(self._holder) if self._holder else None

    def status(self) -> dict:
        with self._cond:
            return {"serialize": self.serialize, "holder": self.holder, "waiting": [dict(w) for w in self.waiting],
                    "recent": [dict(h) for h in self.history[-10:]]}

    @contextmanager
    def hold(self, owner: str, *, model: str | None = None, kind: str = "llm", timeout: float | None = None,
             cancel: Any = None):
        """Hold the GPU for one job. Re-entrant for the same thread. Raises TimeoutError when ``timeout``
        passes, or InterruptedError when ``cancel`` (an Event) is set while waiting."""
        me = threading.get_ident()
        info = {"owner": owner, "model": model, "kind": kind, "since": time.time()}
        with self._cond:
            if self._owner_thread == me:
                self._depth += 1
                prev = self._holder
                self._holder = {**info, "since": prev["since"] if prev else info["since"]}
                try:
                    yield self
                finally:
                    self._depth -= 1
                    self._holder = prev
                return
            if self.serialize:
                deadline = None if timeout is None else time.monotonic() + timeout
                self.waiting.append(info)
                try:
                    while self._owner_thread is not None and self.serialize:
                        if cancel is not None and cancel.is_set():
                            raise InterruptedError(f"{owner}: canceled while waiting for the GPU")
                        left = None if deadline is None else deadline - time.monotonic()
                        if left is not None and left <= 0:
                            raise TimeoutError(f"{owner}: GPU busy ({self._holder and self._holder['owner']})")
                        self._cond.wait(0.25 if left is None else min(0.25, left))
                finally:
                    self.waiting.remove(info)
                self._owner_thread, self._depth = me, 1
            self._holder = info
        try:
            yield self
        finally:
            with self._cond:
                done = dict(info, until=time.time())
                self.history = (self.history + [done])[-50:]
                if self._owner_thread == me:
                    self._owner_thread, self._depth = None, 0
                if self._holder is info or self._owner_thread is None:
                    self._holder = None
                self._cond.notify_all()


def should_serialize(setting: Any, gpu_count: int) -> bool:
    """``resources.serialize_gpu``: true / false / "auto" (serialize when the machine has at most one GPU)."""
    if isinstance(setting, bool):
        return setting
    return gpu_count <= 1


GPU_GATE = GpuGate()

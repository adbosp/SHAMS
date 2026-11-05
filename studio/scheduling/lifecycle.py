"""Model Lifecycle Manager: load/unload per (node, GPU) lane (docs/design/04 §1.3–1.4).

Adapters are per backend: Mock (simulated VRAM), Null (backend manages memory itself) and
Provider (delegates to the provider's load/unload, e.g. Ollama keep_alive). Plugins add more (ADR-116).
"""
from __future__ import annotations

from typing import Callable, Protocol

from studio import contracts
from studio.events.store import EventStore
from studio.nodes.registry import Lane, Worker


class LoadAdapter(Protocol):
    def load(self, lane: Lane, worker: Worker) -> None: ...
    def unload(self, lane: Lane, model: str) -> None: ...
    def vram_free_mb(self, lane: Lane) -> int: ...


class MockAdapter:
    """Simulates VRAM accounting; records loads/unloads so tests can count model switches."""

    def __init__(self):
        self.used: dict[str, int] = {}
        self.log: list[tuple[str, str, str]] = []

    def load(self, lane: Lane, worker: Worker) -> None:
        need = int(worker.entry["runtime"].get("vram_mb_est", 0))
        if self.vram_free_mb(lane) < need:
            raise MemoryError(f"CUDA out of memory (mock): need {need} MB on {lane.lane_id}")
        self.used[lane.lane_id] = self.used.get(lane.lane_id, 0) + need
        self.log.append(("load", lane.lane_id, worker.model))

    def unload(self, lane: Lane, model: str) -> None:
        self.used[lane.lane_id] = 0
        self.log.append(("unload", lane.lane_id, model))

    def vram_free_mb(self, lane: Lane) -> int:
        return lane.vram_mb - self.used.get(lane.lane_id, 0)


class NullAdapter:
    """Backends that manage memory themselves (llama.cpp server, remote APIs): only VRAM bookkeeping."""

    def __init__(self):
        self.used: dict[str, int] = {}

    def load(self, lane: Lane, worker: Worker) -> None:
        self.used[lane.lane_id] = int(worker.entry["runtime"].get("vram_mb_est", 0))

    def unload(self, lane: Lane, model: str) -> None:
        self.used[lane.lane_id] = 0

    def vram_free_mb(self, lane: Lane) -> int:
        return lane.vram_mb - self.used.get(lane.lane_id, 0)


class ProviderAdapter(NullAdapter):
    """Delegates load/unload to the worker's provider when it supports it (e.g. Ollama keep_alive)."""

    def __init__(self, provider_for: Callable[[Worker], object]):
        super().__init__()
        self.provider_for = provider_for
        self.by_model: dict[str, Worker] = {}

    def load(self, lane: Lane, worker: Worker) -> None:
        prov = self.provider_for(worker)
        if hasattr(prov, "load"):
            prov.load(worker.model)
        self.by_model[worker.model] = worker
        super().load(lane, worker)

    def unload(self, lane: Lane, model: str) -> None:
        w = self.by_model.get(model)
        prov = self.provider_for(w) if w else None
        if prov is not None and hasattr(prov, "unload"):
            prov.unload(model)
        super().unload(lane, model)


class ModelLifecycle:
    def __init__(self, store: EventStore, adapters: dict[str, LoadAdapter], workers: dict[str, Worker] | None = None,
                 safety_margin_mb: int = 1024, fallback: LoadAdapter | None = None):
        self.db = store
        self.fallback = fallback
        self.workers = workers if workers is not None else {}
        self.adapters = adapters
        self.margin = safety_margin_mb
        self.switches = 0

    def loaded(self, lane: Lane) -> tuple[str | None, str | None]:
        r = self.db.q1("SELECT model, worker_id FROM models WHERE lane=?", (lane.lane_id,))
        return (r[0], r[1]) if r else (None, None)

    def _adapter(self, worker: Worker) -> LoadAdapter:
        name = worker.entry["runtime"]["load_adapter"]
        if name in self.adapters:
            return self.adapters[name]
        if self.fallback is None:
            raise KeyError(f"no load adapter {name!r} for {worker.worker_id}")
        return self.fallback

    def ensure_loaded(self, lane: Lane, worker: Worker) -> bool:
        """Returns True if a load happened. CPU/external lanes and tool workers need nothing."""
        if lane.kind != "gpu" or not worker.model:
            return False
        model, holder = self.loaded(lane)
        if model == worker.model:
            return False
        adapter = self._adapter(worker)
        if model is not None:
            self.unload(lane)
        need = int(worker.entry["runtime"].get("vram_mb_est", 0))
        if adapter.vram_free_mb(lane) < need + self.margin:
            raise MemoryError(f"out of memory: VRAM not released on {lane.lane_id} "
                              f"(free {adapter.vram_free_mb(lane)} MB, need {need + self.margin} MB)")
        adapter.load(lane, worker)
        with self.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO models VALUES (?,?,?,?)",
                      (lane.lane_id, worker.worker_id, worker.model, contracts.now_iso()))
            self.db.append("model.loaded", "model", worker.model, "scheduler", payload={"lane": lane.lane_id})
        if model is not None:
            self.switches += 1
        return True

    def unload(self, lane: Lane) -> None:
        model, holder = self.loaded(lane)
        if model is None:
            return
        holder_w = self.workers.get(holder)
        if holder_w is not None:
            self._adapter(holder_w).unload(lane, model)
        with self.db.tx() as c:
            c.execute("DELETE FROM models WHERE lane=?", (lane.lane_id,))
            self.db.append("model.unloaded", "model", model, "scheduler", payload={"lane": lane.lane_id})

"""Compute Nodes and Capability Registry (ADR-114).

The controller has no hardware assumption: lanes are generated from declared nodes.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from studio import contracts


@dataclass(frozen=True)
class Lane:
    lane_id: str          # gpu:<node>:<i> | cpu:<node> | external:<node>
    node_id: str
    kind: str             # gpu | cpu | external
    capacity: int
    vram_mb: int = 0


@dataclass
class Worker:
    entry: dict
    worker_id: str = field(init=False)
    node_id: str = field(init=False)
    lane_kind: str = field(init=False)
    model: str | None = field(init=False)

    def __post_init__(self):
        w, rt = self.entry["worker"], self.entry["runtime"]
        self.worker_id, self.node_id, self.lane_kind = w["worker_id"], rt["node_id"], rt["lane"]
        self.model = w.get("model")

    @property
    def capabilities(self) -> list[str]:
        return self.entry["capabilities"]

    @property
    def ref(self) -> dict:
        return self.entry["worker"]

    @property
    def switch_cost_s(self) -> float:
        rt = self.entry["runtime"]
        return float(rt.get("load_seconds_p50", 0)) + float(rt.get("unload_seconds_p50", 0))

    def quality(self, capability: str) -> float:
        q = self.entry.get("quality", {}).get(capability)
        return q["acceptance_rate"] if q else 0.5


class NodeRegistry:
    def __init__(self, nodes: list[dict] | None = None, workers: list[dict] | None = None):
        self.nodes: dict[str, dict] = {}
        self.workers: dict[str, Worker] = {}
        for n in nodes or []:
            self.add_node(n)
        for w in workers or []:
            self.add_worker(w)

    @classmethod
    def from_toml(cls, path: Path) -> "NodeRegistry":
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        nodes = [{"schema_version": contracts.SCHEMA_VERSION, "node_id": n.pop("id"), **n} for n in data.get("node", [])]
        workers = [w for w in data.get("worker", [])]
        return cls(nodes, workers)

    def add_node(self, n: dict) -> None:
        n = {"schema_version": contracts.SCHEMA_VERSION, **n}
        contracts.validate("node", n)
        self.nodes[n["node_id"]] = n

    def add_worker(self, entry: dict) -> None:
        entry = {"schema_version": contracts.SCHEMA_VERSION, **entry}
        contracts.validate("capability", entry)
        if entry["runtime"]["node_id"] not in self.nodes:
            raise ValueError(f"worker {entry['worker']['worker_id']} on unknown node {entry['runtime']['node_id']}")
        self.workers[entry["worker"]["worker_id"]] = Worker(entry)

    def lanes(self) -> list[Lane]:
        out: list[Lane] = []
        for nid, n in self.nodes.items():
            if not n.get("enabled", True) or n.get("health", {}).get("status") == "down":
                continue
            if n["kind"] == "external":
                out.append(Lane(f"external:{nid}", nid, "external", max(1, n.get("rate_limit_per_min", 1))))
                continue
            if n.get("cpu_slots", 2) > 0:
                out.append(Lane(f"cpu:{nid}", nid, "cpu", n.get("cpu_slots", 2)))
            for g in n.get("gpus", []):
                out.append(Lane(f"gpu:{nid}:{g['index']}", nid, "gpu", 1, g["vram_mb"]))
        return out

    def candidates(self, capability: str) -> list[Worker]:
        out = []
        for w in self.workers.values():
            pol = w.entry["policy"]
            node = self.nodes.get(w.node_id, {})
            if (capability in w.capabilities and pol.get("enabled", True) and node.get("enabled", True)
                    and w.entry.get("health", {}).get("status", "ok") != "down"):
                out.append(w)
        return out

    def lanes_for(self, w: Worker) -> list[Lane]:
        need = int(w.entry["runtime"].get("vram_mb_est", 0))
        return [l for l in self.lanes() if l.node_id == w.node_id and l.kind == w.lane_kind
                and (l.kind != "gpu" or l.vram_mb >= need)]


# --------------------------------------------------------------------------- mock setup
MOCK_MODEL_FAMILIES = {
    # capability prefix -> (mock model, lane)
    "plan": ("mock-reasoner", "gpu"), "architect": ("mock-reasoner", "gpu"),
    "debug_analysis": ("mock-reasoner", "gpu"), "semantic_review": ("mock-reasoner", "gpu"),
    "code": ("mock-coder", "gpu"), "integration": ("mock-coder", "gpu"),
    "image": ("mock-diffusion", "gpu"), "visual_review": ("mock-vision", "gpu"),
    "mesh": ("mock-mesh", "gpu"), "rig": ("mock-mesh", "gpu"), "animation": ("mock-motion", "gpu"),
    "audio": ("mock-audio", "gpu"),
    "build": (None, "cpu"), "test": (None, "cpu"), "validate": (None, "cpu"), "vcs": (None, "cpu"),
    "scene": (None, "gpu"),
}


def mock_registry(capabilities: set[str], *, gpu_vram_mb: int = 8192, cpu_slots: int = 3,
                  load_seconds: float = 20.0) -> NodeRegistry:
    """A controller node (CPU only) + one virtual GPU node, with one mock worker per model family."""
    reg = NodeRegistry()
    reg.add_node({"node_id": "node.controller", "kind": "local", "cpu_slots": cpu_slots, "gpus": [],
                  "providers": ["mock"], "profile": "profile.cpu_only"})
    reg.add_node({"node_id": "node.mock_gpu", "kind": "local", "cpu_slots": 0,
                  "gpus": [{"index": 0, "vram_mb": gpu_vram_mb}], "providers": ["mock"], "profile": "profile.mock"})
    families: dict[str, set[str]] = {}
    for cap in capabilities:
        prefix = cap.split(".")[0]
        model, lane = MOCK_MODEL_FAMILIES.get(prefix, ("mock-generic", "gpu"))
        key = f"{model or 'tool'}|{lane}"
        families.setdefault(key, set()).add(cap)
    for key, caps in sorted(families.items()):
        model, lane = key.split("|")
        wid = "worker.mock_" + (model.replace("mock-", "") if model != "tool" else "tool")
        node = "node.mock_gpu" if lane == "gpu" else "node.controller"
        worker = {"worker_id": wid, "kind": "mock", "provider": "mock", "node_id": node}
        if model != "tool":
            worker.update(model=model, model_version="mock-1")
        reg.add_worker({
            "worker": worker, "capabilities": sorted(caps),
            "runtime": {"node_id": node, "lane": lane, "load_adapter": "mock" if model != "tool" else "none",
                        "vram_mb_est": 4000 if lane == "gpu" else 0,
                        "load_seconds_p50": load_seconds if model != "tool" else 0,
                        "unload_seconds_p50": 2.0 if model != "tool" else 0},
            "policy": {"enabled": True, "privacy": "local"},
        })
    return reg

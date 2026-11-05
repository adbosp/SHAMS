"""Studio Core facade: wires Event Store, Artifacts, Nodes, Scheduler, Task Runner, QC, Repair.

Single process, single DB writer (ADR-104). Runs on any PC; no GPU or model needed (ADR-114).
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from shams import contracts
from shams.artifacts.registry import ArtifactRegistry, ImpactAnalyzer
from shams.context.compiler import ContextCompiler
from shams.events.store import EventStore
from shams.mock.worker import MockWorker, mock_check
from shams.nodes.registry import Lane, NodeRegistry, Worker, mock_registry
from shams.planning.validator import validate_dag
from shams.scheduling.lifecycle import MockAdapter, ModelLifecycle, NullAdapter, ProviderAdapter
from shams.scheduling.scheduler import Scheduler, SchedulerConfig
from shams.statemachine import S
from shams.verification.qc import QCRunner, registered_checkers
from shams.verification.repair import AnalysisAgent, FailureClassifier, RepairPolicy, RepairRouter
from shams.orchestration import task_runner

MODES = ("collaborative", "guided", "autonomous")
CRITICAL_KINDS = {"compile", "unit_test", "integration_test"}
MOCK_KINDS = ("exists", "schema", "compile", "unit_test", "integration_test", "runtime_log", "metric")


class Studio:
    def __init__(self, project_dir: Path | str, *, nodes: NodeRegistry | None = None,
                 mock: MockWorker | None = None, analysis: AnalysisAgent | None = None,
                 checkers: dict | None = None, scheduler_config: SchedulerConfig | None = None,
                 repair_policy: RepairPolicy | None = None):
        self.dir = Path(project_dir)
        cfg = json.loads((self.dir / "project.json").read_text(encoding="utf-8"))
        self.project = cfg
        self.mode = cfg.get("mode", "guided")
        self.db = EventStore(self.dir / "studio.sqlite", cfg["project_id"])
        self.artifacts = ArtifactRegistry(self.db, self.dir / "artifacts")
        self.impact = ImpactAnalyzer(self.db, self.artifacts)
        caps = {t["capability"] for t in self.db.tasks()}
        self.mock_mode = cfg.get("mock_mode", False)
        from shams import engines
        engines.load_plugins(self.dir)
        self.nodes = nodes or (mock_registry(caps) if self.mock_mode else self._load_nodes())
        self.mock = mock or MockWorker(self.dir / "attempts")
        self._impls: dict[str, object] = {}
        self.scheduler = Scheduler(self.db, self.nodes, scheduler_config)
        provider_adapter = ProviderAdapter(self._provider_for)
        self.lifecycle = ModelLifecycle(self.db, {"mock": MockAdapter(), "none": NullAdapter(),
                                                  "provider": provider_adapter, "ollama": provider_adapter},
                                        self.nodes.workers, fallback=provider_adapter)
        self.context = ContextCompiler(self.db, self.artifacts, cfg.get("goal", ""), cfg.get("engine"))
        if checkers is None:
            checkers = {k: mock_check for k in MOCK_KINDS} if self.mock_mode else registered_checkers()
        self.qc = QCRunner(self.db, self.artifacts, checkers)
        self.classifier = FailureClassifier()
        self.repair = RepairRouter(self.db, self.nodes, analysis, repair_policy,
                                   initial_task_count=sum(1 for t in self.db.tasks()
                                                          if t.get("created_by") != "repair_router"))
        self._ckpt_conn = sqlite3.connect(self.dir / "checkpoints.sqlite", check_same_thread=False)
        self.graph = task_runner.build_graph(self, SqliteSaver(self._ckpt_conn))
        self.crash_points: set[str] = set()
        self._recovered = False

    # ------------------------------------------------------------------ project setup
    @staticmethod
    def init_project(project_dir: Path | str, project_id: str, goal: str, *, mode: str = "guided",
                     mock_mode: bool = False, engine: dict | None = None) -> Path:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        d = Path(project_dir)
        d.mkdir(parents=True, exist_ok=True)
        cfg = {"project_id": project_id, "goal": goal, "mode": mode, "mock_mode": mock_mode,
               "engine": engine or {"name": "unity", "version": "6000.0"}, "created_at": contracts.now_iso()}
        from shams import engines
        prof = engines.get(cfg["engine"]["name"])
        cfg["engine"] = {"name": prof.name, "version": cfg["engine"].get("version") or prof.default_version}
        (d / "project.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        db = EventStore(d / "studio.sqlite", project_id)
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?)",
                      (project_id, goal, mode, cfg["engine"]["name"], cfg["engine"]["version"], cfg["created_at"]))
            db.append("project.created", "project", project_id, "human", payload={"mode": mode, "mock": mock_mode})
        db.close()
        return d

    def set_engine(self, name: str) -> None:
        """Switch the project's engine before a plan exists (ADR-115)."""
        from shams import engines
        if self.db.tasks():
            raise ValueError("engine can only change before the first plan is loaded")
        prof = engines.get(name)
        self.project["engine"] = {"name": prof.name, "version": prof.default_version}
        self.context.engine = self.project["engine"]
        (self.dir / "project.json").write_text(json.dumps(self.project, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.db.tx() as c:
            c.execute("UPDATE projects SET engine=?, engine_version=? WHERE project_id=?",
                      (prof.name, prof.default_version, self.db.project_id))
            self.db.append("project.engine_set", "project", self.db.project_id, "human", payload={"engine": prof.name})

    def close(self) -> None:
        self.db.close()
        self._ckpt_conn.close()

    def load_plan(self, dag: dict, approved_by: str | None = None) -> list:
        issues = validate_dag(dag, self.nodes if not self.mock_mode else None)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise ValueError("invalid plan:\n" + "\n".join(map(str, errors)))
        if self.mode in ("collaborative", "guided") and not approved_by:
            raise PermissionError(f"mode '{self.mode}' requires plan approval (approved_by)")
        with self.db.tx() as c:
            c.execute("INSERT INTO plans VALUES (?,?,?,?,?,?,?)",
                      (dag["plan_id"], dag["project_id"], dag["plan_version"], dag.get("template"),
                       json.dumps(dag, ensure_ascii=False), approved_by, contracts.now_iso()))
            self.db.append("plan.created", "plan", dag["plan_id"], "planner",
                           payload={"tasks": len(dag["tasks"]), "approved_by": approved_by})
            for t in self._topo(dag["tasks"]):
                self.db.create_task(t, dag["plan_id"], "planner")
        if self.mock_mode:
            fresh = mock_registry({t["capability"] for t in self.db.tasks()})
            self.nodes.nodes = fresh.nodes
            self.nodes.workers.clear()
            self.nodes.workers.update(fresh.workers)
        self.repair.initial = len(dag["tasks"])
        return issues

    @staticmethod
    def _topo(tasks: list[dict]) -> list[dict]:
        done, out, pending = set(), [], list(tasks)
        while pending:
            for t in list(pending):
                if all(d in done for d in t["depends_on"]):
                    out.append(t)
                    done.add(t["task_id"])
                    pending.remove(t)
        return out

    # ------------------------------------------------------------------ helpers used by the graph
    def lane(self, lane_id: str) -> Lane:
        return next(l for l in self.nodes.lanes() if l.lane_id == lane_id)

    def _load_nodes(self) -> NodeRegistry:
        """nodes.toml in the project, else $SHAMS_HOME/nodes.toml (ADR-114/116). Empty if neither exists."""
        from shams.plugins import shams_home
        for p in (self.dir / "nodes.toml", shams_home() / "nodes.toml"):
            if p.exists():
                return NodeRegistry.from_toml(p)
        return NodeRegistry()

    def worker_impl(self, worker: Worker):
        if worker.entry["worker"]["kind"] == "mock":
            return self.mock
        impl = self._impls.get(worker.worker_id)
        if impl is None:
            from shams import workers
            impl = workers.create(worker.entry, self.dir / "attempts", read_blob=self.artifacts.blob_path)
            self._impls[worker.worker_id] = impl
        return impl

    def _provider_for(self, worker: Worker):
        return getattr(self.worker_impl(worker), "provider", None)

    def tools_for(self, worker_id: str) -> list[str]:
        w = self.nodes.workers[worker_id]
        kind = w.entry["worker"]["kind"]
        return ["mock"] if kind == "mock" else [w.entry["worker"].get("provider") or kind]

    def release(self, lease_id: str, lane_id: str, worker_id: str) -> None:
        worker = self.nodes.workers[worker_id]
        lane = self.lane(lane_id)
        if lane.kind == "gpu" and worker.model:
            upcoming = self.db.tasks({S.READY, S.PENDING, S.REPAIR_REQUIRED})
            keep = any(any(w.model == worker.model and w.node_id == worker.node_id
                           for w in self.nodes.candidates(t["capability"])) for t in upcoming)
            if not keep:
                self.lifecycle.unload(lane)
        self.scheduler.release(lease_id)

    def record_failure(self, task_id: str, attempt: int, detail: str, *, source: str, event: str) -> dict:
        task = self.db.task(task_id)
        self.db.transition(task_id, S.REPAIR_REQUIRED, event, "task_runner", {"error": detail[:300]})
        failure = self.classifier.classify(task, attempt, detail, source, [])
        return self.repair.route(failure)

    # ------------------------------------------------------------------ QC routing
    def run_qc(self, task_id: str, *, revalidation: bool = False) -> dict:
        task = self.db.task(task_id)
        handoff = self.db.handoff(task_id)
        if revalidation:
            current = [self.artifacts.accepted(i["logical_id"]) for i in task.get("inputs", [])]
            handoff = {**handoff, "inputs": [c for c in current if c]}
        rep = self.qc.run(task, handoff)
        failed = next((c for c in rep["criteria"] if c["verdict"] == "FAIL"), None)
        if rep["verdict"] == "FAIL":
            failure = self.classifier.classify(task, handoff["attempt"], failed.get("detail", ""), "qc",
                                               [o for o in handoff["outputs"]])
            rep["failure_id"] = failure["failure_id"]
            self.db.save_qc(rep)
            with self.db.tx():
                for o in handoff["outputs"]:
                    self.artifacts.reject(o["sha256"], o["logical_id"])
                self.db.transition(task_id, S.REPAIR_REQUIRED, "qc.failed", "qc",
                                   {"failure_id": failure["failure_id"], "stage": rep.get("stopped_at_stage")})
            self.repair.route(failure)
            return rep
        self.db.save_qc(rep)
        unverified = [c for c in rep["criteria"] if c["verdict"] == "NOT_VERIFIED"]
        if rep["verdict"] == "NOT_VERIFIED":
            kinds = {k["kind"] for k in task["acceptance_criteria"]
                     if k["id"] in {u["criterion_id"] for u in unverified}}
            if not (self.mode == "autonomous" and not kinds & CRITICAL_KINDS):
                self.db.transition(task_id, S.HUMAN_REVIEW, "qc.not_verified", "qc",
                                   {"criteria": [u["criterion_id"] for u in unverified]})
                return rep
        self._accept(task_id, handoff, "qc.passed", "qc",
                     {"gate": rep["gate"], "not_verified": [u["criterion_id"] for u in unverified]},
                     refresh_handoff=revalidation)
        return rep

    def _accept(self, task_id: str, handoff: dict, event: str, actor: str, payload: dict,
                refresh_handoff: bool = False) -> None:
        superseded: list[str] = []
        with self.db.tx():
            if handoff is not None:
                if refresh_handoff:
                    self.db.save_handoff(handoff)   # record the new input hashes after revalidation
                for o in handoff["outputs"]:
                    superseded += self.artifacts.accept(o["sha256"], o["logical_id"])
            self.db.transition(task_id, S.ACCEPTED, event, actor, payload)
        if superseded:
            self.impact.on_superseded(superseded)

    # ------------------------------------------------------------------ human review (ADR-112)
    def human_decide(self, task_id: str, decision: str, note: str = "") -> None:
        st = self.db.state(task_id)
        if decision == "unblock" and st == S.BLOCKED:
            deps_ok = all(self.db.state(d) == S.ACCEPTED for d in self.db.deps(task_id))
            self.db.transition(task_id, S.READY if deps_ok else S.PENDING, "task.unblocked", "human", {"note": note})
            return
        if st != S.HUMAN_REVIEW and decision != "cancel":
            raise ValueError(f"{task_id} is {st}, not HUMAN_REVIEW")
        payload = {"decision": decision, "note": note}
        if decision == "accept":
            self._accept(task_id, self.db.handoff(task_id), "human.decided", "human", payload)
        elif decision == "retry":
            t = self.db.task(task_id)
            self.db.update_task_json(task_id, {"max_attempts": max(t.get("max_attempts", 3), t["attempt"] + 2)})
            self.db.transition(task_id, S.READY, "human.decided", "human", payload, attempt_delta=1)
        elif decision == "fail":
            self.db.transition(task_id, S.FAILED, "human.decided", "human", payload)
        elif decision == "cancel":
            self.db.transition(task_id, S.CANCELED, "task.canceled", "human", payload)
        else:
            raise ValueError("decision must be accept | retry | fail | cancel | unblock")

    # ------------------------------------------------------------------ main loop
    def recover(self) -> list[str]:
        """Resume work interrupted by a controller crash (checkpoint + event log)."""
        resumed = []
        for lease in self.scheduler.active_leases():
            cfg = task_runner.config(lease["task_id"], lease["attempt"])
            snap = self.graph.get_state(cfg)
            if snap.next:
                self.db.append("task.resumed", "task", lease["task_id"], "scheduler",
                               payload={"next": list(snap.next)})
                self.graph.invoke(None, cfg)
                resumed.append(lease["task_id"])
            else:
                if self.db.state(lease["task_id"]) == S.LEASED:
                    self.db.transition(lease["task_id"], S.READY, "task.lease_released", "scheduler",
                                       {"reason": "recovery"})
                self.scheduler.release(lease["lease_id"])
        for t in self.db.tasks({S.QC_PENDING}):
            cfg = task_runner.config(t["task_id"], t["attempt"])
            snap = self.graph.get_state(cfg)
            if snap.next:
                self.graph.invoke(None, cfg)
            else:
                self.run_qc(t["task_id"])
            resumed.append(t["task_id"])
        self._recovered = True
        return resumed

    def step(self) -> bool:
        if not self._recovered:
            self.recover()
        progressed = False
        for t in self.db.tasks({S.STALE}):
            self.db.transition(t["task_id"], S.QC_PENDING, "task.revalidate", "scheduler")
            self.run_qc(t["task_id"], revalidation=True)
            progressed = True
        self.scheduler.promote_ready()
        for t in self.db.tasks({S.READY}):
            if not self.nodes.candidates(t["capability"]):
                self.db.transition(t["task_id"], S.BLOCKED, "task.blocked", "scheduler",
                                   {"reason": f"tool unavailable: no worker for {t['capability']}"})
        for a in self.scheduler.tick():
            self.graph.invoke({"task_id": a.task_id, "attempt": a.attempt, "lease_id": a.lease_id,
                               "lane_id": a.lane.lane_id, "worker_id": a.worker.worker_id, "trace": []},
                              task_runner.config(a.task_id, a.attempt))
            progressed = True
        return progressed

    def run(self, max_steps: int = 500) -> dict:
        for _ in range(max_steps):
            if not self.step():
                break
        return self.status()

    def status(self) -> dict:
        tasks = self.db.tasks()
        return {
            "project_id": self.db.project_id, "mode": self.mode, "mock_mode": self.mock_mode,
            "counts": dict(Counter(t["state"] for t in tasks)),
            "tasks": [{"task_id": t["task_id"], "state": t["state"], "attempt": t["attempt"],
                       "capability": t["capability"]} for t in tasks],
            "model_loads": self.db.q1("SELECT COUNT(*) FROM events WHERE type='model.loaded'")[0],
            "model_unloads": self.db.q1("SELECT COUNT(*) FROM events WHERE type='model.unloaded'")[0],
            "repairs": self.db.q1("SELECT COUNT(*) FROM events WHERE type='repair.scheduled'")[0],
            "events": self.db.q1("SELECT COUNT(*) FROM events")[0],
            "done": all(S(t["state"]) in {S.ACCEPTED, S.CANCELED} for t in tasks) and bool(tasks),
        }

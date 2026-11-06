"""SHAMS Core facade for one project: wires Event Store, Artifacts, Nodes, Scheduler, Task Runner,
QC and Repair, plus the project's Domain Pack (ADR-117).

Single process, single DB writer (ADR-104). Runs on any PC; no GPU or model needed (ADR-114).
The core knows no product domain: everything domain-specific comes from the Domain Pack.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

import os

from shams import contracts, packs
from shams import settings as settings_mod
from shams.hooks import HookRunner
from shams.mcp import MCPManager
from shams.artifacts.registry import ArtifactRegistry, ImpactAnalyzer
from shams.context.compiler import ContextCompiler
from shams.events.store import EventStore
from shams.mock.worker import MockWorker, make_mock_check
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
MOCK_KINDS = ("exists", "schema", "compile", "unit_test", "integration_test", "runtime_log", "metric", "visual")


DB_FILE = "shams.sqlite"


def _db_path(d: Path) -> Path:
    legacy = d / "studio.sqlite"            # projects created before V2.0
    return legacy if legacy.exists() and not (d / DB_FILE).exists() else d / DB_FILE


def _domain_of(cfg: dict) -> tuple[str, dict]:
    """(domain, domain_config) from project.json; pre-V2.0 projects only had an engine -> game."""
    if "domain" in cfg:
        return cfg["domain"], cfg.get("domain_config", {})
    if "engine" in cfg:
        return "game", {"engine": cfg["engine"]}
    return packs.DEFAULT_DOMAIN, {}


class Shams:
    def __init__(self, project_dir: Path | str, *, nodes: NodeRegistry | None = None,
                 mock: MockWorker | None = None, analysis: AnalysisAgent | None = None,
                 checkers: dict | None = None, scheduler_config: SchedulerConfig | None = None,
                 repair_policy: RepairPolicy | None = None):
        self.dir = Path(project_dir)
        cfg = json.loads((self.dir / "project.json").read_text(encoding="utf-8"))
        self.project = cfg
        self.mode = cfg.get("mode", "guided")
        self.db = EventStore(_db_path(self.dir), cfg["project_id"])
        domain, dcfg = _domain_of(cfg)
        self.pack = packs.get(domain)
        self.pack.activate(self.dir)
        cfg["domain"], cfg["domain_config"] = self.pack.name, self.pack.normalize_config(dcfg, self.dir)
        cfg.pop("engine", None)
        self.settings = settings_mod.load(self.dir)
        for k, v in (self.settings.get("env") or {}).items():       # non-secret env (ADR-118)
            os.environ.setdefault(k, str(v))
        self.mcp = MCPManager.from_settings(self.settings)
        from shams.research import Research, ToolBox
        self.tools = ToolBox(self.mcp, Research(self.settings.get("research")))
        self.hooks = HookRunner(self.settings.get("hooks"), self.dir, self.settings.get("env"))
        self.hooks.last_seq = self.db.q1("SELECT COALESCE(MAX(seq),0) FROM events")[0]
        self.artifacts = ArtifactRegistry(self.db, self.dir / "artifacts")
        self.impact = ImpactAnalyzer(self.db, self.artifacts)
        caps = {t["capability"] for t in self.db.tasks()}
        self.mock_mode = cfg.get("mock_mode", False)
        self.nodes = nodes or (mock_registry(caps) if self.mock_mode else self._load_nodes())
        self.mock = mock or MockWorker(self.dir / "attempts")
        self._impls: dict[str, object] = {}
        self.scheduler = Scheduler(self.db, self.nodes, scheduler_config)
        provider_adapter = ProviderAdapter(self._provider_for)
        self.lifecycle = ModelLifecycle(self.db, {"mock": MockAdapter(), "none": NullAdapter(),
                                                  "provider": provider_adapter, "ollama": provider_adapter},
                                        self.nodes.workers, fallback=provider_adapter)
        self.context = ContextCompiler(self.db, self.artifacts, cfg.get("goal", ""), self.pack.name,
                                       self.pack.domain_context(cfg["domain_config"]))
        if checkers is None:
            if self.mock_mode:
                check = make_mock_check(self.pack.mock_failure_logs())
                checkers = {k: check for k in MOCK_KINDS}
            else:
                checkers = asset_checkers(self.settings, {**self.pack.checkers(), **registered_checkers()})
        self.qc = QCRunner(self.db, self.artifacts, checkers)
        self.classifier = FailureClassifier(self.pack.failure_rules())
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
                     mock_mode: bool = False, domain: str | None = None, domain_config: dict | None = None,
                     engine: dict | str | None = None) -> Path:
        """Create a project. `domain` defaults to the pack that best matches the goal, else software.
        `engine=` is a Game Pack shortcut (domain="game", domain_config={"engine": engine})."""
        # Validate everything before touching the disk, so a failed init leaves nothing behind.
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        contracts.check_id(project_id, "project id")
        d = Path(project_dir)
        if (d / "project.json").exists():
            raise ValueError(f"{d} already contains a project (project.json); choose an empty folder")
        dcfg = dict(domain_config or {})
        if engine is not None:
            domain = domain or "game"
            dcfg.setdefault("engine", engine)
        pack = packs.get(domain or packs.detect_domain(goal) or packs.DEFAULT_DOMAIN)
        pack.load_plugins(d if d.exists() else None)
        dcfg = pack.normalize_config(dcfg, d if d.exists() else None)
        cfg = {"project_id": project_id, "goal": goal, "mode": mode, "mock_mode": mock_mode,
               "domain": pack.name, "domain_config": dcfg, "created_at": contracts.now_iso()}
        existed = d.exists()
        before = set(d.iterdir()) if existed else set()
        d.mkdir(parents=True, exist_ok=True)
        pack.activate(d)
        try:
            db = EventStore(_db_path(d), project_id)
            try:
                with db.tx() as c:
                    c.execute("INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?)",
                              (project_id, goal, mode, pack.name, json.dumps(dcfg, ensure_ascii=False),
                               cfg["created_at"]))
                    db.append("project.created", "project", project_id, "human",
                              payload={"mode": mode, "mock": mock_mode, "domain": pack.name})
            finally:
                db.close()
            (d / "project.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            for f in set(d.iterdir()) - before:      # remove only what this call created
                shutil.rmtree(f) if f.is_dir() else f.unlink()
            if not existed:
                d.rmdir()
            raise
        return d

    @property
    def domain_config(self) -> dict:
        return self.project["domain_config"]

    def describe(self) -> str:
        """Human label of the project's domain setup, e.g. 'Software · Python'."""
        return f"{self.pack.display} · {self.pack.describe(self.domain_config)}"

    def configure(self, domain: str | None = None, **options) -> dict:
        """Change the domain and/or its options (e.g. engine="ue5", stack="typescript") before a plan exists."""
        if self.db.tasks():
            raise ValueError("domain and its options can only change before the first plan is loaded")
        if domain and packs.get(domain).name != self.pack.name:
            pack = packs.get(domain)
            pack.activate(self.dir)
            cfg = pack.configure({}, self.dir, **options)
        else:
            pack = self.pack
            cfg = pack.configure(self.domain_config, self.dir, **options)
        self.pack = pack
        self.project["domain"], self.project["domain_config"] = pack.name, cfg
        self.context.domain, self.context.domain_context = pack.name, pack.domain_context(cfg)
        self.classifier = FailureClassifier(pack.failure_rules())
        if self.mock_mode:
            check = make_mock_check(pack.mock_failure_logs())
            self.qc.checkers.update({k: check for k in MOCK_KINDS})
        (self.dir / "project.json").write_text(json.dumps(self.project, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.db.tx() as c:
            c.execute("UPDATE projects SET domain=?, domain_config=? WHERE project_id=?",
                      (pack.name, json.dumps(cfg, ensure_ascii=False), self.db.project_id))
            self.db.append("project.configured", "project", self.db.project_id, "human",
                           payload={"domain": pack.name, "domain_config": cfg})
        return cfg

    def plan_from_template(self, template: str | None = None, goal: str | None = None,
                           approved_by: str | None = None) -> dict:
        """Instantiate a template of this project's Domain Pack and load it."""
        from shams.planning.validator import instantiate
        dag = instantiate(template, self.db.project_id, goal if goal is not None else self.project.get("goal", ""),
                          domain=self.pack.name, config=self.domain_config, project_dir=self.dir)
        self.load_plan(dag, approved_by=approved_by)
        return dag

    def plan_from_llm(self, goal: str | None = None, approved_by: str | None = None, *, fallback: bool = True,
                      max_rounds: int = 3) -> dict:
        """The planner role writes the DAG (ADR-120); the Plan Validator decides; template as fallback."""
        from shams.planning import llm_planner
        goal = goal if goal is not None else self.project.get("goal", "")
        planners = [w for w in self.nodes.candidates("plan.dag") if w.entry["worker"]["kind"] != "mock"]
        result = {"dag": None, "issues": ["no planner worker (capability plan.dag)"], "rounds": 0}
        if planners:
            w = planners[0]
            impl = self.worker_impl(w)
            caps = sorted({c for x in self.nodes.workers.values() for c in x.capabilities})
            result = llm_planner.plan(impl.provider, w.model, goal=goal, project_id=self.db.project_id,
                                      capabilities=caps, domain=self.pack.name,
                                      domain_context=self.pack.domain_context(self.domain_config),
                                      nodes=self.nodes, max_rounds=max_rounds)
        with self.db.tx():
            self.db.append("plan.llm_attempted", "plan", f"plan.{self.db.project_id}", "planner",
                           payload={"ok": result["dag"] is not None, "rounds": result.get("rounds", 0),
                                    "issues": result["issues"][:10]})
        if result["dag"] is not None:
            self.load_plan(result["dag"], approved_by=approved_by)
            return result["dag"]
        if not fallback:
            raise ValueError("LLM plan invalid:\n" + "\n".join(result["issues"]))
        return self.plan_from_template(goal=goal, approved_by=approved_by)

    # ------------------------------------------------------------------ Direct Mode (ADR-121)
    def direct(self, preset_id: str, params: dict, *, by: str = "human", confirm_adult: bool = False) -> str:
        """Queue one task for one chosen preset: no planner, same scheduler, GPU lease, workers,
        artifact registry, QC and event log as Autonomous Mode. Returns the task id; run() executes it."""
        import jsonschema
        from shams import policy
        from shams.models import ModelRegistry
        reg = ModelRegistry(self.settings)
        p = reg.preset(preset_id)
        if p is None:
            raise KeyError(f"unknown preset {preset_id!r}; see `shams presets`")
        if "direct" not in p["execution_modes"]:
            raise PermissionError(f"preset {preset_id} does not allow Direct Mode")
        params = dict(params or {})
        try:
            jsonschema.validate(params, p["input_schema"])
            props = (p["input_schema"].get("properties") or {})
            # preset params the user may also set become defaults; the rest (model files...) stay in the worker config
            params = {**{k: v for k, v in p.get("params", {}).items() if k in props}, **params}
        except jsonschema.ValidationError as e:
            raise ValueError(f"invalid parameters for {preset_id}: {e.message}") from None
        allowed = bool((self.settings.get("general") or {}).get("allow_adult_content", False))
        try:
            params = policy.check(p, params, allowed=allowed, confirmed=confirm_adult)
        except policy.PolicyError as e:
            with self.db.tx():
                self.db.append("policy.refused", "model", preset_id, by, payload={"preset": preset_id, "reason": str(e)[:300]})
            raise
        if p["status"] != "ok":
            raise RuntimeError(f"preset {preset_id} is not available: {p['status']} {p['how']}")
        if p["worker"]["id"] not in self.nodes.workers:          # settings changed after this project opened
            self.nodes = build_registry(self.settings)
            self.scheduler.nodes = self.nodes
            self.repair.nodes = self.nodes
            self.lifecycle.workers = self.nodes.workers
        if p["content_policy"].get("adult"):
            params["_adult_confirmed"] = True
        n = self.db.q1("SELECT COUNT(*) FROM tasks WHERE task_id LIKE 'task.direct.%'")[0] + 1
        tid = f"task.direct.{n:04d}"
        kinds = {"text/markdown": ("metric", {"type": "text", "min_chars": 1}),
                 "image/png": ("screenshot", {"type": "image", "min_width": 64}),
                 "model/gltf-binary": ("schema", {"type": "glb"})}
        crit = [{"id": f"ac.{tid[5:]}.{i}", "description": f"{o['name']} produced and valid", "kind": kinds[o["media_type"]][0],
                 "check": {**kinds[o["media_type"]][1], "logical_id": f"direct.{n:04d}.{o['name']}"}}
                for i, o in enumerate(p["outputs"]) if o["media_type"] in kinds]
        prompt = str(params.get("prompt", ""))
        task = {"schema_version": contracts.SCHEMA_VERSION, "project_id": self.db.project_id, "task_id": tid,
                "title": f"[{preset_id}] {prompt[:80]}" or preset_id, "description": prompt,
                "capability": p["direct_capability"], "depends_on": [], "inputs": [],
                "outputs": [{"name": o["name"], "logical_id": f"direct.{n:04d}.{o['name']}", "media_type": o["media_type"]}
                            for o in p["outputs"]],
                "acceptance_criteria": crit or [{"id": f"ac.{tid[5:]}.exists", "description": "output exists",
                                                 "kind": "exists"}],
                "params": params, "preset": preset_id, "max_attempts": int(p.get("max_attempts", 1) or 1),
                "budget": {"max_wall_seconds": 3600}, "priority": 90, "created_by": "human"}
        dag = {"schema_version": contracts.SCHEMA_VERSION, "project_id": self.db.project_id,
               "plan_id": f"plan.{tid}", "plan_version": 1, "goal": prompt[:500], "template": "direct", "tasks": [task]}
        self.load_plan(dag, approved_by=by)
        with self.db.tx():
            self.db.append("direct.queued", "task", tid, by, payload={"preset": preset_id, "model": p["model"],
                                                                       "provider": p["provider"]})
        return tid

    def run_task(self, task_id: str, max_steps: int = 200) -> dict:
        """Step until one task reaches a final or waiting state (Direct Mode)."""
        final = {S.ACCEPTED, S.FAILED, S.CANCELED, S.HUMAN_REVIEW, S.BLOCKED}
        for _ in range(max_steps):
            if S(self.db.task(task_id)["state"]) in final:
                break
            if not self.step():
                break
        t = self.db.task(task_id)
        return {"task_id": task_id, "state": t["state"], "outputs": [a for a in (self.artifacts.accepted(o["logical_id"])
                                                                               for o in t["outputs"]) if a],
                "handoff": self.db.handoff(task_id)}

    def close(self) -> None:
        self.mcp.close()
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
        """Nodes and workers from settings (which include nodes.toml, ADR-114/116/118).

        A worker may name a node nobody declared: ``node.local`` is then filled from the real hardware
        of this PC, any other id becomes an ``external`` node (remote API)."""
        return build_registry(self.settings)

    def worker_impl(self, worker: Worker):
        if worker.entry["worker"]["kind"] == "mock":
            return self.mock
        impl = self._impls.get(worker.worker_id)
        if impl is None:
            from shams import workers
            impl = workers.create(worker.entry, self.dir / "attempts", read_blob=self.artifacts.blob_path,
                                  mcp=self.tools, max_tool_rounds=int(self.settings["general"].get("max_tool_rounds", 8)))
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
        if self.hooks:
            extra = []
            if not progressed and self.db.tasks():
                s = self.status()
                extra.append({"type": "run.finished", "subject_id": self.db.project_id, "payload":
                              {"done": s["done"], "counts": s["counts"]}})
            self.hooks.dispatch(self.db, extra)
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
            "domain": self.pack.name, "domain_config": self.domain_config, "setup": self.describe(),
            "counts": dict(Counter(t["state"] for t in tasks)),
            "tasks": [{"task_id": t["task_id"], "state": t["state"], "attempt": t["attempt"],
                       "capability": t["capability"]} for t in tasks],
            "model_loads": self.db.q1("SELECT COUNT(*) FROM events WHERE type='model.loaded'")[0],
            "model_unloads": self.db.q1("SELECT COUNT(*) FROM events WHERE type='model.unloaded'")[0],
            "repairs": self.db.q1("SELECT COUNT(*) FROM events WHERE type='repair.scheduled'")[0],
            "events": self.db.q1("SELECT COUNT(*) FROM events")[0],
            "done": all(S(t["state"]) in {S.ACCEPTED, S.CANCELED} for t in tasks) and bool(tasks),
        }


def asset_checkers(s: dict, fallback: dict) -> dict:
    """Path-aware checkers selected by criterion.check.type (ADR-120), the pack's checkers as fallback."""
    from shams.models import ModelRegistry
    from shams.verification.checkers import AssetCheckers, make_visual_checker
    visual = None
    roles = (s.get("models") or {}).get("roles") or {}
    if "visual_qc" in roles:
        b = ModelRegistry(s).binding("visual_qc")
        if b is not None and b.ok:
            from shams import providers
            o = (s.get("models") or {}).get("ollama") or {}
            visual = make_visual_checker(providers.create("ollama", {k: v for k, v in o.items() if k == "base_url"}
                                                          | {"num_ctx": 8192}), b.model_id)
    return AssetCheckers(fallback, settings=s, visual=visual).table()


def build_registry(s: dict) -> NodeRegistry:
    from shams import hardware
    nodes = [dict(n) for n in s.get("nodes") or []]
    from shams.models import merge_role_workers
    wks = merge_role_workers(s)
    declared = {n.get("id") or n.get("node_id") for n in nodes}
    for nid in dict.fromkeys(w.get("node") for w in wks if w.get("node")):
        if nid not in declared:
            nodes.append({k: v for k, v in hardware.local_node(node_id=nid).items()} if nid == "node.local"
                         else {"id": nid, "kind": "external"})
            declared.add(nid)
    return NodeRegistry.from_dict({"node": nodes, "worker": wks})


Studio = Shams      # pre-V2.0 name, kept for compatibility

"""SHAMS local API (FastAPI) + main UI (ADR-006, ADR-112).

Serves `apps/ui/index.html` and a JSON view of the live Studio. One Studio instance per process;
a background thread drives `Studio.step()` while the UI polls `/api/state`.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from studio import contracts, engines
from studio.mock.worker import MockWorker
from studio.orchestration.studio import Studio
from studio.planning.validator import instantiate
from studio.statemachine import S

UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "index.html"

FAMILY_LABEL = {
    "code": "Coding Worker", "integration": "Integration Worker", "plan": "Studio Director",
    "architect": "Architect", "debug_analysis": "Analysis Worker", "semantic_review": "Review Worker",
    "visual_review": "Visual Review", "image": "Image Generator", "mesh": "3D Generator",
    "rig": "Rigging Worker", "animation": "Animation Worker", "audio": "Audio Generator",
    "build": "Build & Test Tools", "test": "Build & Test Tools", "validate": "Validators",
    "scene": "Scene Capture", "vcs": "Git",
}
STATE_LABEL = {"ACCEPTED": "PASS", "QC_PENDING": "QC", "REPAIR_REQUIRED": "REPAIR", "HUMAN_REVIEW": "REVIEW"}


def worker_label(worker) -> str:
    fam = worker.capabilities[0].split(".")[0]
    return FAMILY_LABEL.get(fam, worker.worker_id)


def console_line(e: dict) -> str | None:
    p = json.loads(e["payload_json"] or "{}")
    sid, t = e["subject_id"], e["type"]
    return {
        "model.loaded": f"Loading {sid} on {p.get('lane', '')}",
        "model.unloaded": f"Unloaded {sid}",
        "task.started": f"Executing task {sid}",
        "handoff.saved": f"{sid} handoff saved (attempt {p.get('attempt')})",
        "qc.passed": f"QC PASS {sid}",
        "qc.failed": f"QC FAIL {sid} (stage {p.get('stage')})",
        "qc.not_verified": f"QC NOT VERIFIED {sid} → human review",
        "repair.scheduled": f"Repair scheduled for {sid}: {p.get('strategy')}",
        "repair.exhausted": f"{sid} needs human review",
        "task.invalidated": f"{sid} marked STALE (input changed)",
        "task.revalidate": f"Re-checking {sid}",
        "task.blocked": f"{sid} BLOCKED: {p.get('reason', '')}",
        "task.resumed": f"Resumed {sid} after restart",
        "plan.created": f"Plan created: {p.get('tasks')} tasks",
        "human.decided": f"Human decided {sid}: {p.get('decision')}",
    }.get(t)


class ChatIn(BaseModel):
    text: str


class EngineIn(BaseModel):
    engine: str


class DecideIn(BaseModel):
    task_id: str
    decision: str
    note: str = ""


class StudioService:
    def __init__(self, project_dir: Path, mock_delay: float = 0.0, template: str = "vertical_slice_3d"):
        self.dir = Path(project_dir)
        if not (self.dir / "project.json").exists():
            Studio.init_project(self.dir, "shams_project", "", mode="autonomous", mock_mode=True)
        self.studio = Studio(self.dir, mock=MockWorker(self.dir / "attempts", delay_s=mock_delay))
        self.template = template
        self.lock = threading.RLock()
        self.running = False
        self.chat_file = self.dir / "chat.json"
        self.chat: list[dict] = json.loads(self.chat_file.read_text(encoding="utf-8")) if self.chat_file.exists() else []
        self._thread: threading.Thread | None = None
        if self.studio.db.tasks() and not self.studio.status()["done"]:
            self.start()

    # ------------------------------------------------------------------ run loop
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            while True:
                with self.lock:
                    progressed = self.studio.step()
                if not progressed:
                    break
                time.sleep(0.05)
        finally:
            self.running = False

    # ------------------------------------------------------------------ chat
    def _save_chat(self) -> None:
        self.chat_file.write_text(json.dumps(self.chat, ensure_ascii=False, indent=1), encoding="utf-8")

    def post_chat(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise HTTPException(400, "empty message")
        self.chat.append({"role": "user", "text": text, "ts": contracts.now_iso()})
        st = self.studio
        with self.lock:
            if not st.db.tasks():
                detected = engines.detect(text)
                if detected:
                    st.set_engine(detected)
                profile = engines.get(st.project["engine"]["name"])
                st.project["goal"] = text
                st.context.goal = text
                (self.dir / "project.json").write_text(json.dumps(st.project, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
                dag = instantiate(self.template, st.db.project_id, text, engine=profile.name)
                st.load_plan(dag, approved_by="ui-user")
                how = "nhận diện từ yêu cầu" if detected else "mặc định, ghi tên engine trong yêu cầu để đổi"
                reply = (f"Đã phân tích yêu cầu và tạo {len(dag['tasks'])} nhiệm vụ từ template "
                         f"{self.template} cho engine {profile.display} ({how}). Đang triển khai.")
                if st.mock_mode:
                    reply += " (Chế độ mock: chưa gắn model AI.)"
                self.chat.append({"role": "director", "text": reply, "ts": contracts.now_iso(), "live": True})
                self.start()
            else:
                self.chat.append({"role": "director", "ts": contracts.now_iso(),
                                  "text": "Đã ghi nhận. Planner LLM chưa được gắn (Phase 1b) nên yêu cầu bổ sung "
                                          "chưa tự tạo được nhiệm vụ mới."})
        self._save_chat()
        return self.state()

    # ------------------------------------------------------------------ state for the UI
    def state(self) -> dict:
        st = self.studio
        # Reads use the EventStore's own lock, so a running step never blocks the UI.
        tasks = st.db.tasks()
        leases = st.scheduler.active_leases()
        events = st.db.events(max(0, st.db.q1("SELECT COALESCE(MAX(seq),0) FROM events")[0] - 400))
        arts = [dict(r) for r in st.db.q("SELECT logical_id, version, state, media_type, sha256, produced_task_id "
                                         "FROM artifacts ORDER BY rowid DESC LIMIT 200")]
        qcs = [dict(r) for r in st.db.q("SELECT task_id, attempt, gate, verdict, stopped_at_stage, ts "
                                        "FROM qc_reports ORDER BY rowid DESC LIMIT 200")]
        models = [dict(r) for r in st.db.q("SELECT lane, worker_id, model, loaded_at FROM models")]
        ready = st.db.tasks({S.READY})
        workers = st.nodes.workers
        active = None
        if leases:
            running = [l for l in leases if st.db.state(l["task_id"]) == S.RUNNING]
            l = (running or leases)[0]
            w = workers.get(l["worker_id"])
            active = {"worker_id": l["worker_id"], "label": worker_label(w) if w else l["worker_id"],
                      "lane": l["lane"], "gpu": l["lane"].startswith("gpu:"), "task_id": l["task_id"]}
        vram = None
        gpu_lanes = [ln for ln in st.nodes.lanes() if ln.kind == "gpu"]
        if gpu_lanes:
            lane = gpu_lanes[0]
            used = st.lifecycle.adapters["mock"].used.get(lane.lane_id, 0) if st.mock_mode else 0
            vram = {"lane": lane.lane_id, "used_mb": used, "total_mb": lane.vram_mb, "simulated": st.mock_mode}
        next_worker = None
        for t in sorted(ready, key=lambda t: -t.get("priority", 50)):
            c = st.nodes.candidates(t["capability"])
            if c:
                next_worker = worker_label(c[0])
                break
        task_rows = []
        for t in tasks:
            c = st.nodes.candidates(t["capability"])
            task_rows.append({"task_id": t["task_id"], "title": t["title"], "state": t["state"],
                              "label": STATE_LABEL.get(t["state"], t["state"]), "attempt": t["attempt"],
                              "capability": t["capability"], "depends_on": t.get("depends_on", []),
                              "worker": worker_label(c[0]) if c else None})
        console = []
        for e in events:
            line = console_line(e)
            if line:
                console.append({"ts": e["ts"][11:19], "text": line, "type": e["type"]})
        done = sum(1 for t in tasks if t["state"] == "ACCEPTED")
        goal = st.project.get("goal") or ""
        profile = engines.get((st.project.get("engine") or {}).get("name", "unity"))
        return {
            "app": "SHAMS",
            "engines": [{"id": p.name, "display": p.display, "language": p.language}
                        for p in engines.ENGINES.values()],
            "project": {"id": st.db.project_id, "name": goal[:60] or st.db.project_id,
                        "engine": profile.display, "engine_id": profile.name, "language": profile.language,
                        "dir": str(self.dir)},
            "mode": st.mode, "mock_mode": st.mock_mode, "running": self.running, "connected": True,
            "progress": {"done": done, "total": len(tasks)},
            "chat": self.chat, "tasks": task_rows, "active": active, "vram": vram, "next_worker": next_worker,
            "nodes": list(st.nodes.nodes.values()),
            "workers": [{"worker_id": w.worker_id, "label": worker_label(w), "node_id": w.node_id,
                         "lane": w.lane_kind, "model": w.model, "capabilities": w.capabilities}
                        for w in workers.values()],
            "models": models, "artifacts": arts, "qc": qcs, "console": console[-200:],
            "review": [t for t in task_rows if t["state"] in ("HUMAN_REVIEW", "BLOCKED")],
        }

    def set_engine(self, name: str) -> dict:
        with self.lock:
            try:
                self.studio.set_engine(name)
            except (ValueError, KeyError) as e:
                raise HTTPException(400, str(e))
        return self.state()

    def decide(self, d: DecideIn) -> dict:
        with self.lock:
            try:
                self.studio.human_decide(d.task_id, d.decision, d.note)
            except (ValueError, KeyError) as e:
                raise HTTPException(400, str(e))
        self.start()
        return self.state()


def create_app(project_dir: Path | str, mock_delay: float = 0.0) -> FastAPI:
    svc = StudioService(Path(project_dir), mock_delay)
    app = FastAPI(title="SHAMS", version="0.1.0")
    app.state.svc = svc

    @app.get("/")
    def index():
        return FileResponse(UI_FILE)

    @app.get("/api/state")
    def state():
        return svc.state()

    @app.post("/api/chat")
    def chat(body: ChatIn):
        return svc.post_chat(body.text)

    @app.post("/api/decide")
    def decide(body: DecideIn):
        return svc.decide(body)

    @app.post("/api/engine")
    def engine(body: EngineIn):
        return svc.set_engine(body.engine)

    @app.post("/api/run")
    def run():
        svc.start()
        return {"running": True}

    return app


def serve(project_dir: str, host: str = "127.0.0.1", port: int = 8765, mock_delay: float = 0.8) -> None:
    import uvicorn
    uvicorn.run(create_app(project_dir, mock_delay), host=host, port=port, log_level="warning")

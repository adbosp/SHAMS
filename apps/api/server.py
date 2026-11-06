"""SHAMS local API (FastAPI) + main app UI (ADR-006, ADR-112, ADR-118).

Serves `apps/ui/index.html` and a JSON view of a workspace: a folder of projects, one open at a time.
A background thread drives `Shams.step()` while the UI polls `/api/state`. Settings, model connections,
MCP servers and real hardware are exposed under /api/settings, /api/connections, /api/mcp, /api/hardware.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from shams import __version__, contracts, hardware, packs, providers
from shams import settings as settings_mod
from shams.mcp import MCPManager
from shams.events.store import StoreClosed
from shams.mock.worker import MockWorker
from shams.orchestration.project import Shams as Studio
from shams.scheduling.gate import GPU_GATE
from shams.statemachine import S

UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "index.html"
STATIC_DIR = UI_FILE.parent / "static"

FAMILY_LABEL = {
    "code": "Coding Worker", "integration": "Integration Worker", "plan": "Planner",
    "architect": "Architect", "debug_analysis": "Analysis Worker", "semantic_review": "Review Worker",
    "visual_review": "Visual Review", "image": "Image Generator", "mesh": "3D Generator",
    "rig": "Rigging Worker", "animation": "Animation Worker", "audio": "Audio Generator",
    "build": "Build & Test Tools", "test": "Build & Test Tools", "validate": "Validators",
    "scene": "Scene Capture", "vcs": "Git",
}
STATE_LABEL = {"ACCEPTED": "PASS", "QC_PENDING": "QC", "REPAIR_REQUIRED": "REPAIR", "HUMAN_REVIEW": "REVIEW"}


def worker_label(worker, extra: dict | None = None) -> str:
    fam = worker.capabilities[0].split(".")[0]
    return {**FAMILY_LABEL, **(extra or {})}.get(fam, worker.worker_id)


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
        "hook.failed": f"Hook failed on {p.get('event')}: {p.get('output', '')[:80]}",
    }.get(t)


class ChatIn(BaseModel):
    text: str


class ConfigureIn(BaseModel):
    domain: str | None = None
    options: dict[str, str] = {}


class DecideIn(BaseModel):
    task_id: str
    decision: str
    note: str = ""


class SettingsIn(BaseModel):
    scope: str
    data: dict


class SecretIn(BaseModel):
    name: str
    value: str


class ConnectionTestIn(BaseModel):
    name: str | None = None
    provider: str | None = None
    config: dict = {}


class ProjectIn(BaseModel):
    name: str
    goal: str = ""
    domain: str | None = None
    options: dict[str, str] = {}
    mode: str | None = None
    mock: bool | None = None


class DirectIn(BaseModel):
    preset: str
    params: dict = {}
    confirm_adult: bool = False


class OpenIn(BaseModel):
    id: str


def slug(text: str, fallback: str = "project") -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:40].strip("_")
    if not t or not t[0].isalpha():
        t = f"{fallback}_{t}" if t else fallback
    return t if len(t) >= 2 else f"{t}_project"


class StudioService:
    """A workspace (folder of projects) with at most one open project."""

    def __init__(self, path: Path, mock_delay: float = 0.0, template: str | None = None):
        path = Path(path)
        if (path / "project.json").exists():
            self.workspace, first = path.parent, path
        else:
            self.workspace, first = path, None
            path.mkdir(parents=True, exist_ok=True)
            projects = self.projects()
            if projects:
                first = Path(projects[0]["dir"])
        self.mock_delay, self.template = mock_delay, template
        self.lock = threading.RLock()
        self._open_lock = threading.Lock()
        self.running = False
        self._stop = False
        self._thread: threading.Thread | None = None
        self.studio: Studio | None = None
        self.dir: Path | None = None
        self.chat: list[dict] = []
        self.draft = {"domain": None, "options": {}}
        self.base_url = "http://127.0.0.1:8765"      # serve() sets the real address
        self.open_game = False                       # serve() turns this on: open a finished game in the browser
        self.announce = False                        # off while the service is still being set up
        self._play_servers: dict[str, object] = {}   # playable build folder -> its static server
        self._play_lock = threading.Lock()
        if first:
            self.open(first)

    # ------------------------------------------------------------------ projects
    def projects(self) -> list[dict]:
        out = []
        for pj in self.workspace.glob("*/project.json"):
            if pj.parent.name.startswith("_"):             # _direct, _selftest: internal folders
                continue
            try:
                cfg = json.loads(pj.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"id": cfg.get("project_id"), "dir": str(pj.parent), "folder": pj.parent.name,
                        "goal": (cfg.get("goal") or "")[:80], "domain": cfg.get("domain", "game" if "engine" in cfg
                                                                                 else packs.DEFAULT_DOMAIN),
                        "mock": cfg.get("mock_mode", False), "mtime": pj.parent.stat().st_mtime})
        return sorted(out, key=lambda p: -p["mtime"])

    def open(self, project_dir: Path, resume: bool = False) -> None:
        """Open a project. An unfinished project is NOT resumed on its own (opening the app must not spend
        model time on an old project); the user presses "Tiếp tục chạy" (POST /api/run) or passes resume."""
        with self._open_lock:                   # one switch at a time; repeated clicks must not pile up
            if self.studio is not None and self.dir == Path(project_dir) and not resume:
                return                          # already open
            self._open(project_dir, resume)

    def _open(self, project_dir: Path, resume: bool) -> None:
        self.stop()
        with self.lock:
            if self.studio:
                self.studio.close()
            self.dir = Path(project_dir)
            self.studio = Studio(self.dir, mock=MockWorker(self.dir / "attempts", delay_s=self.mock_delay))
            self.chat_file = self.dir / "chat.json"
            self.chat = json.loads(self.chat_file.read_text(encoding="utf-8")) if self.chat_file.exists() else []
            go = resume and bool(self.studio.db.tasks()) and not self.studio.status()["done"]
        self.announce_playable()
        if go:                                  # decided under the lock: another open() may follow at once
            self.start()

    def open_by_id(self, pid: str) -> dict:
        p = next((p for p in self.projects() if p["id"] == pid or p["folder"] == pid), None)
        if not p:
            raise HTTPException(404, f"no project {pid!r} in {self.workspace}")
        self.open(Path(p["dir"]))
        return self.state()

    def delete_project(self, pid: str, by: str = "ui") -> dict:
        """Delete a project folder permanently (the UI asks for confirmation first). The open project is closed
        first, so its database and run loop let go of the files."""
        import shutil
        import stat
        p = next((p for p in self.projects() if p["id"] == pid or p["folder"] == pid), None)
        if not p:
            raise HTTPException(404, f"no project {pid!r} in {self.workspace}")
        src = Path(p["dir"])
        if src.resolve().parent != self.workspace.resolve():
            raise HTTPException(400, "only project folders directly inside the workspace can be deleted")
        if self.dir and Path(self.dir).resolve() == src.resolve():
            self.close_project()

        def writable(func, path, _exc):          # Windows: read-only files (e.g. git objects) block rmtree
            os.chmod(path, stat.S_IWRITE)
            func(path)
        logd = self.workspace / "_logs"
        logd.mkdir(parents=True, exist_ok=True)
        with open(logd / "deletions.log", "a", encoding="utf-8") as f:   # permanent: keep a trace of who/when
            f.write(json.dumps({"ts": contracts.now_iso(), "project": p["id"], "folder": src.name,
                                "goal": p.get("goal", ""), "by": by}, ensure_ascii=False) + "\n")
        try:
            shutil.rmtree(src, onerror=writable)
        except OSError as e:
            raise HTTPException(409, f"could not delete {src.name}: {e}")
        return self.state()

    def close_project(self) -> dict:
        self.stop()
        with self.lock:
            if self.studio:
                self.studio.close()
            self.studio, self.dir, self.chat = None, None, []
            self.draft = {"domain": None, "options": {}}
        return self.state()

    def _mock_default(self, s: dict) -> bool:
        m = s["general"].get("mock", "auto")
        if m != "auto":
            return m == "on"
        from shams.models import DEFAULT_ROLES, WORKER_PREFIX, merge_role_workers   # same list the project loads
        try:
            # tools that need no setup (default roles, e.g. the web build) do not make a project "real"
            return not [w for w in merge_role_workers(s)
                        if w.get("id") not in {WORKER_PREFIX + r for r in DEFAULT_ROLES}]
        except Exception:  # noqa: BLE001 - an unreachable model server means test mode, not a crash
            return True

    def create_project(self, name: str, goal: str = "", domain: str | None = None, options: dict | None = None,
                       mode: str | None = None, mock: bool | None = None) -> Path:
        s = settings_mod.load(None)
        base = slug(name)
        d, i = self.workspace / base, 2
        while d.exists():
            d, i = self.workspace / f"{base}_{i}", i + 1
        pid = d.name.replace("-", "_")
        try:
            Studio.init_project(d, pid, goal, mode=mode or s["general"].get("default_mode") or "autonomous",
                                mock_mode=self._mock_default(s) if mock is None else mock,
                                domain=domain or s["general"].get("default_domain"), domain_config=options or {})
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
        self.open(d)
        return d

    def new_project(self, body: ProjectIn) -> dict:
        self.create_project(body.name, body.goal, body.domain, body.options, body.mode, body.mock)
        return self.state()

    # ------------------------------------------------------------------ run loop
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.running, self._stop = True, False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=30)

    def _loop(self) -> None:
        try:
            while not self._stop:
                st = self.studio
                if st is None:
                    break
                # mock projects use no GPU; real ones share the GPU with chats and Direct Mode (ADR-122)
                gate = nullcontext() if st.mock_mode else GPU_GATE.hold(f"autonomous:{st.db.project_id}", kind="workflow")
                with gate, self.lock:
                    if self.studio is None:
                        break
                    progressed = self.studio.step()
                self.announce_playable()
                if not progressed:
                    break
                time.sleep(0.05)
        except Exception as e:  # noqa: BLE001 - a crashed run loop must be visible, not a silent stop
            import traceback
            tb = traceback.format_exc()
            if self.dir:
                logd = self.workspace / "_logs"
                logd.mkdir(parents=True, exist_ok=True)
                with open(logd / "run_loop_errors.log", "a", encoding="utf-8") as f:
                    f.write(f"\n=== {contracts.now_iso()} {self.dir.name}\n{tb}")
                self.chat.append({"role": "director", "ts": contracts.now_iso(),
                                  "text": f"Vòng chạy dừng vì lỗi: {type(e).__name__}: {e}. Xem _logs/run_loop_errors.log."})
                self._save_chat()
        finally:
            self.running = False

    # ------------------------------------------------------------------ playable builds
    def playable(self) -> dict[str, dict]:
        """Accepted tasks whose worker left a playable copy (handoff decision "playable build" naming a folder
        with index.html): {task_id: {"dir", "attempt", "failed"}}."""
        out: dict[str, dict] = {}
        st = self.studio
        if st is None:
            return out
        try:
            for t in st.db.tasks({S.ACCEPTED}):
                h = st.db.handoff(t["task_id"]) or {}
                for d in h.get("decisions") or []:
                    if d.get("decision") == "playable build":
                        root = Path(str(d.get("reason") or ""))
                        if (root / "index.html").is_file():
                            reps = [r for r in st.db.qc_reports(t["task_id"]) if r.get("attempt") == h.get("attempt")]
                            crit = reps[-1].get("criteria") or [] if reps else []
                            failed = [c["criterion_id"] for c in crit if c.get("verdict") == "FAIL"]
                            out[t["task_id"]] = {"dir": root, "attempt": h.get("attempt"), "failed": failed}
        except StoreClosed:
            return {}
        return out

    def game_server(self, root: Path) -> str:
        """Start (once per folder) a static server on 127.0.0.1 for a playable build; returns its base URL."""
        import functools
        import http.server
        key = str(Path(root).resolve())
        with self._play_lock:
            srv = self._play_servers.get(key)
            if srv is None:
                class Handler(http.server.SimpleHTTPRequestHandler):
                    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                                      ".js": "text/javascript", ".mjs": "text/javascript",
                                      ".glb": "model/gltf-binary", ".wasm": "application/wasm"}

                    def log_message(self, *a):
                        pass

                    def end_headers(self):
                        self.send_header("Cache-Control", "no-store")
                        super().end_headers()
                srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=key))
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                self._play_servers[key] = srv
        return f"http://127.0.0.1:{srv.server_address[1]}/"

    def play_url(self, task_id: str) -> str:
        return f"{self.base_url}/play/{task_id}/"

    def announce_playable(self) -> None:
        """Once per accepted build: post the local link in the project chat and open it in the browser."""
        if not self.announce:
            return
        try:
            builds = self.playable()
        except Exception:  # noqa: BLE001 - announcing must never stop the run loop
            return
        done = {m.get("play") for m in self.chat if m.get("play")}
        new = [(tid, b) for tid, b in builds.items() if f"{tid}@{b['attempt']}" not in done]
        for tid, b in new:
            url = self.play_url(tid)
            if b.get("failed"):
                text = (f"Game đã build và được duyệt tay, nhưng kiểm tra chưa đạt: {', '.join(b['failed'])}. "
                        f"Game có thể lỗi khi chạy (ví dụ màn hình đen). Mở tại {url} (file nằm ở {b['dir']}).")
            else:
                text = f"Game đã build xong và qua kiểm tra. Chơi thử tại {url} (file nằm ở {b['dir']})."
            self.chat.append({"role": "director", "ts": contracts.now_iso(), "play": f"{tid}@{b['attempt']}",
                              "url": url, "text": text})
            if self.open_game:
                import webbrowser
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        if new:
            self._save_chat()

    # ------------------------------------------------------------------ chat
    def _save_chat(self) -> None:
        if self.dir:
            self.chat_file.write_text(json.dumps(self.chat, ensure_ascii=False, indent=1), encoding="utf-8")

    def post_chat(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise HTTPException(400, "empty message")
        if self.studio is None:
            domain = self.draft["domain"] or packs.detect_domain(text)
            self.create_project(text[:40], text, domain, self.draft["options"] or None)
        st = self.studio
        self.chat.append({"role": "user", "text": text, "ts": contracts.now_iso()})
        with self.lock:
            if not st.db.tasks():
                domain = packs.detect_domain(text)
                target = packs.get(domain) if domain else st.pack
                hints = target.detect(text)
                if (domain and target.name != st.pack.name) or hints:
                    st.configure(domain=target.name, **hints)
                st.project["goal"] = text
                st.context.goal = text
                (self.dir / "project.json").write_text(json.dumps(st.project, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
                try:
                    dag = st.plan_from_template(self.template, goal=text, approved_by="ui-user")
                except (ValueError, KeyError, PermissionError) as e:
                    self.chat.append({"role": "director", "ts": contracts.now_iso(), "text": f"Không tạo được plan: {e}"})
                    self._save_chat()
                    return self.state()
                how = ("nhận diện từ yêu cầu" if (domain or hints)
                       else "mặc định; ghi rõ lĩnh vực hoặc công nghệ trong yêu cầu để đổi")
                reply = (f"Đã tạo {len(dag['tasks'])} nhiệm vụ từ khung {dag['template']} cho {st.describe()} "
                         f"({how}). Đang triển khai. Khung này chưa đọc nội dung chi tiết của yêu cầu; "
                         f"Intent & Scope Manager (V2.1) sẽ tách nhiệm vụ theo từng dự án.")
                if st.mock_mode:
                    reply += (" Project này chạy chế độ thử (không gọi model AI). Thêm model trong Settings → "
                              "Model & API để chạy thật.")
                else:
                    missing = sorted({t["capability"] for t in st.db.tasks()       # same matching as the scheduler
                                      if not st.nodes.candidates(t["capability"])})
                    if missing:
                        reply += " Chưa có worker cho: " + ", ".join(missing) + " (Settings → Model & API)."
                self.chat.append({"role": "director", "text": reply, "ts": contracts.now_iso(), "live": True})
                self.start()
            else:
                self.chat.append({"role": "director", "ts": contracts.now_iso(),
                                  "text": "Đã ghi nhận. Planner LLM chưa được gắn (Phase 1b) nên yêu cầu bổ sung "
                                          "chưa tự tạo được nhiệm vụ mới."})
        self._save_chat()
        return self.state()

    # ------------------------------------------------------------------ hardware
    def hardware_view(self) -> dict:
        hw = hardware.detect()
        g = hw["gpus"][0] if hw["gpus"] else None
        vram = None
        if g:
            vram = {"name": g["name"], "used_mb": g["vram_used_mb"], "total_mb": g["vram_total_mb"],
                    "utilization_pct": g["utilization_pct"], "source": g["source"], "count": len(hw["gpus"]),
                    "integrated": bool(g.get("integrated"))}
        return {**hw, "vram": vram}

    # ------------------------------------------------------------------ state for the UI
    def _packs(self) -> list[dict]:
        return [{"id": p.name, "display": p.display, "description": p.description, "options": p.options(self.dir)}
                for p in sorted(packs.all_packs(), key=lambda p: (p.name != packs.DEFAULT_DOMAIN, p.name))]

    def state(self) -> dict:
        """UI state. A project switch can close the open project mid-read; that is retried, never fatal."""
        for _ in range(20):
            try:
                return self._state()
            except StoreClosed:
                time.sleep(0.1)
        return {"app": "SHAMS", "version": __version__, "connected": True, "switching": True,
                "running": self.running, "projects": self.projects()}

    def _state(self) -> dict:
        hw = self.hardware_view()
        base = {"app": "SHAMS", "version": __version__, "packs": self._packs(), "connected": True,
                "workspace": str(self.workspace), "projects": self.projects(), "hardware": hw, "vram": hw["vram"],
                "running": self.running}
        st = self.studio
        if st is None:
            pack = packs.get(self.draft["domain"] or settings_mod.load(None)["general"].get("default_domain"))
            cfg = pack.normalize_config(self.draft["options"])
            sel = {o: (cfg.get(o).get("name") if isinstance(cfg.get(o), dict) else cfg.get(o)) for o in pack.options()}
            s = settings_mod.load(None)
            return {**base, "project": {"id": None, "name": "Dự án mới", "domain": pack.name,
                                        "domain_display": pack.display, "setup": f"{pack.display} · {pack.describe(cfg)}",
                                        "selected": sel, "dir": None},
                    "mode": s["general"].get("default_mode", "autonomous"), "mock_mode": self._mock_default(s),
                    "progress": {"done": 0, "total": 0}, "chat": [], "tasks": [], "active": None, "next_worker": None,
                    "nodes": [], "workers": [], "models": [], "artifacts": [], "qc": [], "console": [], "review": []}
        labels = st.pack.worker_labels()
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
            active = {"worker_id": l["worker_id"], "label": worker_label(w, labels) if w else l["worker_id"],
                      "lane": l["lane"], "gpu": l["lane"].startswith("gpu:"), "task_id": l["task_id"],
                      "model": w.model if w else None}
        next_worker = None
        for t in sorted(ready, key=lambda t: -t.get("priority", 50)):
            c = st.nodes.candidates(t["capability"])
            if c:
                next_worker = worker_label(c[0], labels)
                break
        task_rows = []
        for t in tasks:
            c = st.nodes.candidates(t["capability"])
            task_rows.append({"task_id": t["task_id"], "title": t["title"], "state": t["state"],
                              "label": STATE_LABEL.get(t["state"], t["state"]), "attempt": t["attempt"],
                              "capability": t["capability"], "depends_on": t.get("depends_on", []),
                              "worker": worker_label(c[0], labels) if c else None})
        console = []
        for e in events:
            line = console_line(e)
            if line:
                console.append({"ts": e["ts"][11:19], "text": line, "type": e["type"]})
        done = sum(1 for t in tasks if t["state"] == "ACCEPTED")
        goal = st.project.get("goal") or ""
        return {
            **base,
            "project": {"id": st.db.project_id, "name": goal[:60] or st.db.project_id,
                        "domain": st.pack.name, "domain_display": st.pack.display, "setup": st.describe(),
                        "selected": self._selected(), "dir": str(self.dir)},
            "mode": st.mode, "mock_mode": st.mock_mode,
            "progress": {"done": done, "total": len(tasks)},
            "chat": self.chat, "tasks": task_rows, "active": active, "next_worker": next_worker,
            "nodes": list(st.nodes.nodes.values()),
            "workers": [{"worker_id": w.worker_id, "label": worker_label(w, labels), "node_id": w.node_id,
                         "lane": w.lane_kind, "model": w.model, "capabilities": w.capabilities,
                         "provider": w.ref.get("provider"), "mcp": (w.entry.get("provider_config") or {}).get("mcp")}
                        for w in workers.values()],
            "models": models, "artifacts": arts, "qc": qcs, "console": console[-200:],
            "review": [t for t in task_rows if t["state"] in ("HUMAN_REVIEW", "BLOCKED")],
        }

    def _selected(self) -> dict[str, str]:
        """Current choice per pack option, as option ids (e.g. {"engine": "godot"})."""
        cfg, out = self.studio.domain_config, {}
        for opt in self.studio.pack.options(self.dir):
            v = cfg.get(opt)
            out[opt] = v.get("name") if isinstance(v, dict) else v
        return out

    def configure(self, body: "ConfigureIn") -> dict:
        with self.lock:
            try:
                if self.studio is None:
                    pack = packs.get(body.domain or self.draft["domain"])
                    same = pack.name == (self.draft["domain"] or packs.DEFAULT_DOMAIN)
                    cfg = pack.configure(self.draft["options"] if same else {}, None, **body.options)
                    self.draft = {"domain": pack.name,
                                  "options": {k: (v.get("name") if isinstance(v, dict) else v) for k, v in cfg.items()}}
                else:
                    self.studio.configure(domain=body.domain, **body.options)
            except (ValueError, KeyError) as e:
                raise HTTPException(400, str(e))
        return self.state()

    def decide(self, d: DecideIn) -> dict:
        if self.studio is None:
            raise HTTPException(400, "no project open")
        with self.lock:
            try:
                self.studio.human_decide(d.task_id, d.decision, d.note)
            except (ValueError, KeyError) as e:
                raise HTTPException(400, str(e))
        self.announce_playable()
        self.start()
        return self.state()

    # ------------------------------------------------------------------ settings
    def settings_view(self) -> dict:
        try:
            merged = settings_mod.load(self.dir)
            errors = settings_mod.validate(merged)
        except settings_mod.SettingsError as e:
            merged, errors = settings_mod.load(None), [str(e)]
        scopes = {}
        for sc in settings_mod.SCOPES:
            if sc != "user" and self.dir is None:
                continue
            try:
                scopes[sc] = settings_mod.read_scope(sc, self.dir)
            except settings_mod.SettingsError as e:
                scopes[sc], errors = {}, errors + [str(e)]
        return {"merged": merged, "scopes": scopes, "sources": settings_mod.sources(self.dir), "errors": errors,
                "env": settings_mod.env_status(merged), "providers": providers.available(),
                "presets": {k: v[1] for k, v in providers.PRESETS.items()},
                "permission_modes": list(settings_mod.PERMISSION_MODES), "mock_modes": list(settings_mod.MOCK_MODES),
                "project_open": self.dir is not None, "running": self.running}

    def save_settings(self, body: SettingsIn) -> dict:
        if body.scope != "user" and self.dir is None:
            raise HTTPException(400, "open a project to edit project settings")
        try:
            settings_mod.save(body.scope, body.data, self.dir)
        except settings_mod.SettingsError as e:
            raise HTTPException(400, str(e))
        self.reload()
        return self.settings_view()

    def reload(self) -> None:
        """Re-open the project so new settings (workers, MCP, hooks) take effect; waits for a running step."""
        if self.dir is not None:
            was = self.running
            self.open(self.dir)
            if was:
                self.start()

    def test_connection(self, body: ConnectionTestIn) -> dict:
        s = settings_mod.load(self.dir)
        if body.name:
            c = dict((s.get("connections") or {}).get(body.name) or {})
            if not c:
                raise HTTPException(404, f"no connection {body.name!r}")
            ptype = c.pop("provider", None)
        else:
            c, ptype = dict(body.config), body.provider
        c.pop("models", None)
        c.pop("label", None)
        if not ptype:
            raise HTTPException(400, "provider is required")
        out = {"provider": ptype, "ok": False, "health": None, "models": [], "error": None}
        try:
            prov = providers.create(ptype, c)
            if hasattr(prov, "health"):
                out["health"] = prov.health()
            if hasattr(prov, "list_models"):
                out["models"] = prov.list_models()[:200]
            out["ok"] = out["health"] in (None, "ok")
        except Exception as e:
            out["error"] = str(e)
        return out

    def mcp_status(self, name: str | None = None) -> list[dict]:
        s = settings_mod.load(self.dir)
        servers = s.get("mcp_servers") or {}
        if name:
            if name not in servers:
                raise HTTPException(404, f"no MCP server {name!r}")
            servers = {name: {**servers[name], "enabled": True}}
        mgr = MCPManager(servers, None, s.get("env"))
        from shams.mcp import Permissions
        mgr.permissions = Permissions(s.get("permissions"))
        try:
            return mgr.status()
        finally:
            mgr.close()


class DirectService:
    """Direct Mode (ADR-121): one prompt on one chosen preset. Tasks live in ``<workspace>/_direct`` and run on
    the same scheduler, GPU lease, workers, artifact registry, QC and event log as planned projects."""

    def __init__(self, workspace: Path):
        self.dir = Path(workspace) / "_direct"
        self.lock = threading.RLock()
        self.studio: Studio | None = None
        self.threads: dict[str, threading.Thread] = {}

    def _open(self) -> Studio:
        with self.lock:
            if self.studio is None:
                if not (self.dir / "project.json").exists():
                    Studio.init_project(self.dir, "direct", "Direct Mode tasks", mode="guided", domain="software",
                                        domain_config={"stack": "python"})
                self.studio = Studio(self.dir)
            return self.studio

    def close(self) -> None:
        with self.lock:
            if self.studio:
                self.studio.close()
            self.studio = None

    def presets(self) -> dict:
        from shams import policy
        from shams.models import ModelRegistry
        s = settings_mod.load(None)
        out = []
        for p in ModelRegistry(s).presets():
            w = p.pop("worker", None)
            p["worker_id"] = w["id"] if w else None
            out.append(p)
        return {"presets": out, "confirm_text": policy.CONFIRM_TEXT,
                "allow_adult_content": bool(s["general"].get("allow_adult_content", False))}

    def queue(self, body: DirectIn) -> str:
        """Validate and queue one Direct Mode task; returns its id (``execute`` runs it)."""
        from shams import policy
        st = self._open()
        with self.lock:
            try:
                st.settings = settings_mod.load(self.dir)          # pick up presets added since the app started
                return st.direct(body.preset, body.params, by="human", confirm_adult=body.confirm_adult)
            except policy.PolicyError as e:
                raise HTTPException(403, str(e))
            except KeyError as e:
                raise HTTPException(404, str(e).strip("'\""))
            except (ValueError, PermissionError, RuntimeError) as e:
                raise HTTPException(400, str(e))

    def execute(self, tid: str) -> dict:
        """Run a queued task to its final state, holding the process-wide GPU gate (ADR-122)."""
        st = self._open()
        t = st.db.task(tid)
        with GPU_GATE.hold(f"direct:{tid}", model=t.get("preset"), kind="direct"):
            with self.lock:
                return st.run_task(tid)

    def run(self, body: DirectIn) -> dict:
        tid = self.queue(body)
        t = threading.Thread(target=self.execute, args=(tid,), daemon=True)
        self.threads[tid] = t
        t.start()
        return self.task(tid)

    def tasks(self, limit: int = 30) -> list[dict]:
        st = self._open()
        rows = [t for t in st.db.tasks() if t["task_id"].startswith("task.direct.")]
        return [self._view(st, t, events=False) for t in rows[::-1][:limit]]

    def task(self, tid: str) -> dict:
        st = self._open()
        try:
            t = st.db.task(tid)
        except KeyError:
            raise HTTPException(404, f"no direct task {tid}") from None
        return self._view(st, t, events=True)

    def _view(self, st: Studio, t: dict, events: bool) -> dict:
        tid = t["task_id"]
        arts = [a for a in (st.artifacts.accepted(o["logical_id"]) for o in t["outputs"]) if a]
        h = st.db.handoff(tid) or {}
        v = {"task_id": tid, "state": t["state"], "preset": t.get("preset"), "title": t["title"],
             "params": {k: val for k, val in (t.get("params") or {}).items() if not k.startswith("_")},
             "running": tid in self.threads and self.threads[tid].is_alive(),
             "summary": h.get("summary"), "error": h.get("blocked_reason"), "usage": h.get("usage"),
             "outputs": [{"logical_id": a["logical_id"], "media_type": a["media_type"], "sha256": a["sha256"],
                          "url": f"/api/direct/artifact/{a['sha256']}"} for a in arts]}
        if events:
            v["events"] = [{"seq": r["seq"], "type": r["type"], "at": r["ts"],
                            "subject": r["subject_id"]}
                           for r in st.db.q("SELECT * FROM events WHERE subject_id=? OR (type LIKE 'model.%' AND seq > "
                                            "(SELECT COALESCE(MIN(seq),0) FROM events WHERE subject_id=?)) ORDER BY seq",
                                            (tid, tid))][-60:]
        return v

    def artifact(self, sha: str) -> FileResponse:
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise HTTPException(400, "bad id")
        st = self._open()
        row = st.db.q1("SELECT media_type FROM artifacts WHERE sha256=? LIMIT 1", (sha,))
        if row is None:
            raise HTTPException(404, "not found")
        return FileResponse(st.artifacts.blob_path(sha), media_type=row[0])


def create_app(project_dir: Path | str, mock_delay: float = 0.0, token: str | None = None) -> FastAPI:
    svc = StudioService(Path(project_dir), mock_delay)
    svc.announce = True
    app = FastAPI(title="SHAMS", version=__version__)
    app.state.svc = svc
    direct = DirectService(svc.workspace)
    app.state.direct = direct

    @app.get("/")
    def index():
        return FileResponse(UI_FILE, headers={"Cache-Control": "no-store"})

    @app.get("/static/{name}")
    def static(name: str):
        f = STATIC_DIR / name
        if f.parent != STATIC_DIR or not f.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(f, headers={"Cache-Control": "max-age=86400"})

    @app.get("/play/{task_id}/{path:path}")
    def play(task_id: str, path: str = ""):
        """Stable link to a playable build. The game is served from the ROOT of its own local port, so the
        model's absolute asset paths (/models/...) work as well as relative ones."""
        b = svc.playable().get(task_id)
        if not b:
            raise HTTPException(404, "no playable build for this task")
        return RedirectResponse(svc.game_server(Path(b["dir"])) + path, status_code=307)

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(STATIC_DIR / "favicon.ico")

    @app.get("/api/state")
    def state():
        return svc.state()

    @app.get("/api/hardware")
    def hw():
        return svc.hardware_view()

    @app.post("/api/chat")
    def chat(body: ChatIn):
        return svc.post_chat(body.text)

    @app.post("/api/decide")
    def decide(body: DecideIn):
        return svc.decide(body)

    @app.post("/api/configure")
    def configure(body: ConfigureIn):
        return svc.configure(body)

    @app.post("/api/run")
    def run():
        svc.start()
        return {"running": True}

    @app.get("/api/projects")
    def projects():
        return {"workspace": str(svc.workspace), "projects": svc.projects()}

    @app.post("/api/projects")
    def new_project(body: ProjectIn):
        return svc.new_project(body)

    @app.post("/api/projects/delete")
    def delete_project(body: OpenIn):
        return svc.delete_project(body.id)

    @app.post("/api/projects/open")
    def open_project(body: OpenIn):
        return svc.open_by_id(body.id)

    @app.post("/api/projects/close")
    def close_project():
        return svc.close_project()

    @app.get("/api/settings")
    def get_settings():
        return svc.settings_view()

    @app.put("/api/settings")
    def put_settings(body: SettingsIn):
        return svc.save_settings(body)

    @app.post("/api/secret")
    def secret(body: SecretIn):
        try:
            settings_mod.set_session_secret(body.name, body.value)
        except settings_mod.SettingsError as e:
            raise HTTPException(400, str(e))
        svc.reload()
        return {"name": body.name, "set": True, "persisted": False}

    @app.post("/api/connections/test")
    def test_connection(body: ConnectionTestIn):
        return svc.test_connection(body)

    @app.get("/api/mcp")
    def mcp(name: str | None = None):
        return svc.mcp_status(name)

    @app.get("/api/direct/presets")
    def direct_presets():
        return direct.presets()

    @app.post("/api/direct/run")
    def direct_run(body: DirectIn):
        return direct.run(body)

    @app.get("/api/direct/tasks")
    def direct_tasks():
        return {"tasks": direct.tasks()}

    @app.get("/api/direct/tasks/{task_id}")
    def direct_task(task_id: str):
        return direct.task(task_id)

    @app.get("/api/direct/artifact/{sha}")
    def direct_artifact(sha: str):
        return direct.artifact(sha)

    from apps.api.desktop.routes import install       # desktop client API (ADR-122)
    install(app, svc, direct, token)
    return app


def serve(path: str, host: str = "127.0.0.1", port: int = 8765, mock_delay: float = 0.8,
          open_browser: bool | None = None, token: str | None = None, ready_line: bool = False) -> None:
    import uvicorn
    app = create_app(path, mock_delay, token=token)
    if open_browser is None:
        open_browser = bool(settings_mod.load(None)["general"].get("open_browser", True))
    svc = app.state.svc
    svc.base_url = f"http://{host}:{port}"
    svc.open_game = bool(settings_mod.load(None)["general"].get("open_game", True))
    svc.announce_playable()             # a game built in an earlier session is announced (and opened) now
    if open_browser:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    app.state.server = server                       # /api/desktop/shutdown stops it gracefully
    crash = enable_crash_log(app.state.svc.workspace)
    print(f"SHAMS {__version__} — http://{host}:{port}/  (workspace: {app.state.svc.workspace}, crash log: {crash})",
          flush=True)
    if ready_line:
        print(json.dumps({"shams_backend": "starting", "port": port, "pid": __import__("os").getpid()}), flush=True)
    try:
        server.run()
    finally:
        app.state.svc.stop()
        app.state.desktop.store.close()


def enable_crash_log(workspace: Path) -> Path:
    """A native crash (access violation in the interpreter or an extension) leaves no Python traceback; the
    fault handler writes the stack of every thread to a file so the cause can be found afterwards."""
    import faulthandler
    d = Path(workspace) / "_logs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "crash.log"
    f = open(path, "a", encoding="utf-8")          # noqa: SIM115 - must stay open for the process lifetime
    f.write(f"\n=== SHAMS {__version__} started {contracts.now_iso()} pid {os.getpid()} ===\n")
    f.flush()
    faulthandler.enable(file=f, all_threads=True)
    _CRASH_FILES.append(f)
    return path


_CRASH_FILES: list = []

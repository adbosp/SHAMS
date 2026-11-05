"""HTTP routes of the desktop client (ADR-122), mounted on the existing local API.

All routes live under ``/api/desktop``; streams are Server-Sent Events. When the desktop shell starts the
backend it passes a random token (``SHAMS_DESKTOP_TOKEN``); every ``/api`` route except ``/api/health`` then
requires it (header ``X-Shams-Token`` or ``?token=`` for EventSource / <img> URLs).
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from shams import __version__
from shams.scheduling.gate import GPU_GATE

from . import models_manager as mm
from .engine import DesktopService
from .tools import MODE_LABELS, MODES, Workspace

DESKTOP_ORIGINS = ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
                   "http://localhost:1420", "http://127.0.0.1:1420"]
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


class ConvIn(BaseModel):
    title: str | None = None
    mode: str | None = None
    preset: str | None = None
    workspace_id: str | None = None
    permission_mode: str | None = None
    pinned: bool | None = None
    archived: bool | None = None


class SendIn(BaseModel):
    text: str = ""
    attachments: list[dict] = []
    preset: str | None = None
    mode: str | None = None
    params: dict = {}
    confirm_adult: bool = False


class DecisionIn(BaseModel):
    approve: bool
    note: str = ""


class WorkspaceIn(BaseModel):
    name: str | None = None
    path: str | None = None
    create_folder: bool = False
    env: dict[str, str] | None = None
    shell: str | None = None
    python: str | None = None
    notes: str | None = None


class PresetIn(BaseModel):
    id: str
    config: dict = {}


class EnableIn(BaseModel):
    enabled: bool


class OllamaIn(BaseModel):
    model: str | None = None
    keep_alive: Any = "10m"


class RegisterIn(BaseModel):
    inventory_id: str
    preset_id: str | None = None


def _err(e: Exception) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, KeyError):
        return HTTPException(404, str(e).strip("'\""))
    if isinstance(e, (ValueError, PermissionError)):
        return HTTPException(400, str(e))
    return HTTPException(409, str(e))


def install(app: FastAPI, svc: Any, direct: Any, token: str | None = None) -> DesktopService:
    desk = DesktopService(svc.workspace, direct, svc)
    app.state.desktop = desk
    app.state.token = token
    started = time.time()

    app.add_middleware(CORSMiddleware, allow_origins=DESKTOP_ORIGINS, allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        tok = app.state.token
        p = request.url.path
        if tok and p.startswith("/api/") and p != "/api/health" and request.method != "OPTIONS":
            given = request.headers.get("x-shams-token") or request.query_params.get("token") or ""
            if not hmac.compare_digest(given, tok):
                return JSONResponse({"detail": "missing or wrong desktop token"}, status_code=401)
        return await call_next(request)

    # ------------------------------------------------------------------ health / lifecycle
    @app.get("/api/health")
    def health():
        return {"ok": True, "app": "SHAMS", "version": __version__, "pid": os.getpid(), "workspace": str(svc.workspace),
                "uptime_s": round(time.time() - started, 1), "token_required": bool(app.state.token)}

    @app.post("/api/desktop/shutdown")
    def shutdown():
        server = getattr(app.state, "server", None)
        if server is None:
            raise HTTPException(409, "not started by `shams desktop-backend`; stop the process instead")
        threading.Timer(0.3, lambda: setattr(server, "should_exit", True)).start()
        return {"stopping": True}

    @app.get("/api/desktop/info")
    def info():
        return {"version": __version__, "workspace": str(svc.workspace), "projects_root": str(desk.projects_root()),
                "permission_modes": [{"id": m, "label": MODE_LABELS[m]} for m in MODES],
                "default_permission_mode": desk.desktop_cfg()["permission_mode"], "gpu_gate": GPU_GATE.status()}

    # ------------------------------------------------------------------ conversations
    @app.get("/api/desktop/conversations")
    def conversations(q: str = "", archived: bool = False):
        return {"conversations": desk.store.conversations(q, archived)}

    @app.post("/api/desktop/conversations")
    def new_conversation(body: ConvIn):
        pm = body.permission_mode or desk.desktop_cfg()["permission_mode"]
        if pm not in MODES:
            raise HTTPException(400, f"permission_mode: one of {MODES}")
        return desk.store.create_conversation(body.title or "Cuộc trò chuyện mới", body.mode or "chat", body.preset,
                                              body.workspace_id, pm)

    @app.get("/api/desktop/conversations/{cid}")
    def conversation(cid: str):
        c = desk.store.conversation(cid)
        if c is None:
            raise HTTPException(404, "no conversation")
        msgs = desk.store.messages(cid)
        active = [m["job_id"] for m in msgs if m["status"] == "streaming" and m.get("job_id")]
        return {"conversation": c, "messages": msgs, "active_jobs": active,
                "pending_approvals": [a for a in desk.store.approvals("pending") if a["conversation_id"] == cid]}

    @app.patch("/api/desktop/conversations/{cid}")
    def update_conversation(cid: str, body: ConvIn):
        if body.permission_mode and body.permission_mode not in MODES:
            raise HTTPException(400, f"permission_mode: one of {MODES}")
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        for k in ("pinned", "archived"):
            if k in fields:
                fields[k] = int(fields[k])
        c = desk.store.update_conversation(cid, **fields)
        if c is None:
            raise HTTPException(404, "no conversation")
        return c

    @app.delete("/api/desktop/conversations/{cid}")
    def delete_conversation(cid: str):
        desk.store.delete_conversation(cid)
        return {"deleted": cid}

    @app.post("/api/desktop/conversations/{cid}/send")
    def send(cid: str, body: SendIn):
        try:
            return desk.send(cid, body.text, attachments=body.attachments, preset=body.preset, mode=body.mode,
                             params=body.params, confirm_adult=body.confirm_adult)
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    # ------------------------------------------------------------------ jobs / streams
    @app.get("/api/desktop/jobs")
    def jobs(kind: str | None = None, limit: int = 100):
        return {"jobs": desk.store.jobs(limit, kind)}

    @app.get("/api/desktop/jobs/{jid}/stream")
    def stream(jid: str, request: Request, after: int = 0):
        last = request.headers.get("last-event-id")
        start = int(last) if last and last.isdigit() else after
        return StreamingResponse(desk.bus.sse(jid, start), media_type="text/event-stream", headers=SSE_HEADERS)

    @app.post("/api/desktop/jobs/{jid}/cancel")
    def cancel(jid: str):
        try:
            return desk.cancel(jid)
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    # ------------------------------------------------------------------ approvals
    @app.get("/api/desktop/approvals")
    def approvals(status: str | None = "pending"):
        return {"approvals": desk.store.approvals(status)}

    @app.post("/api/desktop/approvals/{aid}")
    def decide(aid: str, body: DecisionIn):
        try:
            return desk.decide(aid, body.approve, body.note)
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    @app.get("/api/desktop/tool-log")
    def tool_log(conversation_id: str | None = None):
        return {"entries": desk.store.tool_log(conversation_id)}

    @app.post("/api/desktop/jobs/{jid}/revert")
    def revert(jid: str):
        job = desk.store.job(jid)
        if job is None:
            raise HTTPException(404, "no job")
        conv = desk.store.conversation(job["conversation_id"]) if job.get("conversation_id") else None
        ws_row = desk.store.workspace(conv["workspace_id"]) if conv and conv.get("workspace_id") else None
        if ws_row is None:
            raise HTTPException(400, "job has no workspace")
        ws = Workspace(ws_row["path"], checkpoint_root=desk.dir / "checkpoints")
        restored = ws.revert(jid)
        desk.store.log_tool(jid, job.get("conversation_id"), "revert", {"job_id": jid}, "user", ", ".join(restored))
        return {"restored": restored}

    # ------------------------------------------------------------------ uploads (raw body: no multipart dependency)
    @app.post("/api/desktop/uploads")
    async def upload(request: Request, name: str = "file"):
        data = await request.body()
        if len(data) > 200 * 1024 * 1024:
            raise HTTPException(413, "file larger than 200 MB")
        return desk.save_upload(name, data, request.headers.get("content-type"))

    @app.get("/api/desktop/uploads/{uid}")
    def get_upload(uid: str):
        try:
            p = desk.upload_path(uid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not p.exists():
            raise HTTPException(404, "not found")
        return FileResponse(p)

    # ------------------------------------------------------------------ presets / models
    @app.get("/api/desktop/presets")
    def presets():
        return {"presets": desk.presets()}

    @app.get("/api/desktop/models")
    def models():
        return mm.report()

    @app.post("/api/desktop/models/presets")
    def upsert(body: PresetIn):
        try:
            return {"id": body.id, "config": mm.upsert_preset(body.id, body.config)}
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    @app.post("/api/desktop/models/register")
    def register(body: RegisterIn):
        try:
            pid, cfg = mm.suggest_preset(body.inventory_id, mm.report())
            pid = body.preset_id or pid
            return {"id": pid, "config": mm.upsert_preset(pid, cfg)}
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    @app.post("/api/desktop/models/presets/{pid}/enabled")
    def enable(pid: str, body: EnableIn):
        try:
            return mm.set_enabled(pid, body.enabled)
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    @app.delete("/api/desktop/models/presets/{pid}")
    def remove(pid: str):
        try:
            mm.remove_preset(pid)
            return {"removed": pid}
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    @app.post("/api/desktop/models/ollama/load")
    def oload(body: OllamaIn):
        if not body.model:
            raise HTTPException(400, "model is required")
        try:
            with GPU_GATE.hold("model-manager", model=body.model, kind="llm", timeout=5):
                return mm.ollama_load(body.model, body.keep_alive)
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    @app.post("/api/desktop/models/ollama/unload")
    def ounload(body: OllamaIn):
        try:
            return mm.ollama_unload(body.model)
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    @app.post("/api/desktop/models/comfyui/free")
    def cfree():
        try:
            return mm.comfy_free()
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    @app.post("/api/desktop/models/comfyui/start")
    def cstart():
        try:
            return mm.comfy_start()
        except Exception as e:  # noqa: BLE001
            raise _err(e)

    # ------------------------------------------------------------------ resources
    @app.get("/api/desktop/resources")
    def resources():
        out: dict[str, Any] = {"hardware": svc.hardware_view(), "gpu_gate": GPU_GATE.status(),
                               "jobs": [j for j in desk.store.jobs(30) if j["status"] in ("queued", "running")]}
        try:
            out["ollama_loaded"] = mm.ollama_provider().ps()
        except Exception as e:  # noqa: BLE001
            out["ollama_error"] = str(e)
        cu = mm.comfy()
        if cu is not None:
            try:
                out["comfyui"] = cu.resources() if cu.health() == "ok" else {"status": "down"}
            except Exception as e:  # noqa: BLE001
                out["comfyui"] = {"status": "error", "error": str(e)}
        return out

    # ------------------------------------------------------------------ media gallery
    @app.get("/api/desktop/media")
    def media(limit: int = 200):
        items = []
        for m in desk.store.q("SELECT id, conversation_id, preset, blocks, created FROM messages WHERE role='assistant' "
                              "AND (blocks LIKE '%\"image\"%' OR blocks LIKE '%\"video\"%' OR blocks LIKE '%\"model3d\"%') "
                              "ORDER BY created DESC LIMIT ?", (limit,)):
            for b in m["blocks"]:
                if b.get("type") in ("image", "video", "model3d", "audio"):
                    items.append({**b, "message_id": m["id"], "conversation_id": m["conversation_id"],
                                  "preset": m["preset"], "created": m["created"]})
        return {"items": items}

    # ------------------------------------------------------------------ local workspaces (Projects)
    @app.get("/api/desktop/workspaces")
    def workspaces():
        rows = desk.store.workspaces()
        for r in rows:
            r["exists"] = Path(r["path"]).is_dir()
            r["git"] = (Path(r["path"]) / ".git").exists()
        return {"workspaces": rows, "projects_root": str(desk.projects_root())}

    @app.post("/api/desktop/workspaces")
    def new_workspace(body: WorkspaceIn):
        name = (body.name or "").strip()
        path = (body.path or "").strip()
        if not name and not path:
            raise HTTPException(400, "name or path is required")
        if not path:
            from apps.api.server import slug
            path = str(desk.projects_root() / slug(name, "project"))
        p = Path(path).expanduser()
        if not p.is_dir():
            if not body.create_folder:
                raise HTTPException(400, f"folder does not exist: {p} (tick 'create folder' to create it)")
            p.mkdir(parents=True, exist_ok=True)
        return desk.store.create_workspace(name or p.name, str(p.resolve()), body.env, body.shell, body.python,
                                           body.notes or "")

    @app.patch("/api/desktop/workspaces/{wid}")
    def update_workspace(wid: str, body: WorkspaceIn):
        if body.path and not Path(body.path).expanduser().is_dir():
            raise HTTPException(400, f"folder does not exist: {body.path}")
        w = desk.store.update_workspace(wid, **body.model_dump(exclude={"create_folder"}))
        if w is None:
            raise HTTPException(404, "no workspace")
        return w

    @app.delete("/api/desktop/workspaces/{wid}")
    def delete_workspace(wid: str):
        desk.store.delete_workspace(wid)
        return {"forgotten": wid, "files_deleted": False}

    def _ws(wid: str) -> Workspace:
        row = desk.store.workspace(wid)
        if row is None:
            raise HTTPException(404, "no workspace")
        try:
            return Workspace(row["path"], env=row.get("env"), shell=row.get("shell"), python=row.get("python"))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))

    @app.get("/api/desktop/workspaces/{wid}/tree")
    def tree(wid: str, path: str = "."):
        ws = _ws(wid)
        try:
            base = ws.resolve(path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        from .tools import SKIP_DIRS
        items = []
        for c in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if c.name in SKIP_DIRS:
                continue
            items.append({"name": c.name, "path": ws.rel(c), "dir": c.is_dir(),
                          "size": c.stat().st_size if c.is_file() else None})
        return {"path": ws.rel(base), "items": items[:2000]}

    @app.get("/api/desktop/workspaces/{wid}/file")
    def file(wid: str, path: str):
        r = _ws(wid).call("read_file", {"path": path})
        if not r["ok"]:
            raise HTTPException(400, r["text"])
        return r

    @app.get("/api/desktop/workspaces/{wid}/git")
    def git(wid: str):
        ws = _ws(wid)
        if not (ws.root / ".git").exists():
            return {"git": False}
        flags = 0x08000000 if os.name == "nt" else 0

        def run(*a: str) -> str:
            try:
                return subprocess.run(["git", *a], cwd=ws.root, capture_output=True, text=True, timeout=20,
                                      encoding="utf-8", errors="replace", creationflags=flags).stdout
            except (OSError, subprocess.SubprocessError) as e:
                return f"error: {e}"
        return {"git": True, "branch": run("rev-parse", "--abbrev-ref", "HEAD").strip(),
                "status": run("status", "--short"), "diff": run("diff")[:200_000]}

    # ------------------------------------------------------------------ autonomous progress stream
    @app.get("/api/desktop/autonomous/stream")
    def autonomous_stream(interval: float = 1.0):
        def gen():
            last = None
            idle = 0
            while True:
                try:
                    st = svc.state()
                except Exception as e:  # noqa: BLE001
                    st = {"error": str(e)}
                st["gpu_gate"] = GPU_GATE.status()
                body = json.dumps(st, ensure_ascii=False, default=str)
                if body != last:
                    last, idle = body, 0
                    yield f"event: state\ndata: {body}\n\n"
                else:
                    idle += 1
                    if idle % 15 == 0:
                        yield ": keep-alive\n\n"
                time.sleep(max(0.3, interval))
        return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

    return desk

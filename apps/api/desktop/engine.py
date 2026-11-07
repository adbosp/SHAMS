"""Desktop client engine (ADR-122): streamed chat, coding agent, media jobs, approvals.

Everything runs on the existing runtime:

* models come from the Model Registry (roles + presets, ``shams.models``) — no model name in this file;
* chat replies stream from the provider (``providers.stream_chat``); the coding agent uses the same
  provider's tool calling with the workspace tools in ``tools.py``;
* image / 3D / video presets are Direct Mode tasks (``DirectService``): scheduler, GPU lease, workers,
  artifact registry and QC are the ones Autonomous Mode uses;
* every GPU user takes the process-wide ``GPU_GATE``; before a job switches models the previous one is
  released through the backend's API (Ollama ``keep_alive: 0``, ComfyUI ``/free``) — no process is killed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import threading
import time
from pathlib import Path
from typing import Any

from apps.api.chat_prompt import assistant_system_prompt
from shams import providers
from shams import settings as settings_mod
from shams.mcp import MCPManager
from shams.research import RESEARCH_PROMPT, Research, ToolBox, requested_urls

MCP_CHAT_PROMPT = ("Tools named mcp__<server>__<tool> come from MCP servers the user connected. Use them only "
                   "when they clearly help the request. A result starting with 'permission denied' means the "
                   "user has not allowed that tool: say so and tell them to allow it in Settings → Permissions "
                   "instead of retrying.")
from shams.scheduling.gate import GPU_GATE, should_serialize

from .bus import EventBus, JobChannel
from .store import DesktopStore
from .tools import MODES, TOOL_SPECS, PermissionPolicy, Workspace

URL_RX = re.compile(r"https?://[^\s<>()\[\]\"'`]+[^\s<>()\[\]\"'`.,;:!?]")
TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml", ".yaml", ".yml", ".csv", ".html",
            ".css", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".cs", ".sh", ".ps1", ".bat", ".ini", ".cfg", ".xml",
            ".sql", ".log", ".gd", ".lua", ".kt", ".swift", ".rb", ".php"}
MAX_ATTACH_TEXT = 60_000
APPROVAL_TIMEOUT_S = 1800
MEDIA_KINDS = {"image", "scene3d", "video", "image_to_3d", "audio"}

AGENT_SYSTEM = """You are a coding agent working inside the user's local project folder through tools.
Workspace root: {root}
Shell: {shell}. Operating system: {os}.
Rules:
- Use list_dir / read_file / search_files to understand the code before changing it.
- Change files only with write_file or edit_file. Keep changes minimal and focused on the request.
- After changing code, run the project's tests or a quick check with run_command when one exists, and report
  the real result (exit code, failures). Never claim something works without having run it.
- Some actions need the user's approval; if one is denied, do not retry it — explain or choose another way.
- Answer in the user's language. End with a short summary of what changed and how it was verified.
{extra}"""


def extract_links(text: str) -> list[dict]:
    seen, out = set(), []
    for m in URL_RX.finditer(text or ""):
        u = m.group(0)
        if u not in seen:
            seen.add(u)
            kind = "image" if re.search(r"\.(png|jpe?g|gif|webp|svg)(\?|$)", u, re.I) else \
                   "video" if re.search(r"\.(mp4|webm|mov|m4v)(\?|$)", u, re.I) else "link"
            out.append({"url": u, "kind": kind})
    return out[:30]


def media_block(media_type: str, url: str, name: str = "") -> dict:
    if media_type.startswith("image/"):
        return {"type": "image", "url": url, "name": name, "mime": media_type}
    if media_type.startswith("video/"):
        return {"type": "video", "url": url, "name": name, "mime": media_type}
    if media_type in ("model/gltf-binary", "model/gltf+json"):
        return {"type": "model3d", "url": url, "name": name, "mime": media_type}
    if media_type.startswith("audio/"):
        return {"type": "audio", "url": url, "name": name, "mime": media_type}
    return {"type": "file", "url": url, "name": name, "mime": media_type}


class DesktopService:
    def __init__(self, workspace: Path, direct: Any, studio: Any = None):
        self.workspace = Path(workspace)
        self.dir = self.workspace / "_desktop"
        self.uploads = self.dir / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.store = DesktopStore(self.dir / "desktop.db")
        self.bus = EventBus()
        self.direct = direct
        self.studio = studio
        self.approvals: dict[str, dict] = {}        # approval id -> {"event", "decision", "note"}
        self.threads: dict[str, threading.Thread] = {}
        self.started = time.time()
        self.configure_gate()

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        return settings_mod.load(None)

    def desktop_cfg(self) -> dict:
        return {"permission_mode": "ask", "agent_max_rounds": 30, "command_timeout": 120, "allow": [], "deny": [],
                "projects_root": None, **(self.settings().get("desktop") or {})}

    def configure_gate(self) -> None:
        from shams import hardware
        try:
            n = len(hardware.detect()["gpus"])
        except Exception:  # noqa: BLE001 - no GPU info means "one GPU at most": serialize
            n = 0
        GPU_GATE.configure(should_serialize((self.settings().get("resources") or {}).get("serialize_gpu", "auto"), n))

    def projects_root(self) -> Path:
        root = self.desktop_cfg().get("projects_root")
        return Path(root) if root else self.workspace.parent / "SHAMS_Projects"

    # ------------------------------------------------------------------ presets
    def presets(self) -> list[dict]:
        from shams.models import ModelRegistry
        out = []
        for p in ModelRegistry(self.settings()).presets():
            p = dict(p)
            w = p.pop("worker", None)
            p["worker_config"] = (w or {}).get("config")
            p["worker_provider"] = (w or {}).get("provider")
            p["supports_tools"] = None
            out.append(p)
        return out

    def preset(self, pid: str) -> dict:
        from shams.models import ModelRegistry
        p = ModelRegistry(self.settings()).preset(pid)
        if p is None:
            raise KeyError(f"unknown preset {pid!r}")
        return p

    def _provider_for(self, preset: dict) -> tuple[Any, str]:
        w = preset.get("worker") or {}
        cfg = {k: v for k, v in (w.get("config") or {}).items()
               if k in ("base_url", "keep_alive", "num_ctx", "think", "api_key_env", "headers", "timeout")}
        ptype = w.get("provider") or preset.get("provider")
        if not ptype:
            raise RuntimeError(f"preset {preset['id']} has no provider")
        if ptype == "ollama" and "think" not in cfg:
            cfg["think"] = None                   # let the model decide; the client shows thinking separately
        return providers.create(ptype, cfg), w.get("model") or preset.get("model")

    def _model_tools_support(self, preset: dict) -> bool | None:
        if (preset.get("worker") or {}).get("provider") != "ollama":
            return True
        from shams.models import discover_ollama
        o = (self.settings().get("models") or {}).get("ollama") or {}
        d = discover_ollama(o.get("base_url"))
        info = next((m for m in d.get("models", []) if m["name"] == preset.get("model")), None)
        caps = (info or {}).get("capabilities") or []
        return ("tools" in caps) if caps else None

    # ------------------------------------------------------------------ attachments
    def save_upload(self, name: str, data: bytes, mime: str | None = None) -> dict:
        sha = hashlib.sha256(data).hexdigest()
        ext = Path(name).suffix.lower()[:10]
        p = self.uploads / f"{sha}{ext}"
        if not p.exists():
            p.write_bytes(data)
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return {"id": f"{sha}{ext}", "name": name, "mime": mime, "size": len(data), "url": f"/api/desktop/uploads/{sha}{ext}"}

    def upload_path(self, uid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}(\.[a-z0-9]{1,9})?", uid):
            raise ValueError("bad upload id")
        return self.uploads / uid

    # ------------------------------------------------------------------ conversation -> provider messages
    def _history(self, cid: str, upto: str | None, vision: bool) -> tuple[list[dict], list[str]]:
        msgs, images = [], []
        for m in self.store.messages(cid):
            if m["id"] == upto:
                break
            if m["role"] not in ("user", "assistant"):
                continue
            parts, imgs = [], []
            for b in m["blocks"] or []:
                if b.get("type") == "text" and b.get("text"):
                    parts.append(b["text"])
                elif b.get("type") == "attachment":
                    p = self.upload_path(b["id"])
                    if not p.exists():
                        continue
                    if (b.get("mime") or "").startswith("image/") and m["role"] == "user":
                        if vision:
                            imgs.append(base64.b64encode(p.read_bytes()).decode())
                        else:
                            parts.append(f"[image attached: {b.get('name')} — this model cannot see images]")
                    elif Path(b.get("name", "")).suffix.lower() in TEXT_EXT or (b.get("mime") or "").startswith("text/"):
                        txt = p.read_text(encoding="utf-8", errors="replace")[:MAX_ATTACH_TEXT]
                        parts.append(f"<file name=\"{b.get('name')}\">\n{txt}\n</file>")
                    else:
                        parts.append(f"[file attached: {b.get('name')} ({b.get('mime')}, {b.get('size')} bytes)]")
                elif b.get("type") == "tool" and m["role"] == "assistant":
                    parts.append(f"[tool {b.get('name')}: {b.get('status')}]")
            if parts or imgs:
                msgs.append({"role": m["role"], "content": "\n\n".join(parts)})
                images = imgs if m["role"] == "user" else images
        return msgs, images

    # ------------------------------------------------------------------ public: send a message
    def send(self, cid: str, text: str, *, attachments: list[dict] | None = None, preset: str | None = None,
             mode: str | None = None, params: dict | None = None, confirm_adult: bool = False) -> dict:
        conv = self.store.conversation(cid)
        if conv is None:
            raise KeyError(f"no conversation {cid}")
        preset = preset or conv.get("preset")
        mode = mode or conv.get("mode") or "chat"
        if not preset:
            raise ValueError("choose a model or preset first")
        p = self.preset(preset)
        if p["status"] != "ok":
            raise RuntimeError(f"preset {preset} is not available: {p['status']} {p.get('how', '')}".strip())
        updates: dict[str, Any] = {"preset": preset, "mode": mode}
        if conv["title"] in ("Cuộc trò chuyện mới", "New chat") and text.strip():
            updates["title"] = text.strip().splitlines()[0][:60]
        self.store.update_conversation(cid, **updates)
        blocks = ([{"type": "text", "text": text}] if text else []) + \
                 [{"type": "attachment", **{k: a.get(k) for k in ("id", "name", "mime", "size", "url")}} for a in attachments or []]
        user = self.store.add_message(cid, "user", blocks)
        kind = "media" if p["kind"] in MEDIA_KINDS else ("agent" if mode == "agent" else "chat")
        job = self.store.create_job(kind, conversation_id=cid, preset=preset, model=p.get("model"),
                                    params={"mode": mode, **(params or {})})
        asst = self.store.add_message(cid, "assistant", [], status="streaming", model=p.get("model"), preset=preset,
                                      job_id=job["id"])
        self.store.x("UPDATE jobs SET message_id=? WHERE id=?", (asst["id"], job["id"]))
        ch = self.bus.open(job["id"])
        ch.publish("message", message_id=asst["id"], user_message_id=user["id"], conversation_id=cid, kind=kind)
        target = {"chat": self._run_chat, "agent": self._run_agent, "media": self._run_media}[kind]
        t = threading.Thread(target=self._guard, args=(target, ch, job["id"], cid, asst["id"], p, text, params or {},
                                                        confirm_adult, user["id"]), daemon=True)
        self.threads[job["id"]] = t
        t.start()
        return {"job_id": job["id"], "message_id": asst["id"], "user_message_id": user["id"], "kind": kind}

    def _guard(self, fn, ch: JobChannel, jid: str, cid: str, mid: str, preset: dict, text: str, params: dict,
               confirm_adult: bool, user_mid: str) -> None:
        self.store.update_job(jid, status="running", started=time.time())
        state = {"blocks": [], "usage": {}}
        try:
            fn(ch, jid, cid, mid, preset, text, params, confirm_adult, user_mid, state)
            status = "canceled" if ch.cancel.is_set() else "done"
        except InterruptedError:
            status = "canceled"
        except Exception as e:  # noqa: BLE001 - every failure is shown to the user with its message
            status = "error"
            state["blocks"].append({"type": "error", "text": _friendly(e)})
            ch.publish("error", text=_friendly(e))
        if status == "canceled":
            state["blocks"].append({"type": "notice", "text": "Đã hủy."})
        text_all = "\n".join(b.get("text", "") for b in state["blocks"] if b.get("type") == "text")
        sources = state.get("sources", {})
        if sources:
            state["blocks"].extend([
                {"type": "notice", "text": "Trang SHAMS đã đọc"},
                {"type": "links", "items": [{"url": url, "kind": "link"} for url in sources]},
            ])
        links = [link for link in extract_links(text_all) if link["url"] not in sources]
        if links:
            state["blocks"].append({"type": "links", "items": links})
        self.store.update_message(mid, blocks=state["blocks"], status=status, usage=state["usage"])
        self.store.update_job(jid, status=status, finished=time.time(), result={"usage": state["usage"]},
                              error=next((b["text"] for b in state["blocks"] if b.get("type") == "error"), None))
        ch.publish("done", status=status, message=self.store.message(mid))
        ch.close()

    # ------------------------------------------------------------------ GPU preparation
    def _prepare_ollama(self, ch: JobChannel, prov: Any, model: str) -> None:
        """Single GPU: release other Ollama models (and ComfyUI's cache) before this one loads."""
        if not GPU_GATE.serialize or not hasattr(prov, "ps"):
            return
        try:
            loaded = prov.ps()
        except providers.ProviderError:
            return
        for m in loaded:
            if m["name"] != model:
                ch.publish("status", phase="unload", text=f"Dỡ model {m['name']} để giải phóng VRAM")
                try:
                    prov.unload(m["name"])
                except providers.ProviderError as e:
                    ch.publish("status", phase="warn", text=f"Không dỡ được {m['name']}: {e}")
        self._free_comfyui(ch)
        if not any(m["name"] == model for m in loaded):
            ch.publish("status", phase="load", text=f"Nạp model {model}")

    def _free_comfyui(self, ch: JobChannel | None) -> None:
        c = (self.settings().get("tools") or {}).get("comfyui")
        if not c:
            return
        last = next((h for h in reversed(GPU_GATE.history) if h.get("kind") in ("image", "video", "scene3d")), None)
        if last is None:
            return
        try:
            from shams_tools.comfyui import ComfyUI
            cu = ComfyUI(**{k: v for k, v in c.items() if k in ("base_url",)})
            if cu.health() == "ok":
                cu.unload()
                if ch:
                    ch.publish("status", phase="unload", text="Giải phóng VRAM của ComfyUI (/free)")
        except Exception:  # noqa: BLE001 - freeing is best effort
            pass

    def _release_ollama_all(self, ch: JobChannel) -> None:
        if not GPU_GATE.serialize:
            return
        o = (self.settings().get("models") or {}).get("ollama") or {}
        try:
            prov = providers.create("ollama", {"base_url": o.get("base_url")} if o.get("base_url") else {})
            for m in prov.ps():
                ch.publish("status", phase="unload", text=f"Dỡ model {m['name']} trước khi tạo ảnh/video")
                prov.unload(m["name"])
        except providers.ProviderError:
            pass

    # ------------------------------------------------------------------ chat
    def _run_chat(self, ch, jid, cid, mid, preset, text, params, confirm_adult, user_mid, state) -> None:
        prov, model = self._provider_for(preset)
        caps = set(preset.get("capabilities") or [])
        vision = "vision" in caps or (preset.get("worker") or {}).get("config", {}).get("vision") or \
            self._is_vision(preset)
        history, images = self._history(cid, mid, bool(vision))
        sys_prompt = assistant_system_prompt()
        preset_params = preset.get("params") or {}
        extra = params.get("system") or preset_params.get("system")
        if extra:
            sys_prompt += "\n\nAdditional preset instructions, subordinate to the chat rules above:\n" + str(extra)
        msgs = [{"role": "system", "content": sys_prompt}] + history
        settings = self.settings()
        research = Research(settings.get("research"))
        tools_ok = getattr(prov, "name", "") == "ollama" and self._model_tools_support(preset) is True
        catalog = research.chat_tools() if (tools_ok and
                                           params.get("research", preset_params.get("research")) is not False) else []
        has_research = bool(catalog)
        # MCP servers for chat (ADR-123): params.mcp / preset params.mcp / desktop.chat_mcp — a list or "all"
        mcp_sel = params.get("mcp", preset_params.get("mcp", (settings.get("desktop") or {}).get("chat_mcp")))
        mcp = MCPManager.from_settings(settings) if (tools_ok and mcp_sel and settings.get("mcp_servers")) else None
        if mcp is not None:
            catalog = catalog + mcp.tools(mcp_sel)
            for name, err in mcp.errors.items():
                ch.publish("status", text=f"MCP {name} không kết nối được: {err[:160]}")
        opts = self._gen_opts(prov, params)
        if images:
            opts["images"] = images
        with GPU_GATE.hold(f"chat:{jid}", model=model, kind="llm", cancel=ch.cancel):
            self._prepare_ollama(ch, prov, model)
            if not catalog:
                self._stream_into(ch, state, prov, msgs, model, None, opts)
                return
            if has_research:
                msgs[0]["content"] += "\n\n" + RESEARCH_PROMPT
            if mcp is not None:
                msgs[0]["content"] += "\n\n" + MCP_CHAT_PROMPT
            try:
                self._stream_research_chat(ch, state, prov, msgs, model, opts, text, research, catalog, mcp)
            finally:
                if mcp is not None:
                    mcp.close()

    def _stream_research_chat(self, ch, state, prov, msgs, model, opts, text, research, catalog, mcp=None) -> None:
        toolbox = ToolBox(mcp, research)

        def fetch_results(calls, tools):
            results = []
            for call in calls:
                if ch.cancel.is_set():
                    raise InterruptedError
                res = toolbox.call(call["name"], call["arguments"], tools)
                results.append((call, ("ERROR: " if res["is_error"] else "") + res["text"]))
            return prov.stream_tool_results(results)

        with research.session() as run:
            for i, url in enumerate(requested_urls(text)):
                name = "research__web_scrape"
                args = {"url": url}
                ch.publish("status", text="Đang đọc trang nguồn…")
                msgs.append({"role": "assistant", "content": "",
                             "tool_calls": [{"function": {"name": name, "arguments": args}}]})
                msgs += fetch_results([{"name": name, "id": f"requested_source_{i}", "arguments": args}], catalog)
            for round_ in range(research.max_calls + 1):
                tools = toolbox.available(catalog) if round_ < research.max_calls else []
                ev = self._stream_into(ch, state, prov, msgs, model, tools, opts)
                if not ev.get("calls") or not tools:
                    break
                ch.publish("status", text="Đang gọi công cụ…" if any(
                    c["name"].startswith("mcp__") for c in ev["calls"]) else "Đang tra cứu nguồn…")
                msgs.append(ev["assistant"])
                msgs += fetch_results(ev["calls"], tools)
            state["sources"] = run.sources

    def _is_vision(self, preset: dict) -> bool:
        if (preset.get("worker") or {}).get("provider") != "ollama":
            return False
        from shams.models import discover_ollama
        o = (self.settings().get("models") or {}).get("ollama") or {}
        info = next((m for m in discover_ollama(o.get("base_url")).get("models", []) if m["name"] == preset.get("model")), None)
        return "vision" in ((info or {}).get("capabilities") or [])

    @staticmethod
    def _gen_opts(prov: Any, params: dict) -> dict:
        if getattr(prov, "name", "") == "ollama":
            o = {k2: params[k] for k, k2 in (("temperature", "temperature"), ("max_tokens", "num_predict"),
                                              ("num_ctx", "num_ctx")) if params.get(k) is not None}
            kw: dict[str, Any] = {"options": o}
            if params.get("think") is not None:
                kw["think"] = bool(params["think"])
            return kw
        return {k: params[k] for k in ("temperature", "max_tokens") if params.get(k) is not None}

    def _stream_into(self, ch, state, prov, msgs, model, tools, opts) -> dict:
        """One streamed model turn appended to state['blocks']. Returns the tool_calls event (or {})."""
        blocks = state["blocks"]
        text_block: dict | None = None
        think_block: dict | None = None
        calls: dict = {}
        t0 = time.monotonic()
        first = None
        for ev in providers.stream_chat(prov, msgs, model=model, tools=tools, cancel=ch.cancel, **opts):
            if ch.cancel.is_set():
                raise InterruptedError
            if ev["type"] in ("text", "thinking") and first is None:
                first = time.monotonic() - t0
            if ev["type"] == "thinking":
                if think_block is None:
                    think_block = {"type": "thinking", "text": ""}
                    blocks.append(think_block)
                think_block["text"] += ev["text"]
                ch.publish("thinking", text=ev["text"], index=blocks.index(think_block))
            elif ev["type"] == "text":
                if text_block is None:
                    text_block = {"type": "text", "text": ""}
                    blocks.append(text_block)
                text_block["text"] += ev["text"]
                ch.publish("delta", text=ev["text"], index=blocks.index(text_block))
            elif ev["type"] == "tool_calls":
                calls = ev
            elif ev["type"] == "done":
                u = state["usage"]
                u["tokens_in"] = u.get("tokens_in", 0) + ev.get("tokens_in", 0)
                u["tokens_out"] = u.get("tokens_out", 0) + ev.get("tokens_out", 0)
                u["seconds"] = round(u.get("seconds", 0) + time.monotonic() - t0, 2)
                if ev.get("load_seconds"):
                    u["load_seconds"] = ev["load_seconds"]
                if first is not None and "first_token_s" not in u:
                    u["first_token_s"] = round(first, 2)
                ch.publish("usage", usage=u)
        if ch.cancel.is_set():
            raise InterruptedError
        if text_block is not None and not text_block["text"].strip():
            blocks.remove(text_block)           # whitespace between tool calls: keeps tool groups together
        return calls

    # ------------------------------------------------------------------ coding agent
    def _run_agent(self, ch, jid, cid, mid, preset, text, params, confirm_adult, user_mid, state) -> None:
        conv = self.store.conversation(cid)
        ws_row = self.store.workspace(conv["workspace_id"]) if conv.get("workspace_id") else None
        if ws_row is None:
            raise RuntimeError("Chế độ Agent cần một Project (thư mục local). Chọn hoặc tạo Project cho cuộc trò chuyện này.")
        if self._model_tools_support(preset) is False:
            raise RuntimeError(f"Model {preset.get('model')} không hỗ trợ tool calling (Ollama không báo capability "
                               f"'tools'), nên không chạy được như agent coding. Chọn model khác cho chế độ Agent.")
        dcfg = self.desktop_cfg()
        ws = Workspace(ws_row["path"], env=ws_row.get("env") or {}, shell=ws_row.get("shell"),
                       python=ws_row.get("python"), checkpoint_root=self.dir / "checkpoints")
        mode = conv.get("permission_mode") or dcfg["permission_mode"]
        policy = PermissionPolicy(mode if mode in MODES else "ask", dcfg.get("allow"), dcfg.get("deny"))
        prov, model = self._provider_for(preset)
        import platform
        extra = ws_row.get("notes") or ""
        sys_prompt = AGENT_SYSTEM.format(root=ws.root, shell=ws.shell or ("cmd" if platform.system() == "Windows" else "sh"),
                                         os=platform.system(), extra=("Project notes:\n" + extra) if extra else "")
        history, _ = self._history(cid, mid, False)
        msgs = [{"role": "system", "content": sys_prompt}] + history
        opts = self._gen_opts(prov, params)
        native = hasattr(prov, "chat_stream")
        fmt = getattr(prov, "stream_tool_results", None) if native else getattr(prov, "tool_results", None)
        rounds = int(dcfg.get("agent_max_rounds", 30))
        with GPU_GATE.hold(f"agent:{jid}", model=model, kind="llm", cancel=ch.cancel):
            self._prepare_ollama(ch, prov, model)
            for r in range(rounds + 1):
                ch.publish("status", text=f"Agent đang phân tích · vòng {r + 1}")
                tools = TOOL_SPECS if r < rounds else None
                ev = self._stream_into(ch, state, prov, msgs, model, tools, opts)
                if not ev or not ev.get("calls"):
                    return
                msgs.append(ev["assistant"])
                results = []
                for call in ev["calls"]:
                    out = self._agent_call(ch, jid, cid, ws, policy, call, state, dcfg)
                    results.append((call, out))
                msgs += fmt(results)
            state["blocks"].append({"type": "notice", "text": f"Dừng sau {rounds} vòng tool (desktop.agent_max_rounds)."})

    def _agent_call(self, ch, jid, cid, ws: Workspace, policy: PermissionPolicy, call: dict, state: dict, dcfg: dict) -> str:
        name, args = call.get("name") or "", call.get("arguments") or {}
        if not isinstance(args, dict):
            args = {"_raw": args}
        if name == "run_command" and "timeout" not in args:
            args["timeout"] = int(dcfg.get("command_timeout", 120))
        block = {"type": "tool", "id": call.get("id"), "name": name, "arguments": args, "status": "pending"}
        state["blocks"].append(block)
        idx = len(state["blocks"]) - 1
        decision, reason = policy.decide(name, args)
        block["reason"] = reason
        if decision == "deny":
            block["status"] = "denied"
            ch.publish("tool", index=idx, block=dict(block))
            self.store.log_tool(jid, cid, name, args, "deny", reason)
            return f"DENIED by permission policy: {reason}"
        if decision == "ask":
            preview = ws.preview(name, args)
            appr = self.store.create_approval(jid, cid, name, args, reason, preview)
            block.update(status="approval", approval_id=appr["id"], preview=preview)
            ch.publish("approval", index=idx, block=dict(block), approval=appr)
            ok, note = self._wait_approval(appr["id"], ch)
            if not ok:
                block["status"] = "denied"
                block["note"] = note
                ch.publish("tool", index=idx, block=dict(block))
                self.store.log_tool(jid, cid, name, args, "denied_by_user", note)
                return "DENIED by the user" + (f": {note}" if note else "") + ". Do not retry this action."
        block.update(status="running", started_at=time.time())
        ch.publish("tool", index=idx, block=dict(block))
        res = ws.call(name, args, job_id=jid, cancel=ch.cancel)
        block["status"] = "done" if res.get("ok") else "canceled" if res.get("status") == "canceled" else "error"
        block["finished_at"] = time.time()
        block["result"] = res.get("text", "")[:6000]
        for k in ("diff", "path", "exit_code", "checkpoint", "seconds"):
            if res.get(k) is not None:
                block[k] = res[k]
        if "file_change" in res:
            block.update(file_change=res["file_change"], added=res["added"], removed=res["removed"])
        ch.publish("tool", index=idx, block=dict(block))
        self.store.log_tool(jid, cid, name, args, "allow" if decision == "allow" else "approved", res.get("text"),
                            res.get("checkpoint"))
        return ("" if res.get("ok") else "ERROR: ") + res.get("text", "")[:20000]

    def _wait_approval(self, aid: str, ch: JobChannel) -> tuple[bool, str]:
        slot = {"event": threading.Event(), "decision": None, "note": ""}
        self.approvals[aid] = slot
        deadline = time.monotonic() + APPROVAL_TIMEOUT_S
        try:
            while not slot["event"].wait(0.25):
                if ch.cancel.is_set():
                    self.store.decide_approval(aid, "canceled")
                    raise InterruptedError
                if time.monotonic() > deadline:
                    self.store.decide_approval(aid, "expired")
                    return False, "no answer within 30 minutes"
            return slot["decision"] == "approve", slot["note"]
        finally:
            self.approvals.pop(aid, None)

    def decide(self, aid: str, approve: bool, note: str = "") -> dict:
        row = self.store.approval(aid)
        if row is None:
            raise KeyError(f"no approval {aid}")
        if row["status"] != "pending":
            return row
        self.store.decide_approval(aid, "approved" if approve else "denied", note)
        slot = self.approvals.get(aid)
        if slot:
            slot["decision"], slot["note"] = ("approve" if approve else "deny"), note
            slot["event"].set()
        return self.store.approval(aid)

    # ------------------------------------------------------------------ media (Direct Mode task)
    def _run_media(self, ch, jid, cid, mid, preset, text, params, confirm_adult, user_mid, state) -> None:
        from apps.api.server import DirectIn
        from fastapi import HTTPException
        body = {"prompt": text, **{k: v for k, v in params.items() if k not in ("mode",)}}
        props = (preset.get("input_schema") or {}).get("properties") or {}
        body = {k: v for k, v in body.items() if k in props and v not in (None, "")}
        kind = preset.get("kind", "image")
        with GPU_GATE.hold(f"media:{jid}", model=preset.get("model"), kind=kind, cancel=ch.cancel):
            self._release_ollama_all(ch)
            ch.publish("status", phase="queued", text=f"Gửi tác vụ {kind} tới {preset['id']}")
            try:
                tid = self.direct.queue(DirectIn(preset=preset["id"], params=body, confirm_adult=confirm_adult))
            except HTTPException as e:
                raise RuntimeError(str(e.detail)) from None
            state["blocks"].append({"type": "task", "task_id": tid, "preset": preset["id"], "state": "READY"})
            ch.publish("task", task_id=tid, state="READY")
            stop = threading.Event()
            threading.Thread(target=self._watch_task, args=(tid, ch, stop), daemon=True).start()
            try:
                self.direct.execute(tid)            # this thread already holds the GPU gate (re-entrant)
            finally:
                stop.set()
            v = self.direct.task(tid)
        state["blocks"][-1]["state"] = v["state"]
        for o in v.get("outputs", []):
            b = media_block(o["media_type"], o["url"], o["logical_id"])
            state["blocks"].append(b)
            ch.publish("media", block=b)
        if v.get("error"):
            state["blocks"].append({"type": "error", "text": v["error"]})
        elif v["state"] != "ACCEPTED":
            state["blocks"].append({"type": "notice", "text": f"Tác vụ kết thúc ở trạng thái {v['state']}."})
        if v.get("summary") and not v.get("outputs"):
            state["blocks"].append({"type": "text", "text": v["summary"]})

    def _watch_task(self, tid: str, ch: JobChannel, stop: threading.Event) -> None:
        st, last = self.direct.studio, None
        while not stop.wait(0.5):
            try:
                s = st.db.task(tid)["state"]
            except Exception:  # noqa: BLE001 - the task row may not be visible yet
                continue
            if s != last:
                last = s
                ch.publish("status", phase="task", text=f"{tid}: {s}", state=s)

    # ------------------------------------------------------------------ cancel
    def cancel(self, jid: str) -> dict:
        ch = self.bus.get(jid)
        job = self.store.job(jid)
        if job is None:
            raise KeyError(f"no job {jid}")
        if ch is not None:
            ch.cancel.set()
            ch.publish("status", phase="cancel", text="Đang hủy...")
        if job["kind"] == "media":
            self._interrupt_comfyui()
        return self.store.job(jid)

    def _interrupt_comfyui(self) -> None:
        c = (self.settings().get("tools") or {}).get("comfyui")
        if not c:
            return
        try:
            from shams.providers import _post_json
            _post_json(f"{c.get('base_url', 'http://127.0.0.1:8188').rstrip('/')}/interrupt", {}, {}, 10)
        except Exception:  # noqa: BLE001 - ComfyUI may already be idle
            pass


def _friendly(e: BaseException) -> str:
    s = str(e) or e.__class__.__name__
    if "cannot reach" in s and "11434" in s:
        return f"Không kết nối được Ollama ({s}). Kiểm tra Ollama đang chạy."
    if "out of memory" in s.lower():
        return f"Hết VRAM: {s}. Thử model nhỏ hơn, giảm num_ctx, hoặc dỡ model khác trong Model Manager."
    return s

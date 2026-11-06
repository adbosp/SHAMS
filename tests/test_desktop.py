"""Desktop client API (ADR-122): streamed chat, coding agent with permissions, Model Manager, GPU gate,
workspaces, token, persistence. Runs against fakes only (no model, no GPU)."""
import json
import threading
import time

import pytest

from shams import models
from shams.scheduling.gate import GpuGate, should_serialize

import tests.test_local_models as tlm

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from apps.api.desktop.tools import PermissionPolicy, Workspace, classify_command  # noqa: E402
from apps.api.server import create_app  # noqa: E402

CODER = "qwen2.5-coder:14b"


class StreamingOllama(tlm.FakeOllama):
    """FakeOllama that answers streamed /api/chat with NDJSON and can play a script of tool calls."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests = []
        super().__init__()

    def handle(self, method, path, body):
        if path == "/api/chat" and body.get("stream"):
            self.requests.append(body)
            if body["model"] not in self.loaded:
                self.loaded.append(body["model"])
            step = self.script.pop(0) if self.script else {"text": "Xin chào! Xem https://example.com/docs nhé."}
            lines = []
            if step.get("thinking"):
                lines.append({"message": {"role": "assistant", "thinking": step["thinking"]}, "done": False})
            for word in (step.get("text") or "").split(" "):
                lines.append({"message": {"role": "assistant", "content": word + " "}, "done": False})
            if step.get("tool_calls"):
                lines.append({"message": {"role": "assistant", "content": "",
                                          "tool_calls": [{"function": c} for c in step["tool_calls"]]}, "done": False})
            lines.append({"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": 7,
                          "eval_count": 5, "load_duration": 1_000_000})
            return 200, ("\n".join(json.dumps(x) for x in lines) + "\n").encode(), "application/x-ndjson"
        return super().handle(method, path, body)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(tlm, "CAPS", {**tlm.CAPS, CODER: ["completion", "tools"]})
    o = StreamingOllama()
    # Server._send takes (code, obj[, ctype])
    models._CACHE.clear()
    yield o
    o.close()
    models._CACHE.clear()


def _settings(url, **extra):
    s = {"models": {"ollama": {"base_url": url}, "roles": {"coder": {"model": CODER}}},
         "resources": {"serialize_gpu": True}}
    s.update(extra)
    return s


@pytest.fixture
def client(tmp_path, isolated_home, fake):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "settings.json").write_text(json.dumps(_settings(fake.url)), encoding="utf-8")
    app = create_app(tmp_path / "ws")
    c = TestClient(app)
    c.app_ref = app
    yield c
    app.state.desktop.store.close()


def read_stream(c, job_id, timeout=20):
    evs = []
    with c.stream("GET", f"/api/desktop/jobs/{job_id}/stream") as r:
        assert r.status_code == 200
        ev = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: ") and ev:
                evs.append((ev, json.loads(line[6:])))
                if ev == "end":
                    break
    return evs


def wait_approval(c, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        a = c.get("/api/desktop/approvals").json()["approvals"]
        if a:
            return a[0]
        time.sleep(0.05)
    raise AssertionError("no approval request")


# --------------------------------------------------------------------------- chat
def test_streamed_chat_saves_history_and_groups_links(client, fake):
    c = client
    conv = c.post("/api/desktop/conversations", json={}).json()
    assert conv["permission_mode"] == "ask"
    r = c.post(f"/api/desktop/conversations/{conv['id']}/send", json={"text": "Chào bạn", "preset": "coder"})
    assert r.status_code == 200, r.text
    job = r.json()
    evs = read_stream(c, job["job_id"])
    types = [e for e, _ in evs]
    assert types[0] == "message" and "delta" in types and types[-2:] == ["done", "end"]
    text = "".join(d["text"] for e, d in evs if e == "delta")
    assert text.startswith("Xin chào!")
    done = next(d for e, d in evs if e == "done")
    assert done["status"] == "done"
    blocks = done["message"]["blocks"]
    assert blocks[0]["type"] == "text" and blocks[-1] == {"type": "links", "items": [
        {"url": "https://example.com/docs", "kind": "link"}]}
    assert done["message"]["usage"]["tokens_out"] == 5
    full = c.get(f"/api/desktop/conversations/{conv['id']}").json()
    assert full["conversation"]["title"] == "Chào bạn" and full["conversation"]["preset"] == "coder"
    assert [m["role"] for m in full["messages"]] == ["user", "assistant"]
    # the model really got the conversation, through the native streaming endpoint
    assert fake.requests[-1]["messages"][-1] == {"role": "user", "content": "Chào bạn"}
    assert "Runtime server date and time:" in fake.requests[-1]["messages"][0]["content"]
    assert "Always finish with a meaningful follow-up question" in fake.requests[-1]["messages"][0]["content"]
    assert "tools" not in fake.requests[-1]
    assert c.get("/api/desktop/jobs").json()["jobs"][0]["status"] == "done"


def test_desktop_chat_reads_sources_and_streams_without_workspace_tools(client, fake, isolated_home, monkeypatch):
    from shams.research import Research

    settings = _settings(fake.url, research={"enabled": True, "browser": {"enabled": True}})
    (isolated_home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    first = "https://example.org/a"
    second = "https://example.org/b"
    monkeypatch.setattr(Research, "_get", lambda self, url, **kwargs: (b"<main>Evidence</main>", "text/html", url, 200))
    fake.script = [
        {"tool_calls": [{"name": "research__web_scrape", "arguments": {"url": second}}]},
        {"text": f"Đây là nguồn {first}. Bạn muốn tìm hiểu gì tiếp?"},
    ]
    cid = client.post("/api/desktop/conversations", json={"preset": "coder"}).json()["id"]
    job = client.post(f"/api/desktop/conversations/{cid}/send", json={"text": f"Đọc {first}"}).json()
    events = read_stream(client, job["job_id"])
    assert any(event == "delta" for event, _ in events)
    assert len(fake.requests) == 2
    for request in fake.requests:
        assert {t["function"]["name"] for t in request["tools"]} == {
            "research__web_search", "research__web_scrape", "research__web_research"}
        assert "Evidence" in request["messages"][-1]["content"]
    done = next(data for event, data in events if event == "done")
    assert done["status"] == "done"
    assert done["message"]["blocks"][-2:] == [
        {"type": "notice", "text": "Trang SHAMS đã đọc"},
        {"type": "links", "items": [{"url": first, "kind": "link"}, {"url": second, "kind": "link"}]},
    ]


def test_text_attachment_is_inlined_and_unknown_preset_refused(client, fake):
    c = client
    up = c.post("/api/desktop/uploads?name=notes.txt", content=b"hello file", headers={"content-type": "text/plain"}).json()
    assert c.get(up["url"]).content == b"hello file"
    conv = c.post("/api/desktop/conversations", json={"preset": "coder"}).json()
    job = c.post(f"/api/desktop/conversations/{conv['id']}/send", json={"text": "tóm tắt", "attachments": [up]}).json()
    read_stream(c, job["job_id"])
    assert '<file name="notes.txt">\nhello file\n</file>' in fake.requests[-1]["messages"][-1]["content"]
    assert c.post(f"/api/desktop/conversations/{conv['id']}/send", json={"text": "x", "preset": "nope"}).status_code == 404


# --------------------------------------------------------------------------- coding agent
def _agent_conv(c, tmp_path, mode):
    folder = tmp_path / "myproj"
    folder.mkdir()
    (folder / "app.py").write_text("print('v1')\n", encoding="utf-8")
    ws = c.post("/api/desktop/workspaces", json={"name": "My project", "path": str(folder),
                                                 "env": {"APP_MODE": "test"}}).json()
    conv = c.post("/api/desktop/conversations", json={"mode": "agent", "preset": "coder", "workspace_id": ws["id"],
                                                      "permission_mode": mode}).json()
    return folder, ws, conv


def test_agent_edits_files_with_diff_and_reverts(client, fake, tmp_path):
    c = client
    folder, ws, conv = _agent_conv(c, tmp_path, "safe")
    fake.script = [
        {"text": "Đọc file trước.", "tool_calls": [{"name": "read_file", "arguments": {"path": "app.py"}}]},
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "app.py", "old_text": "v1", "new_text": "v2"}},
                        {"name": "run_command", "arguments": {"command": "python --version"}}]},
        {"text": "Đã đổi v1 thành v2 và chạy kiểm tra."},
    ]
    job = c.post(f"/api/desktop/conversations/{conv['id']}/send", json={"text": "đổi v1 thành v2"}).json()
    evs = read_stream(c, job["job_id"])
    done = next(d for e, d in evs if e == "done")
    assert done["status"] == "done", done
    assert done["message"]["job_kind"] == "agent"
    assert any(e == "status" and "vòng 1" in d["text"] for e, d in evs)
    command_events = [d["block"] for e, d in evs if e == "tool" and d["block"]["name"] == "run_command"]
    assert [b["status"] for b in command_events] == ["running", "done"]
    assert command_events[0]["started_at"] <= command_events[1]["finished_at"]
    assert "exit_code" not in command_events[0]
    tools = [b for b in done["message"]["blocks"] if b["type"] == "tool"]
    assert [t["name"] for t in tools] == ["read_file", "edit_file", "run_command"]
    assert all(t["status"] == "done" for t in tools), tools
    assert "-print('v1')" in tools[1]["diff"] and "+print('v2')" in tools[1]["diff"]
    assert (tools[1]["file_change"], tools[1]["added"], tools[1]["removed"]) == ("edited", 1, 1)
    assert tools[2]["exit_code"] == 0
    assert (folder / "app.py").read_text() == "print('v2')\n"
    # tool results went back to the model in native format
    assert any(m.get("role") == "tool" and m.get("tool_name") == "read_file" for m in fake.requests[1]["messages"])
    assert fake.requests[0]["tools"][0]["function"]["name"] == "list_dir"
    # checkpoint: revert restores the file
    assert c.post(f"/api/desktop/jobs/{job['job_id']}/revert").json()["restored"] == ["app.py"]
    assert (folder / "app.py").read_text() == "print('v1')\n"
    log = c.get("/api/desktop/tool-log", params={"conversation_id": conv["id"]}).json()["entries"]
    assert {e["tool"] for e in log} >= {"read_file", "edit_file", "run_command", "revert"}


def test_agent_asks_before_acting_and_respects_denial(client, fake, tmp_path):
    c = client
    folder, ws, conv = _agent_conv(c, tmp_path, "ask")
    fake.script = [
        {"tool_calls": [{"name": "write_file", "arguments": {"path": "new.txt", "content": "hi"}}]},
        {"tool_calls": [{"name": "run_command", "arguments": {"command": "git push origin main"}}]},
        {"text": "Xong."},
    ]
    job = c.post(f"/api/desktop/conversations/{conv['id']}/send", json={"text": "tạo file"}).json()
    a = wait_approval(c)
    assert a["tool"] == "write_file" and "+hi" in a["preview"]["diff"]
    assert not (folder / "new.txt").exists()
    assert c.post(f"/api/desktop/approvals/{a['id']}", json={"approve": True}).json()["status"] == "approved"
    b = wait_approval(c)
    assert b["tool"] == "run_command" and "destructive" in b["reason"]
    c.post(f"/api/desktop/approvals/{b['id']}", json={"approve": False, "note": "không push"})
    evs = read_stream(c, job["job_id"])
    pending = [d["block"] for e, d in evs if e == "approval"]
    assert len(pending) == 2 and all(b["status"] == "approval" for b in pending)
    done = next(d for e, d in evs if e == "done")
    tools = [x for x in done["message"]["blocks"] if x["type"] == "tool"]
    assert [t["status"] for t in tools] == ["done", "denied"]
    assert (folder / "new.txt").read_text() == "hi"
    assert "DENIED by the user: không push" in fake.requests[-1]["messages"][-1]["content"]


def test_agent_patch_details_counts_lines_without_a_trailing_newline():
    from apps.api.desktop.tools import patch_details

    edit = patch_details("old", "+new", "notes.txt")
    assert edit["file_change"] == "edited"
    assert (edit["added"], edit["removed"]) == (1, 1)
    assert "-old\n++new\n" in edit["diff"]
    assert patch_details("", "", "empty.txt", created=True)["file_change"] == "created"
    assert patch_details("same", "same", "notes.txt")["file_change"] is None


def test_agent_needs_a_workspace_and_a_tool_model(client, fake, monkeypatch):
    c = client
    conv = c.post("/api/desktop/conversations", json={"mode": "agent", "preset": "coder"}).json()
    job = c.post(f"/api/desktop/conversations/{conv['id']}/send", json={"text": "x"}).json()
    done = next(d for e, d in read_stream(c, job["job_id"]) if e == "done")
    assert done["status"] == "error" and "Project" in done["message"]["blocks"][0]["text"]


def test_permission_policy_modes():
    assert classify_command("python -m pytest -q") == "safe"
    assert classify_command("git status && rm -rf build") == "destructive"
    assert classify_command("pip install requests") == "sensitive"
    assert classify_command("node build.js") == "normal"
    ask, safe, auto = PermissionPolicy("ask"), PermissionPolicy("safe"), PermissionPolicy("auto")
    assert ask.decide("read_file", {})[0] == "allow"
    assert ask.decide("write_file", {})[0] == "ask" and safe.decide("write_file", {})[0] == "allow"
    assert safe.decide("run_command", {"command": "pytest"})[0] == "allow"
    assert safe.decide("run_command", {"command": "node x.js"})[0] == "ask"
    assert auto.decide("run_command", {"command": "node x.js"})[0] == "allow"
    for p in (ask, safe, auto):
        assert p.decide("run_command", {"command": "git reset --hard"})[0] == "ask"
        assert p.decide("delete_file", {"path": "a"})[0] == "ask"
    assert PermissionPolicy("auto", deny=["npm *"]).decide("run_command", {"command": "npm run build"})[0] == "deny"


def test_workspace_refuses_paths_outside(tmp_path):
    (tmp_path / "w").mkdir()
    ws = Workspace(tmp_path / "w")
    assert ws.call("read_file", {"path": "../secret.txt"})["ok"] is False
    assert "outside" in ws.call("write_file", {"path": str(tmp_path / "x.txt"), "content": "x"})["text"]


# --------------------------------------------------------------------------- GPU gate
def test_gpu_gate_serializes_and_cancels():
    g = GpuGate(serialize=True)
    order = []

    def job(name, hold):
        with g.hold(name, model=name):
            order.append(("start", name))
            time.sleep(hold)
            order.append(("end", name))
    t1 = threading.Thread(target=job, args=("a", 0.3))
    t1.start()
    time.sleep(0.05)
    assert g.status()["holder"]["owner"] == "a"
    t2 = threading.Thread(target=job, args=("b", 0.0))
    t2.start()
    t1.join()
    t2.join()
    assert order == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]
    with g.hold("outer"):
        with g.hold("inner"):                         # re-entrant in the same thread
            assert g.status()["holder"]["owner"] == "inner"
    ev = threading.Event()
    ev.set()
    errors = []

    def waiter():
        try:
            with g.hold("x", cancel=ev):
                pass
        except InterruptedError as e:
            errors.append(e)
    with g.hold("busy"):
        th = threading.Thread(target=waiter)
        th.start()
        th.join(2)
    assert len(errors) == 1
    assert should_serialize("auto", 1) and not should_serialize("auto", 2) and should_serialize(True, 4)


# --------------------------------------------------------------------------- Model Manager
def test_model_manager_inventory_register_enable_remove(client, fake):
    c = client
    rep = c.get("/api/desktop/models").json()
    inv = {r["id"]: r for r in rep["inventory"]}
    assert inv["qwen2.5-coder-14b"]["status"] == "installed" and inv["qwen2.5-coder-14b"]["registered"] is True
    assert inv["qwen3-vl-4b"]["installed_id"] == "qwen3-vl:4b"
    assert inv["qwen3.5-9b-heretic-q4"]["status"] == "missing"          # listed, but not installed: never assumed
    assert inv["sdxl-lightning"]["status"] == "unknown"                 # ComfyUI not configured
    r = c.post("/api/desktop/models/register", json={"inventory_id": "qwen3-vl-4b"}).json()
    assert r["config"]["model"] == "qwen3-vl:4b" and r["config"]["autonomous_default"] is False
    assert c.post("/api/desktop/models/register", json={"inventory_id": "qwen3.5-9b-heretic-q4"}).status_code == 400
    by = {p["id"]: p for p in c.get("/api/desktop/presets").json()["presets"]}
    assert by["qwen3-vl-4b"]["status"] == "ok" and by["qwen3-vl-4b"]["execution_modes"] == ["direct"]
    c.post("/api/desktop/models/presets/qwen3-vl-4b/enabled", json={"enabled": False})
    by = {p["id"]: p for p in c.get("/api/desktop/presets").json()["presets"]}
    assert by["qwen3-vl-4b"]["status"] == "disabled"
    adult = c.post("/api/desktop/models/presets", json={"id": "x18", "config": {
        "provider": "ollama", "model": CODER, "content_policy": {"adult": True},
        "execution_modes": ["direct", "autonomous"], "autonomous_default": True}}).json()
    assert adult["config"]["execution_modes"] == ["direct"] and adult["config"]["autonomous_default"] is False
    assert c.delete("/api/desktop/models/presets/qwen3-vl-4b").status_code == 200
    assert c.delete("/api/desktop/models/presets/qwen3-vl-4b").status_code == 404
    c.post("/api/desktop/models/ollama/load", json={"model": CODER, "keep_alive": "5m"})
    assert fake.loaded == [CODER]
    assert c.post("/api/desktop/models/ollama/unload", json={}).json()["unloaded"] == [CODER] and fake.loaded == []


def test_chat_switch_unloads_other_models_on_one_gpu(client, fake):
    fake.loaded = ["qwen3.5:9b"]
    c = client
    conv = c.post("/api/desktop/conversations", json={"preset": "coder"}).json()
    evs = read_stream(c, c.post(f"/api/desktop/conversations/{conv['id']}/send", json={"text": "hi"}).json()["job_id"])
    texts = [d["text"] for e, d in evs if e == "status"]
    assert any("qwen3.5:9b" in t for t in texts) and fake.loaded == [CODER]
    res = c.get("/api/desktop/resources").json()
    assert res["gpu_gate"]["serialize"] is True and res["gpu_gate"]["recent"][-1]["model"] == CODER


# --------------------------------------------------------------------------- workspaces, token, persistence
def test_workspaces_tree_and_forget_keeps_files(client, tmp_path):
    c = client
    assert c.post("/api/desktop/workspaces", json={"name": "x", "path": str(tmp_path / "nope")}).status_code == 400
    w = c.post("/api/desktop/workspaces", json={"name": "Dự án Mới", "create_folder": True}).json()
    from pathlib import Path
    p = Path(w["path"])
    assert p.is_dir() and p.parent.name == "SHAMS_Projects" and p.name == "du_an_moi"
    (p / "src").mkdir()
    (p / "src" / "a.py").write_text("x = 1\n")
    assert [i["name"] for i in c.get(f"/api/desktop/workspaces/{w['id']}/tree").json()["items"]] == ["src"]
    assert c.get(f"/api/desktop/workspaces/{w['id']}/file", params={"path": "src/a.py"}).json()["text"] == "x = 1\n"
    assert c.get(f"/api/desktop/workspaces/{w['id']}/file", params={"path": "../../etc/passwd"}).status_code == 400
    c.delete(f"/api/desktop/workspaces/{w['id']}")
    assert (p / "src" / "a.py").exists() and c.get("/api/desktop/workspaces").json()["workspaces"] == []


def test_token_required_except_health(tmp_path, isolated_home, fake):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "settings.json").write_text(json.dumps(_settings(fake.url)), encoding="utf-8")
    app = create_app(tmp_path / "ws", token="s3cret")
    c = TestClient(app)
    assert c.get("/api/health").json()["token_required"] is True
    assert c.get("/api/desktop/conversations").status_code == 401
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/desktop/conversations", headers={"X-Shams-Token": "s3cret"}).status_code == 200
    assert c.get("/api/desktop/conversations?token=s3cret").status_code == 200
    r = c.options("/api/desktop/conversations", headers={"Origin": "http://tauri.localhost",
                                                          "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == "http://tauri.localhost"
    app.state.desktop.store.close()


def test_history_survives_restart_and_running_jobs_are_marked(tmp_path, isolated_home, fake):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "settings.json").write_text(json.dumps(_settings(fake.url)), encoding="utf-8")
    app = create_app(tmp_path / "ws")
    c = TestClient(app)
    conv = c.post("/api/desktop/conversations", json={"preset": "coder"}).json()
    read_stream(c, c.post(f"/api/desktop/conversations/{conv['id']}/send", json={"text": "hi"}).json()["job_id"])
    st = app.state.desktop.store
    stuck = st.create_job("chat", conversation_id=conv["id"])
    st.update_job(stuck["id"], status="running")
    st.add_message(conv["id"], "assistant", [{"type": "text", "text": "half"}], status="streaming")
    st.close()
    app2 = create_app(tmp_path / "ws")
    c2 = TestClient(app2)
    got = c2.get(f"/api/desktop/conversations/{conv['id']}").json()
    assert [m["status"] for m in got["messages"]] == ["done", "done", "interrupted"]
    assert c2.get("/api/desktop/jobs").json()["jobs"][0]["status"] == "interrupted"
    assert c2.get("/api/desktop/conversations", params={"q": "hi"}).json()["conversations"][0]["id"] == conv["id"]
    app2.state.desktop.store.close()


def test_open_url_only_allows_local_links(client, monkeypatch):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u))
    assert client.post("/api/desktop/open-url", json={"url": "https://evil.example/x"}).status_code == 400
    assert client.post("/api/desktop/open-url", json={"url": "file:///etc/passwd"}).status_code == 400
    assert client.post("/api/desktop/open-url", json={"url": "http://127.0.0.1:8765/play/t/"}).status_code == 200
    import time
    time.sleep(0.2)
    assert opened == ["http://127.0.0.1:8765/play/t/"]

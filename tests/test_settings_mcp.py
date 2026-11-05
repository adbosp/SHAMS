"""ADR-118: real hardware, layered settings, MCP client + permissions, tool-using LLM worker, hooks, app API."""
import json
import sys
from pathlib import Path

import pytest

from shams import hardware, settings
from shams.mcp import MCPManager, Permissions, qualified

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
SERVER = {"type": "stdio", "command": sys.executable, "args": [FAKE]}


# ---------------------------------------------------------------- hardware
def test_parse_nvidia_smi():
    g = hardware.parse_nvidia_smi("0, NVIDIA GeForce RTX 3060, 12288, 3120, 37, 552.22\n")
    assert g == [{"index": 0, "name": "NVIDIA GeForce RTX 3060", "vendor": "NVIDIA", "vram_total_mb": 12288,
                  "vram_used_mb": 3120, "utilization_pct": 37, "driver": "552.22", "source": "nvidia-smi"}]
    assert hardware.parse_nvidia_smi("0, GPU, [N/A], [N/A]\n")[0]["vram_used_mb"] is None


def test_parse_rocm_smi():
    out = json.dumps({"card0": {"Card series": "Radeon RX 7900", "VRAM Total Memory (B)": str(24 * 2**30),
                                "VRAM Total Used Memory (B)": str(2**30)}})
    g = hardware.parse_rocm_smi(out)
    assert g[0]["vram_total_mb"] == 24576 and g[0]["vram_used_mb"] == 1024 and g[0]["vendor"] == "AMD"


def test_detect_is_real_and_cached():
    a = hardware.detect()
    assert a["cpu"]["cores"] >= 1 and "gpus" in a and "ram" in a
    assert hardware.detect() is a
    node = hardware.local_node(a)
    assert node["id"] == "node.local" and len(node["gpus"]) == len(a["gpus"])


# ---------------------------------------------------------------- settings
def test_layers_merge_and_workers_by_id(tmp_path, isolated_home):
    proj = tmp_path / "p"
    settings.save("user", {"permissions": {"allow": ["mcp__a__*"]},
                           "connections": {"local": {"provider": "ollama"}},
                           "workers": [{"id": "w1", "connection": "local", "model": "m1", "node": "node.local",
                                        "capabilities": ["code.python"]}]})
    settings.save("project", {"permissions": {"mode": "full_access", "allow": ["mcp__b__x"]},
                              "workers": [{"id": "w1", "model": "m2"}]}, proj)
    settings.save("local", {"general": {"mock": "off"}}, proj)
    s = settings.load(proj)
    assert s["permissions"] == {"mode": "full_access", "allow": ["mcp__a__*", "mcp__b__x"], "deny": []}
    assert s["general"]["mock"] == "off" and s["general"]["language"] == "vi"
    (w,) = settings.resolve_workers(s)
    assert w["provider"] == "ollama" and w["model"] == "m2" and "connection" not in w
    assert (proj / ".shams" / ".gitignore").read_text().strip() == "settings.local.json"


def test_secrets_are_refused_and_env_refs_allowed(tmp_path):
    with pytest.raises(settings.SettingsError, match="secrets"):
        settings.save("user", {"connections": {"x": {"provider": "openai", "api_key": "sk-123"}}})
    with pytest.raises(settings.SettingsError, match="secrets"):
        settings.save("user", {"mcp_servers": {"gh": {"type": "http", "url": "https://x",
                                                      "headers": {"Authorization": "Bearer abc"}}}})
    with pytest.raises(settings.SettingsError, match="secrets"):
        settings.save("user", {"env": {"GITHUB_TOKEN": "ghp_x"}})
    settings.save("user", {"connections": {"x": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"}},
                           "mcp_servers": {"gh": {"type": "http", "url": "https://x",
                                                  "headers": {"Authorization": "Bearer ${GH_TOKEN}"}}}})
    s = settings.load()
    assert set(settings.env_status(s)) >= {"OPENAI_API_KEY", "GH_TOKEN"}


def test_session_secret_is_not_written(tmp_path, isolated_home, monkeypatch):
    monkeypatch.delenv("SHAMS_TEST_KEY", raising=False)
    settings.set_session_secret("SHAMS_TEST_KEY", "abc")
    import os
    assert os.environ["SHAMS_TEST_KEY"] == "abc"
    assert not list(isolated_home.rglob("*")) or "abc" not in "".join(
        p.read_text() for p in isolated_home.rglob("*") if p.is_file())
    monkeypatch.delenv("SHAMS_TEST_KEY")


def test_mcp_json_and_nodes_toml_are_merged(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "npx", "args": ["x"]},
                                                               "web": {"type": "sse", "url": "http://h/mcp"}}}))
    (proj / "nodes.toml").write_text('[[node]]\nid = "node.a"\nkind = "local"\n\n[[worker]]\nid = "w"\n'
                                     'provider = "ollama"\nmodel = "m"\nnode = "node.a"\ncapabilities = ["code.python"]\n')
    s = settings.load(proj)
    assert s["mcp_servers"]["fs"]["type"] == "stdio" and s["mcp_servers"]["web"]["type"] == "http"
    assert [n["id"] for n in s["nodes"]] == ["node.a"] and s["workers"][0]["id"] == "w"


def test_invalid_values_are_reported():
    errs = settings.validate(settings.merge(settings.DEFAULTS, {
        "permissions": {"mode": "yolo"}, "mcp_servers": {"a": {"type": "stdio"}},
        "workers": [{"id": "w", "connection": "nope"}], "hooks": {"task.*": [{}]}}))
    assert len(errs) == 4


# ---------------------------------------------------------------- MCP
def test_mcp_stdio_tools_and_calls():
    mgr = MCPManager({"fake": SERVER}, Permissions({"mode": "default"}))
    try:
        tools = mgr.tools()
        assert {t["name"] for t in tools} == {"mcp__fake__echo", "mcp__fake__write_note"}
        r = mgr.call("mcp__fake__echo", {"text": "hi"}, tools)
        assert r == {**r, "text": "echo: hi", "is_error": False, "denied": False}
        r = mgr.call("mcp__fake__write_note", {"note": "x"}, tools)
        assert r["denied"] and "permissions.allow" in r["text"]
        mgr.permissions = Permissions({"allow": ["mcp__fake__*"]})
        assert mgr.call("mcp__fake__write_note", {"note": "x"}, tools)["text"] == "saved 1"
        mgr.permissions = Permissions({"mode": "full_access", "deny": ["mcp__fake__write_*"]})
        assert mgr.call("mcp__fake__write_note", {"note": "y"}, tools)["denied"]
        status = mgr.status()
        assert status[0]["ok"] and status[0]["server"]["name"] == "fake" and len(status[0]["tools"]) == 2
    finally:
        mgr.close()


def test_mcp_broken_server_is_reported_not_raised():
    mgr = MCPManager({"bad": {"type": "stdio", "command": "definitely-not-a-program-xyz"}, "fake": SERVER})
    try:
        assert {t["server"] for t in mgr.tools()} == {"fake"}
        rows = {r["name"]: r for r in mgr.status()}
        assert not rows["bad"]["ok"] and "cannot start" in rows["bad"]["error"] and rows["fake"]["ok"]
    finally:
        mgr.close()


def test_permissions_modes():
    ro = {"annotations": {"readOnlyHint": True}}
    assert Permissions({"mode": "read_only"}).check("mcp__a__b", ro)[0]
    assert not Permissions({"mode": "read_only", "allow": ["*"]}).check("mcp__a__b", {})[0]
    assert Permissions({"mode": "full_access"}).check("mcp__a__b", {})[0]
    assert not Permissions({"mode": "full_access", "deny": ["mcp__a__*"]}).check("mcp__a__b", ro)[0]
    assert qualified("my server", "do.it") == "mcp__my_server__do_it"


# ---------------------------------------------------------------- tool-using LLM worker
class FakeToolProvider:
    """Calls echo, then write_note (denied), then answers with the files JSON."""
    supports_tools = True
    name = "fake"

    def __init__(self):
        self.turn = 0
        self.seen_tools = []

    def chat_tools(self, messages, *, model, tools, **_):
        self.turn += 1
        self.seen_tools.append([t["name"] for t in tools])
        if self.turn == 1:
            calls = [{"id": "1", "name": "mcp__fake__echo", "arguments": {"text": "ctx"}},
                     {"id": "2", "name": "mcp__fake__write_note", "arguments": {"note": "n"}}]
            return {"text": "", "tool_calls": calls, "assistant": {"role": "assistant", "content": ""},
                    "tokens_in": 5, "tokens_out": 5}
        results = [m for m in messages if m.get("role") == "tool"]
        assert results[0]["content"] == "echo: ctx" and results[1]["content"].startswith("ERROR: permission denied")
        out = {"files": [{"logical_id": "out.x", "content": "print(1)\n"}], "summary": "done", "status": "READY_FOR_QC"}
        return {"text": json.dumps(out), "tool_calls": [], "assistant": {}, "tokens_in": 7, "tokens_out": 9}

    @staticmethod
    def tool_results(results):
        return [{"role": "tool", "tool_call_id": c["id"], "content": t} for c, t in results]


def test_llm_worker_uses_mcp_tools_with_permissions(tmp_path):
    from shams.workers import LLMWorker
    task = {"task_id": "task.x", "title": "x", "capability": "code.python", "workspace_scope": [],
            "outputs": [{"name": "x", "logical_id": "out.x", "media_type": "text/x-python"}],
            "acceptance_criteria": [{"id": "ac.x", "kind": "exists", "description": "exists"}]}
    ctx = {"project_id": "p1", "task": task, "attempt": 0, "inputs": [], "goal": "g"}
    entry = {"worker": {"worker_id": "w", "kind": "llm_agent", "model": "m"}, "provider_config": {"mcp": ["fake"]}}
    mgr = MCPManager({"fake": SERVER}, Permissions({"mode": "default"}))
    prov = FakeToolProvider()
    try:
        files, handoff = LLMWorker(entry=entry, attempts_root=tmp_path, provider=prov, mcp=mgr).run(
            ctx, entry["worker"])
    finally:
        mgr.close()
    assert handoff["status"] == "READY_FOR_QC" and len(files) == 1
    assert handoff["usage"]["tokens_in"] == 12
    assert [d["decision"] for d in handoff["decisions"]] == ["used tool mcp__fake__echo",
                                                             "used tool mcp__fake__write_note (denied)"]
    assert set(prov.seen_tools[0]) == {"mcp__fake__echo", "mcp__fake__write_note"}


# ---------------------------------------------------------------- hooks + registry
def test_hooks_fire_and_registry_fills_nodes(tmp_path):
    from shams.mock.worker import MockWorker
    from shams.orchestration.project import Shams, build_registry
    proj = tmp_path / "p"
    log = tmp_path / "hooks.log"
    Shams.init_project(proj, "hooked", "CLI Python", mode="autonomous", mock_mode=True)
    cmd = [sys.executable, "-c", f"import os;open(r'{log}','a').write(os.environ['SHAMS_EVENT'][:200]+'\\n')"]
    settings.save("project", {"hooks": {"plan.created": [{"command": cmd}], "run.finished": [{"command": cmd}]}}, proj)
    st = Shams(proj, mock=MockWorker(proj / "attempts"))
    st.plan_from_template()
    st.run()
    st.close()
    lines = log.read_text().splitlines()
    assert any('"plan.created"' in l for l in lines) and any('"run.finished"' in l for l in lines)

    reg = build_registry({"nodes": [], "connections": {}, "workers": [
        {"id": "w.local", "provider": "ollama", "model": "m", "node": "node.local", "capabilities": ["code.python"]},
        {"id": "w.api", "provider": "openai", "model": "gpt", "node": "node.cloud", "lane": "external",
         "capabilities": ["plan"]}]})
    assert reg.nodes["node.local"]["kind"] == "local" and reg.nodes["node.cloud"]["kind"] == "external"


# ---------------------------------------------------------------- app API
fastapi = pytest.importorskip("fastapi")


def test_app_workspace_projects_settings_and_mcp(tmp_path):
    from fastapi.testclient import TestClient
    from apps.api.server import create_app
    ws = tmp_path / "ws"
    c = TestClient(create_app(ws))
    s = c.get("/api/state").json()
    assert s["project"]["id"] is None and s["projects"] == [] and s["version"].startswith("2.")
    s = c.post("/api/projects", json={"name": "Công cụ đổi tên", "domain": "software",
                                      "options": {"stack": "typescript"}}).json()
    assert s["project"]["id"] == "cong_cu_doi_ten" and s["project"]["selected"] == {"stack": "typescript"}
    assert s["mock_mode"] is True                       # no worker configured -> test mode
    c.post("/api/projects", json={"name": "Thứ hai"})
    assert len(c.get("/api/projects").json()["projects"]) == 2
    assert c.post("/api/projects/open", json={"id": "cong_cu_doi_ten"}).json()["project"]["id"] == "cong_cu_doi_ten"

    r = c.put("/api/settings", json={"scope": "project", "data": {"mcp_servers": {"fake": SERVER},
                                                                    "permissions": {"allow": ["mcp__fake__*"]}}})
    assert r.status_code == 200 and "fake" in r.json()["merged"]["mcp_servers"]
    assert c.put("/api/settings", json={"scope": "user", "data": {"connections": {
        "x": {"provider": "openai", "api_key": "sk-1"}}}}).status_code == 400
    (row,) = c.get("/api/mcp").json()
    assert row["ok"] and all(t["allowed"] for t in row["tools"])
    assert c.post("/api/secret", json={"name": "SHAMS_T_KEY", "value": "v"}).json() == {
        "name": "SHAMS_T_KEY", "set": True, "persisted": False}
    t = c.post("/api/connections/test", json={"provider": "openai_compatible",
                                              "config": {"base_url": "http://127.0.0.1:9/v1"}}).json()
    assert t["ok"] is False and t["error"]
    hw = c.get("/api/hardware").json()
    assert "gpus" in hw and "cpu" in hw


def test_app_first_chat_creates_project_in_workspace(tmp_path):
    from fastapi.testclient import TestClient
    from apps.api.server import create_app
    ws = tmp_path / "ws"
    c = TestClient(create_app(ws))
    c.post("/api/configure", json={"domain": "game", "options": {"engine": "godot"}})
    s = c.post("/api/chat", json={"text": "Game giải đố nhỏ"}).json()
    assert s["project"]["domain"] == "game" and s["project"]["selected"] == {"engine": "godot"}
    assert (ws / s["project"]["dir"].split("/")[-1].split("\\")[-1] / "project.json").exists()
    assert s["progress"]["total"] == 11


def test_mcp_http_transport_json_and_sse():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert msg["method"] == "initialize" or self.headers.get("Mcp-Session-Id") == "s1"
            if "id" not in msg:
                self.send_response(202); self.end_headers(); return
            res = {"initialize": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "web"}, "capabilities": {}},
                   "tools/list": {"tools": [{"name": "ping", "inputSchema": {"type": "object"},
                                             "annotations": {"readOnlyHint": True}}]},
                   "tools/call": {"content": [{"type": "text", "text": "pong"}]}}[msg["method"]]
            body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res})
            sse = msg["method"] == "tools/call"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            if msg["method"] == "initialize":
                self.send_header("Mcp-Session-Id", "s1")
            data = (f"event: message\ndata: {body}\n\n" if sse else body).encode()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_DELETE(self):
            self.send_response(200); self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mgr = MCPManager({"web": {"type": "http", "url": f"http://127.0.0.1:{srv.server_port}/mcp"}})
    try:
        tools = mgr.tools()
        assert [t["name"] for t in tools] == ["mcp__web__ping"]
        assert mgr.call("mcp__web__ping", {}, tools)["text"] == "pong"
    finally:
        mgr.close()
        srv.shutdown()


def test_parse_windows_counters():
    out = json.dumps({"memory": [{"Name": "luid_0x1_phys_0", "DedicatedUsage": 2**28, "SharedUsage": 3 * 2**30}],
                      "engines": [{"Name": "pid_1_engtype_3D", "UtilizationPercentage": 12},
                                  {"Name": "pid_2_engtype_3D", "UtilizationPercentage": 5}]})
    assert hardware.parse_windows_counters(out) == {"dedicated_mb": 256, "shared_mb": 3072, "utilization_pct": 17}
    assert hardware.parse_windows_counters('{"memory": null, "engines": null}') == {}
    assert hardware.parse_windows_counters("not json") == {}


def test_logo_and_favicon_are_served(tmp_path):
    from fastapi.testclient import TestClient
    from apps.api.server import create_app
    c = TestClient(create_app(tmp_path / "ws"))
    assert 'src="/static/logo.png"' in c.get("/").text
    r = c.get("/static/logo.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert c.get("/favicon.ico").status_code == 200
    assert c.get("/static/..%2Fserver.py").status_code == 404


def test_app_auto_mock_counts_model_registry_role_workers(tmp_path, monkeypatch):
    """general.mock=auto: workers generated from models.roles count, so a configured PC gets real mode."""
    from apps.api.server import StudioService
    import shams.models as models
    svc = StudioService(tmp_path / "ws")
    s = {"general": {"mock": "auto"}}
    monkeypatch.setattr(models, "merge_role_workers", lambda st: [{"id": "worker.role.coder"}])
    assert svc._mock_default(s) is False
    monkeypatch.setattr(models, "merge_role_workers", lambda st: [])
    assert svc._mock_default(s) is True
    assert svc._mock_default({"general": {"mock": "off"}}) is False
    assert svc._mock_default({"general": {"mock": "on"}}) is True


def test_crash_log_is_enabled(tmp_path):
    import faulthandler
    from apps.api.server import enable_crash_log
    try:
        p = enable_crash_log(tmp_path)
        assert p.exists() and "started" in p.read_text(encoding="utf-8") and faulthandler.is_enabled()
    finally:
        faulthandler.disable()

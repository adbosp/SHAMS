"""Open core (ADR-116): any local model server, any API, custom workers, engines, templates, checkers."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from shams import engines, providers, workers
from shams.nodes.registry import NodeRegistry, normalize_worker
from shams.orchestration.project import Studio
from shams.planning.validator import instantiate, list_templates
from shams.verification import qc

EXAMPLES = Path(__file__).resolve().parent.parent / "shams" / "examples"


# --------------------------------------------------------------------------- fake model server
class FakeServer:
    """Speaks the OpenAI Chat Completions API, Ollama's /api/generate and Anthropic /v1/messages."""

    def __init__(self, fail_first=0):
        self.requests, self.generate, self.fail_first = [], [], fail_first
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._send(200, {"models": []})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/generate":
                    outer.generate.append(body)
                    return self._send(200, {"done": True})
                outer.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                user = next(m["content"] for m in body["messages"] if m["role"] == "user")
                text = outer.reply(user)
                if self.path.endswith("/v1/messages"):
                    return self._send(200, {"content": [{"type": "text", "text": text}],
                                            "usage": {"input_tokens": 11, "output_tokens": 7}})
                self._send(200, {"choices": [{"message": {"content": text}}],
                                 "usage": {"prompt_tokens": 11, "completion_tokens": 7}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reply(self, user: str) -> str:
        try:
            req = json.loads(user)
        except json.JSONDecodeError:
            return "pong"
        n = sum(1 for r in self.requests if req["task"]["task_id"] in json.dumps(r["body"]))
        if n <= self.fail_first:
            return "Sorry, here is some prose and no JSON."
        files = [{"logical_id": o["logical_id"], "content": f"// {o['path']} for {req['task']['task_id']}\n"}
                 for o in req["outputs"]]
        return "```json\n" + json.dumps({"files": files, "summary": f"did {req['task']['title']}",
                                         "decisions": ["kept it simple"], "status": "READY_FOR_QC"}) + "\n```"

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


GEN_SCRIPT = """
import json, os, sys
ctx = json.load(open(sys.argv[1], encoding="utf-8"))
for o in ctx["outputs"]:
    os.makedirs(os.path.dirname(o["abs"]), exist_ok=True)
    open(o["abs"], "wb").write(b"generated " + o["logical_id"].encode())
json.dump({"summary": "generated " + ctx["task"]["task_id"]}, open(os.path.join(os.environ["SHAMS_OUT_DIR"], "handoff.json"), "w"))
"""


def real_nodes(tmp_path, base_url, provider="ollama", engine="web"):
    script = tmp_path / "gen.py"
    script.write_text(GEN_SCRIPT, encoding="utf-8")
    caps = {t["capability"] for t in instantiate("vertical_slice_3d", "real_game", "g", engine=engine)["tasks"]}
    text_caps = sorted(c for c in caps if c.startswith(("code.", "integration")))
    tool_caps = sorted(caps - set(text_caps))
    return NodeRegistry.from_dict({
        "node": [{"id": "node.local", "kind": "local", "cpu_slots": 2, "gpus": [{"index": 0, "vram_mb": 12288}]}],
        "worker": [
            {"id": "worker.coder", "provider": provider, "model": "coder-model:7b", "node": "node.local",
             "lane": "gpu", "vram_mb": 6000, "capabilities": text_caps, "config": {"base_url": base_url}},
            {"id": "worker.generator", "provider": "command", "node": "node.local", "lane": "cpu",
             "capabilities": tool_caps, "config": {"argv": [sys.executable, str(script), "{context}"]}},
        ]})


def passing(kind, criterion, contents):
    return "PASS", f"{kind} ok"


# Stand-ins for real engine checkers: compile/test results are critical, so without a checker even
# autonomous mode sends the task to human review (ADR-010).
CHECKERS = {k: passing for k in ("compile", "unit_test", "integration_test")}


def make_real(tmp_path, base_url, provider="ollama", engine="web", checkers=None, **kw):
    d = tmp_path / "proj"
    Studio.init_project(d, "real_game", "Game đua xe", mode="autonomous", engine={"name": engine})
    st = Studio(d, nodes=real_nodes(tmp_path, base_url, provider, engine), checkers=checkers or dict(CHECKERS), **kw)
    st.load_plan(instantiate("vertical_slice_3d", "real_game", "Game đua xe", engine=engine))
    return st


# --------------------------------------------------------------------------- real worker path
def test_full_run_with_local_model_server_and_command_tool(tmp_path, server):
    st = make_real(tmp_path, server.url)
    s = st.run()
    assert s["done"] and s["counts"] == {"ACCEPTED": 11}
    assert {r["body"]["model"] for r in server.requests} == {"coder-model:7b"}
    assert all(r["path"] == "/v1/chat/completions" for r in server.requests)
    # Ollama got an explicit load (keep_alive) and a final unload (keep_alive 0) from the Model Lifecycle
    assert server.generate[0]["keep_alive"] == "30m" and server.generate[-1]["keep_alive"] == 0
    h = st.db.handoff("task.player_controller")
    assert h["producer"]["provider"] == "ollama" and h["usage"]["tokens_out"] == 7
    assert "did " in h["context_summary"]
    code = st.artifacts.accepted("code.player_controller")
    assert "for task.player_controller" in Path(st.artifacts.blob_path(code["sha256"])).read_text()
    mesh = st.artifacts.accepted("asset.player.mesh")
    assert Path(st.artifacts.blob_path(mesh["sha256"])).read_bytes() == b"generated asset.player.mesh"
    # the integration task saw its inputs' content in the prompt
    integ = [r for r in server.requests if "task.integration_slice1" in json.dumps(r["body"])][0]
    assert "for task.player_controller" in integ["body"]["messages"][1]["content"]


def test_unparseable_reply_goes_through_repair(tmp_path):
    srv = FakeServer(fail_first=1)
    try:
        st = make_real(tmp_path, srv.url + "/v1", provider="openai_compatible")
        assert st.run()["done"]
        f = st.db.failures("task.project_scaffold")[0]
        assert "schema violation" in f["summary"]
        assert st.db.task("task.project_scaffold")["attempt"] == 1
    finally:
        srv.close()


def test_anthropic_provider(server, monkeypatch):
    monkeypatch.setenv("SHAMS_TEST_KEY", "sk-test")
    p = providers.create("anthropic", {"base_url": server.url, "api_key_env": "SHAMS_TEST_KEY"})
    r = p.chat([{"role": "system", "content": "be brief"}, {"role": "user", "content": "ping"}], model="m")
    assert r == {"text": "pong", "tokens_in": 11, "tokens_out": 7}
    req = server.requests[-1]
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert req["body"]["system"] == "be brief" and headers["x-api-key"] == "sk-test"
    monkeypatch.delenv("SHAMS_TEST_KEY")
    with pytest.raises(providers.ProviderError, match="SHAMS_TEST_KEY"):
        p.chat([{"role": "user", "content": "x"}], model="m")


def test_api_key_is_sent_as_bearer(server, monkeypatch):
    monkeypatch.setenv("SHAMS_TEST_KEY", "sk-abc")
    p = providers.create("openai", {"base_url": server.url + "/v1", "api_key_env": "SHAMS_TEST_KEY"})
    assert p.chat([{"role": "user", "content": "hi"}], model="gpt-x")["text"] == "pong"
    assert server.requests[-1]["headers"]["Authorization"] == "Bearer sk-abc"


def test_presets_cover_local_servers_and_apis():
    names = set(providers.available())
    assert {"ollama", "llamacpp", "lmstudio", "vllm", "openai_compatible", "openai", "anthropic", "gemini",
            "openrouter", "command"} <= names
    with pytest.raises(KeyError):
        providers.create("nope")


# --------------------------------------------------------------------------- custom code
class EchoProvider:
    name = "echo"

    def __init__(self, prefix="echo", **_):
        self.prefix = prefix

    def chat(self, messages, *, model, **_):
        req = json.loads(messages[-1]["content"])
        files = [{"logical_id": o["logical_id"], "content": self.prefix} for o in req["outputs"]]
        return {"text": json.dumps({"files": files, "summary": "echo", "status": "READY_FOR_QC"}),
                "tokens_in": 1, "tokens_out": 1}


class StaticWorker:
    def __init__(self, *, entry, attempts_root, **_):
        self.attempts_root = attempts_root

    def run(self, ctx, worker_ref):
        _, plan = workers.output_plan(ctx, self.attempts_root)
        for o in plan:
            Path(o["abs"]).parent.mkdir(parents=True, exist_ok=True)
            Path(o["abs"]).write_text("static")
        return plan, workers.base_handoff(ctx, worker_ref, "READY_FOR_QC", "static worker")


def test_custom_provider_and_worker_by_reference(tmp_path):
    caps = sorted({t["capability"] for t in instantiate("vertical_slice_3d", "real_game", "g", engine="godot")["tasks"]})
    code = [c for c in caps if c.startswith(("code.", "integration"))]
    reg = NodeRegistry.from_dict({
        "node": [{"id": "node.cloud", "kind": "external", "rate_limit_per_min": 5}],
        "worker": [
            {"id": "worker.echo", "provider": "test_plugins:EchoProvider", "model": "any", "node": "node.cloud",
             "lane": "external", "privacy": "external_ok", "capabilities": code, "config": {"prefix": "hello"}},
            {"id": "worker.static", "node": "node.cloud", "lane": "external", "capabilities": sorted(set(caps) - set(code)),
             "config": {"impl": "test_plugins:StaticWorker"}},
        ]})
    d = tmp_path / "p"
    Studio.init_project(d, "real_game", "g", mode="autonomous", engine={"name": "godot"})
    st = Studio(d, nodes=reg, checkers=dict(CHECKERS))
    st.load_plan(instantiate("vertical_slice_3d", "real_game", "g", engine="godot"))
    assert st.run()["done"]
    ref = st.artifacts.accepted("code.player_controller")
    assert Path(st.artifacts.blob_path(ref["sha256"])).read_text() == "hello"


def test_registered_checker_drives_qc(tmp_path, server):
    seen = []

    def compile_checker(kind, criterion, contents):
        seen.append(criterion["id"])
        return ("FAIL", "error: broken") if seen.count(criterion["id"]) == 1 and criterion["id"] == "ac.hud_compiles" \
            else ("PASS", "compiled")

    qc.register_checker("compile", compile_checker)
    try:
        d = tmp_path / "proj"
        Studio.init_project(d, "real_game", "g", mode="autonomous", engine={"name": "web"})
        st = Studio(d, nodes=real_nodes(tmp_path, server.url))      # checkers come from the registry
        st.qc.checkers.update({k: passing for k in ("unit_test", "integration_test")})
        st.load_plan(instantiate("vertical_slice_3d", "real_game", "g", engine="web"))
        assert st.run()["done"]
        assert [r["verdict"] for r in st.db.qc_reports("task.hud")] == ["FAIL", "PASS"]
    finally:
        qc._CHECKERS.clear()
    with pytest.raises(ValueError):
        qc.register_checker("smell", compile_checker)


# --------------------------------------------------------------------------- custom engine + template
def test_custom_engine_from_project_folder(tmp_path):
    d = tmp_path / "bevy_game"
    (d / "engines").mkdir(parents=True)
    (d / "engines" / "bevy.toml").write_text((EXAMPLES / "engine.example.toml").read_text(encoding="utf-8"))
    try:
        from apps.cli.main import main
        assert main(["init", str(d), "--project-id", "bevy_game", "--goal", "g", "--engine", "bevy",
                     "--mode", "autonomous", "--mock"]) == 0
        assert main(["plan", str(d)]) == 0
        st = Studio(d)
        assert st.run()["done"]
        assert any(t["capability"] == "code.bevy_rust" for t in st.db.tasks())
        assert engines.detect("một game viết bằng Bevy") == "bevy"
        cmds = engines.EngineAdapter(engines.get("bevy"), d).commands()
        assert cmds["test"] == ["cargo", "test", "--manifest-path", f"{d}/Cargo.toml"]
        st.close()
    finally:
        engines.ENGINES.pop("bevy", None)
        for k in [k for k, v in engines.ALIASES.items() if v == "bevy"]:
            engines.ALIASES.pop(k)
        engines._DETECT[:] = [x for x in engines._DETECT if x[0] != "bevy"]


def test_custom_engine_validation():
    with pytest.raises(ValueError, match="roots missing"):
        engines.EngineProfile.from_dict({"name": "x", "roots": {}})


def test_project_template_folder(tmp_path):
    d = tmp_path / "p"
    (d / "templates").mkdir(parents=True)
    tpl = json.loads((Path(engines.__file__).parent.parent / "templates" / "vertical_slice_3d.json").read_text("utf-8"))
    tpl["tasks"] = tpl["tasks"][:1]
    (d / "templates" / "tiny.json").write_text(json.dumps(tpl), encoding="utf-8")
    assert "tiny" in list_templates(d)
    dag = instantiate("tiny", "real_game", "g", engine="unity", project_dir=d)
    assert len(dag["tasks"]) == 1 and dag["template"] == "tiny@unity"


# --------------------------------------------------------------------------- config format
def test_example_nodes_toml_parses():
    reg = NodeRegistry.from_toml(EXAMPLES / "nodes.example.toml")
    assert {"worker.coder_local", "worker.reasoner_api"} <= set(reg.workers)
    assert reg.workers["worker.coder_local"].entry["runtime"]["load_adapter"] == "ollama"
    assert "api_key" not in json.dumps([w.entry for w in reg.workers.values()]).replace("api_key_env", "")
    assert {l.kind for l in reg.lanes()} == {"cpu", "gpu", "external"}


def test_short_worker_form_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown keys"):
        normalize_worker({"id": "w", "node": "n", "capabilities": ["code.x"], "api_key": "sk-123"})

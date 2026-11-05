"""Direct Mode, presets, content policy and research tools (ADR-121). Runs against fakes only."""
import json

import pytest

from shams import models, policy, settings as settings_mod
from shams.orchestration.project import Shams
from shams.research import Research, ToolBox
from shams.verification import assets

import tests.test_local_models as tlm

HERETIC = "hf.co/darrellbest/Qwen3.5-9B-Heretic-GGUF:Q4_K_M"
PRESETS = {
    "qwen35-heretic-q4": {"provider": "ollama", "model": HERETIC, "capabilities": ["chat", "text_generation"],
                          "execution_modes": ["direct"], "autonomous_default": False},
    "sdxl": {"provider": "comfyui", "workflow": "sdxl_lightning_4step", "label": "SDXL Lightning"},
    "moss_image_nsfw_lora": {"provider": "comfyui", "workflow": "moss_image_lora", "execution_modes": ["direct"],
                             "content_policy": {"adult": True}},
}


@pytest.fixture
def ollama(monkeypatch):
    monkeypatch.setattr(tlm, "INSTALLED", tlm.INSTALLED + [HERETIC])
    monkeypatch.setattr(tlm, "SIZES", {**tlm.SIZES, HERETIC: 5600})
    monkeypatch.setattr(tlm, "CAPS", {**tlm.CAPS, HERETIC: ["completion", "tools"]})
    o = tlm.FakeOllama(reply=lambda body: f"reply from {body['model']}")
    models._CACHE.clear()
    yield o
    o.close()
    models._CACHE.clear()


def _settings(ollama_url, comfy_url=None, **general):
    s = {"general": general,
         "nodes": [{"id": "node.local", "kind": "local", "cpu_slots": 2, "gpus": [{"index": 0, "vram_mb": 12288}]}],
         "models": {"ollama": {"base_url": ollama_url}, "roles": {"coder": {"model": "qwen2.5-coder:14b"}},
                                        "presets": PRESETS}}
    if comfy_url:
        s["tools"] = {"comfyui": {"base_url": comfy_url, "poll_seconds": 0.01}}
    return s


# --------------------------------------------------------------------------- registry
def test_presets_carry_a_contract_and_stay_out_of_autonomous_mode(ollama):
    reg = models.ModelRegistry(_settings(ollama.url, "http://127.0.0.1:9"))
    ps = {p["id"]: p for p in reg.presets()}
    h = ps["qwen35-heretic-q4"]
    assert h["status"] == "ok" and h["model"] == HERETIC and h["provider"] == "ollama"
    assert h["execution_modes"] == ["direct"] and h["autonomous_default"] is False
    assert h["worker"]["capabilities"] == ["direct.qwen35_heretic_q4"]        # never matches a planned task
    assert h["input_schema"]["required"] == ["prompt"] and h["outputs"][0]["media_type"] == "text/markdown"
    assert h["worker"]["vram_mb"] > 5600 and h["worker"]["config"]["impl"] == "direct_chat"
    assert ps["coder"]["source"] == "role" and ps["coder"]["status"] == "ok"
    assert ps["sdxl"]["kind"] == "image" and ps["sdxl"]["worker"]["config"]["impl"] == "comfyui_image"
    adult = ps["moss_image_nsfw_lora"]
    assert adult["status"] == "disabled_adult" and "worker" not in adult and adult["autonomous_default"] is False
    ws = {w["id"] for w in models.merge_role_workers(_settings(ollama.url), [])}
    assert "worker.direct.qwen35_heretic_q4" in ws and not any("moss" in w for w in ws)


def test_adult_preset_can_never_be_autonomous(ollama):
    bad = {"models": {"presets": {"x": {"provider": "comfyui", "content_policy": {"adult": True},
                                        "autonomous_default": True, "execution_modes": ["direct", "autonomous"]}}}}
    errs = settings_mod.validate(settings_mod.merge(settings_mod.DEFAULTS, bad))
    assert any("autonomous_default" in e for e in errs) and any("direct mode" in e for e in errs)
    s = _settings(ollama.url, "http://127.0.0.1:9", allow_adult_content=True)
    s["models"]["presets"] = {"x": {"provider": "comfyui", "content_policy": {"adult": True}, "autonomous_default": True,
                                    "execution_modes": ["direct", "autonomous"], "capabilities": ["image.generate"]}}
    p = models.ModelRegistry(s).preset("x")
    assert p["autonomous_default"] is False and p["worker"]["capabilities"] == ["direct.x"]


# --------------------------------------------------------------------------- policy
@pytest.mark.parametrize("prompt", ["a teen on the beach", "schoolgirl outfit", "16 years old", "drugged woman",
                                    "deepfake of a celebrity", "nữ sinh", "my ex naked", "hidden camera shot"])
def test_policy_refuses_minors_non_consent_and_real_people(prompt):
    pre = {"id": "a", "kind": "image", "content_policy": {"adult": True}}
    with pytest.raises(policy.PolicyError):
        policy.check(pre, {"prompt": prompt}, allowed=True, confirmed=True)


def test_policy_gates_and_adds_negative_prompt():
    pre = {"id": "a", "kind": "image", "content_policy": {"adult": True}}
    with pytest.raises(policy.PolicyError, match="allow_adult_content"):
        policy.check(pre, {"prompt": "x"}, allowed=False, confirmed=True)
    with pytest.raises(policy.PolicyError, match="confirm"):
        policy.check(pre, {"prompt": "x"}, allowed=True, confirmed=False)
    out = policy.check(pre, {"prompt": "two adult women at a spa", "negative": "blurry"}, allowed=True, confirmed=True)
    assert out["negative"].startswith("blurry, ") and "minor" in out["negative"]
    assert policy.check({"id": "b"}, {"prompt": "a teen wizard"}, allowed=False, confirmed=False) == {"prompt": "a teen wizard"}


# --------------------------------------------------------------------------- research
def test_research_refuses_local_and_private_addresses(ollama):
    r = Research({"enabled": True})
    for url in (ollama.url + "/api/tags", "http://10.0.0.5/", "http://192.168.1.1/", "file:///etc/passwd",
                "http://localhost:8188/prompt", "ftp://example.com/"):
        res = r.call("research__web_fetch", {"url": url})
        assert res["is_error"], url
    assert not any(c[1] == "/api/tags" for c in ollama.server.calls)          # never reached
    deny = Research({"enabled": True, "deny_domains": ["example.com"]})
    assert "denied" in deny.call("research__web_fetch", {"url": "https://www.example.com/"})["text"]
    allow = Research({"enabled": True, "allow_domains": ["wikipedia.org"]})
    assert "allow_domains" in allow.call("research__web_fetch", {"url": "https://example.com/"})["text"]
    assert Research({}).tools() == [] and len(r.tools()) == 2


def test_research_parses_results_and_marks_web_text_untrusted(monkeypatch):
    r = Research({"enabled": True, "search_url": "https://searx.example/search?q={query}&format=json"})
    seen = []
    def fake_get(url):
        seen.append(url)
        if "searx" in url:
            return json.dumps({"results": [{"title": "T", "url": "https://a.example/x", "content": "S"}]}), "application/json"
        return "<html><head><title>x</title></head><body><script>bad()</script><p>Hello</p><p>World</p></body></html>", "text/html"
    monkeypatch.setattr(r, "_get", fake_get)
    out = r.call("research__web_search", {"query": "blender bpy"})
    assert "q=blender+bpy" in seen[0] and "https://a.example/x" in out["text"] and out["text"].startswith("[UNTRUSTED")
    page = r.call("research__web_fetch", {"url": "https://a.example/x"})["text"]
    assert "Hello" in page and "bad()" not in page
    box = ToolBox(None, r)
    assert [t["name"] for t in box.tools(None)] == ["research__web_search", "research__web_fetch"]
    assert r.log[0]["urls"] == ["https://a.example/x"]


# --------------------------------------------------------------------------- Direct Mode end to end (fakes)
def _project(tmp_path, isolated_home, s):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "settings.json").write_text(json.dumps(s), encoding="utf-8")
    d = tmp_path / "p"
    Shams.init_project(d, "direct", "direct tasks", mode="guided", domain="software", domain_config={"stack": "python"})
    return Shams(d)


def test_direct_chat_runs_one_task_and_switches_models(tmp_path, isolated_home, ollama):
    st = _project(tmp_path, isolated_home, _settings(ollama.url))
    try:
        tid = st.direct("qwen35-heretic-q4", {"prompt": "Say hi", "temperature": 0.3})
        assert tid == "task.direct.0001"
        res = st.run_task(tid)
        assert res["state"] == "ACCEPTED", res
        art = res["outputs"][0]
        assert art["media_type"] == "text/markdown"
        assert st.artifacts.blob_path(art["sha256"]).read_text(encoding="utf-8") == f"reply from {HERETIC}"
        chat = [c[2] for c in ollama.server.calls if c[1] == "/api/chat"][-1]
        assert chat["options"]["temperature"] == 0.3 and chat["messages"][-1]["content"] == "Say hi"
        res2 = st.run_task(st.direct("coder", {"prompt": "write fizzbuzz"}))
        assert res2["state"] == "ACCEPTED"
        unloads = [c[2]["model"] for c in ollama.server.calls if c[1] == "/api/generate" and c[2].get("keep_alive") == 0]
        assert HERETIC in unloads                                                # model switch on the GPU lane
        assert ollama.loaded == [] or HERETIC not in ollama.loaded
        ev = [r[0] for r in st.db.q("SELECT type FROM events WHERE type LIKE 'direct.%'")]
        assert ev.count("direct.queued") == 2
    finally:
        st.close()


def test_direct_rejects_bad_input_unknown_presets_and_adult_without_opt_in(tmp_path, isolated_home, ollama):
    st = _project(tmp_path, isolated_home, _settings(ollama.url, "http://127.0.0.1:9"))
    try:
        with pytest.raises(KeyError):
            st.direct("nope", {"prompt": "x"})
        with pytest.raises(ValueError, match="invalid parameters"):
            st.direct("qwen35-heretic-q4", {"temperature": 0.3})               # prompt is required
        with pytest.raises(policy.PolicyError, match="allow_adult_content"):
            st.direct("moss_image_nsfw_lora", {"prompt": "x"}, confirm_adult=True)
        n = st.db.q1("SELECT COUNT(*) FROM events WHERE type='policy.refused'")[0]
        assert n == 1 and not st.db.tasks()
    finally:
        st.close()


def test_direct_adult_preset_refuses_prompts_even_when_enabled(tmp_path, isolated_home, ollama):
    st = _project(tmp_path, isolated_home, _settings(ollama.url, "http://127.0.0.1:9", allow_adult_content=True))
    try:
        with pytest.raises(policy.PolicyError, match="confirm"):
            st.direct("moss_image_nsfw_lora", {"prompt": "two adults"})
        with pytest.raises(policy.PolicyError, match="minors"):
            st.direct("moss_image_nsfw_lora", {"prompt": "a teen girl"}, confirm_adult=True)
        assert not st.db.tasks()
    finally:
        st.close()


def test_direct_image_task_through_comfyui(tmp_path, isolated_home, ollama):
    fc = tlm.FakeComfy()
    try:
        st = _project(tmp_path, isolated_home, _settings(ollama.url, fc.url))
        try:
            res = st.run_task(st.direct("sdxl", {"prompt": "a cabin in the snow", "seed": 7}))
            assert res["state"] == "ACCEPTED", res
            png = st.artifacts.blob_path(res["outputs"][0]["sha256"])
            assert assets.check_png(png)[0] == "PASS"
            sent = [c[2] for c in fc.server.calls if c[1] == "/prompt"][-1]["prompt"]
            assert any("a cabin in the snow" == n["inputs"].get("text") for n in sent.values())
        finally:
            st.close()
    finally:
        fc.close()


def test_direct_api_runs_a_task_and_serves_the_artifact(tmp_path, isolated_home, ollama):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from apps.api.server import create_app
    import time
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "settings.json").write_text(json.dumps(_settings(ollama.url, "http://127.0.0.1:9")), encoding="utf-8")
    c = TestClient(create_app(tmp_path / "ws"))
    ps = c.get("/api/direct/presets").json()
    by = {p["id"]: p for p in ps["presets"]}
    assert by["qwen35-heretic-q4"]["status"] == "ok" and "config" not in json.dumps(by["qwen35-heretic-q4"].get("worker", {}))
    assert by["moss_image_nsfw_lora"]["status"] == "disabled_adult" and ps["allow_adult_content"] is False
    assert c.post("/api/direct/run", json={"preset": "moss_image_nsfw_lora", "params": {"prompt": "x"},
                                           "confirm_adult": True}).status_code == 403
    assert c.post("/api/direct/run", json={"preset": "nope", "params": {"prompt": "x"}}).status_code == 404
    assert c.post("/api/direct/run", json={"preset": "qwen35-heretic-q4", "params": {}}).status_code == 400
    t = c.post("/api/direct/run", json={"preset": "qwen35-heretic-q4", "params": {"prompt": "hello"}}).json()
    end = time.time() + 20
    while time.time() < end and (t["running"] or t["state"] not in ("ACCEPTED", "FAILED", "BLOCKED", "HUMAN_REVIEW")):
        time.sleep(0.05)
        t = c.get(f"/api/direct/tasks/{t['task_id']}").json()
    assert t["state"] == "ACCEPTED", t
    assert any(e["type"] == "direct.queued" for e in t["events"])
    r = c.get(t["outputs"][0]["url"])
    assert r.status_code == 200 and r.text == f"reply from {HERETIC}" and r.headers["content-type"].startswith("text/markdown")
    assert c.get("/api/direct/tasks").json()["tasks"][0]["task_id"] == t["task_id"]
    assert not any(p["folder"] == "_direct" for p in c.get("/api/projects").json()["projects"])

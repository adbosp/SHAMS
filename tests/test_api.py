"""Main UI API: chat creates the plan, state reflects the live Studio, human decisions work."""
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from apps.api.server import create_app  # noqa: E402


def _wait_idle(client, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get("/api/state").json()
        if not s["running"]:
            return s
        time.sleep(0.05)
    raise AssertionError("studio did not finish")


def test_ui_page_is_branded_shams(tmp_path):
    c = TestClient(create_app(tmp_path / "p"))
    r = c.get("/")
    assert r.status_code == 200
    assert "<title>SHAMS</title>" in r.text and "AI GAME STUDIO" not in r.text.upper()


def test_chat_creates_plan_and_runs_to_done(tmp_path):
    c = TestClient(create_app(tmp_path / "p"))
    s = c.get("/api/state").json()
    assert s["app"] == "SHAMS" and s["progress"] == {"done": 0, "total": 0}
    s = c.post("/api/chat", json={"text": "Tạo game cá mập 3D có hệ thống săn mồi và tiến hóa."}).json()
    assert s["progress"]["total"] == 14
    assert s["chat"][1]["role"] == "director"
    s = _wait_idle(c)
    assert s["progress"] == {"done": 14, "total": 14}
    assert any("Loading mock-coder" in l["text"] for l in s["console"])
    assert {a["state"] for a in s["artifacts"]} == {"ACCEPTED"}
    assert "gpus" in s["hardware"]                      # real hardware, not a simulation
    assert s["vram"] is None or s["vram"]["total_mb"] > 0


def test_decide_rejects_invalid_task(tmp_path):
    c = TestClient(create_app(tmp_path / "p"))
    r = c.post("/api/decide", json={"task_id": "task.nope", "decision": "accept"})
    assert r.status_code == 400


def test_chat_detects_domain_and_engine_and_configure_endpoint(tmp_path):
    c = TestClient(create_app(tmp_path / "p"))
    s = c.get("/api/state").json()
    assert {"software", "game"} <= {p["id"] for p in s["packs"]}
    game = next(p for p in s["packs"] if p["id"] == "game")
    assert {e["id"] for e in game["options"]["engine"]} >= {"unity", "unreal", "godot", "web"}
    s = c.post("/api/configure", json={"domain": "game", "options": {"engine": "godot"}}).json()
    assert s["project"]["domain"] == "game" and s["project"]["selected"] == {"engine": "godot"}
    assert c.post("/api/configure", json={"options": {"engine": "nope"}}).status_code == 400
    assert c.post("/api/configure", json={"domain": "software", "options": {"engine": "web"}}).status_code == 400
    s = c.post("/api/chat", json={"text": "Game cá mập trên trình duyệt bằng Three.js WebGPU"}).json()
    assert s["project"]["selected"] == {"engine": "web"} and "Three.js" in s["chat"][1]["text"]
    assert any(t["capability"] == "code.web_typescript" for t in s["tasks"])
    s = _wait_idle(c)
    assert s["progress"] == {"done": 14, "total": 14}
    assert c.post("/api/configure", json={"options": {"engine": "unity"}}).status_code == 400


def test_chat_for_software_request_uses_software_pack(tmp_path):
    c = TestClient(create_app(tmp_path / "p"))
    s = c.post("/api/chat", json={"text": "Viết một công cụ CLI bằng Python để đổi tên file hàng loạt"}).json()
    assert s["project"]["domain"] == "software" and s["project"]["selected"] == {"stack": "python"}
    assert {t["capability"] for t in s["tasks"]} >= {"code.python", "build.python", "integration"}
    s = _wait_idle(c)
    assert s["progress"] == {"done": 5, "total": 5}

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
    assert s["progress"]["total"] == 11
    assert s["chat"][1]["role"] == "director"
    s = _wait_idle(c)
    assert s["progress"] == {"done": 11, "total": 11}
    assert any("Loading mock-coder" in l["text"] for l in s["console"])
    assert {a["state"] for a in s["artifacts"]} == {"ACCEPTED"}
    assert s["vram"]["simulated"] is True


def test_decide_rejects_invalid_task(tmp_path):
    c = TestClient(create_app(tmp_path / "p"))
    r = c.post("/api/decide", json={"task_id": "task.nope", "decision": "accept"})
    assert r.status_code == 400

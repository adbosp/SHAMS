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
    assert s["progress"]["total"] == 12
    assert s["chat"][1]["role"] == "director"
    s = _wait_idle(c)
    assert s["progress"] == {"done": 12, "total": 12}
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
    assert s["progress"] == {"done": 12, "total": 12}
    assert c.post("/api/configure", json={"options": {"engine": "unity"}}).status_code == 400


def test_chat_for_software_request_uses_software_pack(tmp_path):
    c = TestClient(create_app(tmp_path / "p"))
    s = c.post("/api/chat", json={"text": "Viết một công cụ CLI bằng Python để đổi tên file hàng loạt"}).json()
    assert s["project"]["domain"] == "software" and s["project"]["selected"] == {"stack": "python"}
    assert {t["capability"] for t in s["tasks"]} >= {"code.python", "build.python", "integration"}
    s = _wait_idle(c)
    assert s["progress"] == {"done": 5, "total": 5}


def test_accepted_playable_build_is_announced_and_served(tmp_path, monkeypatch):
    app = create_app(tmp_path / "p")
    c = TestClient(app)
    c.post("/api/chat", json={"text": "Tạo game cá mập 3D có hệ thống săn mồi và tiến hóa."})
    _wait_idle(c)
    svc = app.state.svc
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    svc.open_game = True
    play = tmp_path / "play"
    (play / "assets").mkdir(parents=True)
    (play / "index.html").write_text("<html>game</html>", encoding="utf-8")
    (play / "assets" / "main.js").write_text("console.log(1)", encoding="utf-8")
    db = svc.studio.db
    tid = db.tasks()[-1]["task_id"]
    h = db.handoff(tid)
    h["decisions"] = list(h.get("decisions") or []) + [{"decision": "playable build", "reason": str(play)}]
    db.save_handoff(h)
    rep = db.qc_reports(tid)[-1]
    rep["criteria"] = list(rep["criteria"]) + [{"criterion_id": "ac.smoke", "verdict": "FAIL", "stage": 4}]
    db.save_qc(rep)                                          # accepted by hand although a check failed
    svc.announce_playable()
    svc.announce_playable()                                  # once only
    msgs = [m for m in svc.chat if m.get("play")]
    assert len(msgs) == 1 and f"http://127.0.0.1:8765/play/{tid}/" in msgs[0]["text"]
    assert "ac.smoke" in msgs[0]["text"] and msgs[0]["url"].endswith(f"/play/{tid}/")
    for _ in range(50):
        if opened:
            break
        time.sleep(0.02)
    assert opened == [f"http://127.0.0.1:8765/play/{tid}/"]
    r = c.get(f"/play/{tid}/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("http://127.0.0.1:")
    import urllib.request
    base = r.headers["location"]
    assert urllib.request.urlopen(base).read() == b"<html>game</html>"
    with urllib.request.urlopen(base + "assets/main.js") as f:          # absolute /assets/... paths work too
        assert f.headers["Content-Type"].startswith("text/javascript")
    assert c.get(f"/play/{tid}/assets/main.js", follow_redirects=False).headers["location"] == base + "assets/main.js"
    assert svc.game_server(play) == base                                # one server per build
    assert c.get("/play/nope/", follow_redirects=False).status_code == 404


def test_delete_project_moves_it_to_trash(tmp_path):
    c = TestClient(create_app(tmp_path / "ws"))
    s = c.post("/api/projects", json={"name": "keep me"}).json()
    s = c.post("/api/projects", json={"name": "drop me"}).json()
    drop = s["project"]["id"]
    assert {p["id"] for p in s["projects"]} >= {drop}
    s = c.post("/api/projects/delete", json={"id": drop}).json()
    assert drop not in {p["id"] for p in s["projects"]} and s["project"].get("id") != drop
    trashed = list((tmp_path / "ws" / "_trash").iterdir())
    assert len(trashed) == 1 and (trashed[0] / "project.json").exists()
    assert c.post("/api/projects/delete", json={"id": drop}).status_code == 404

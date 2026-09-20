from conftest import PERSONA
from fastapi.testclient import TestClient

from cc_chat.web import make_app


def client(env):
    e = env[0]
    app = make_app(e, run_worker=False)
    token = (e.settings.data_dir / "access.token").read_text()
    return TestClient(app, headers={"X-CC-Chat-Token": token})


def test_pages_and_local_auth(env):
    c = client(env)
    for page in ["/", "/persona", "/life", "/memories"]:
        r = c.get(page)
        assert r.status_code == 200 and "AICHAT" in r.text
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/status", headers={"X-CC-Chat-Token": ""}).status_code == 403
    assert c.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
    assert (
        c.post("/api/pause", json={"paused": True}, headers={"Origin": "https://evil.example"}).status_code
        == 403
    )


def test_persona_changes_version_and_future_only(env):
    c = client(env)
    before = c.get("/api/persona").json()["current"]["version"]
    r = c.put("/api/persona", json={**PERSONA, "major": "物理"})
    assert r.status_code == 200
    after = c.get("/api/persona").json()
    assert after["current"]["version"] > before and len(after["history"]) == 2
    assert c.put("/api/persona", json={**PERSONA, "age": 16}).status_code == 422


def test_real_mode_rejects_local_impersonation(env):
    env[0].settings.transport = "weixin"
    c = client(env)
    assert c.post("/api/chat", json={"content": "hello"}).status_code == 400


def test_hard_pause(env):
    c = client(env)
    assert c.post("/api/pause", json={"paused": True}).status_code == 200
    assert c.get("/api/status").json()["paused"] is True

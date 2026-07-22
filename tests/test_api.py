"""openwebui-lite 后端 API 测试（MOCK_LLM 模式，无需 Ollama）。

运行：pytest openwebui-lite/tests
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["MOCK_LLM"] = "1"  # 离线 mock，不连 Ollama

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def test_models_endpoint():
    c = TestClient(main.app)
    r = c.get("/api/models")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["models"], list)
    assert data["mock"] is True


def test_chat_persists_then_history():
    c = TestClient(main.app)
    c.post("/api/new")  # 开新会话，成为当前会话
    r = c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "你好世界"}]},
    )
    assert r.status_code == 200

    # 历史应持久化包含刚发的 user 消息（重启后前端据此还原）
    h = c.get("/api/history").json()
    msgs = h.get("messages", [])
    assert any(
        m.get("role") == "user" and "你好世界" in (m.get("content") or "")
        for m in msgs
    )


def test_new_clears_history():
    c = TestClient(main.app)
    c.post("/api/new")
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "临时消息"}]},
    )
    c.post("/api/new")  # 再次新会话应清空当前
    h = c.get("/api/history").json()
    assert h.get("messages") == []


def test_health_endpoint():
    c = TestClient(main.app)
    r = c.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("ok", "degraded")
    assert "ollama_base" in data
    assert data["db"] is True  # WAL 后 SQLite 可读


def test_list_sessions_returns_current():
    c = TestClient(main.app)
    c.post("/api/new")  # 至少有一个当前会话
    r = c.get("/api/sessions")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["sessions"], list)
    assert len(data["sessions"]) >= 1
    assert data["current"]
    # 每个会话项含消息数等可观测字段
    sess = data["sessions"][0]
    assert "message_count" in sess and "id" in sess


def test_switch_session_restores_history():
    c = TestClient(main.app)
    first = c.post("/api/new").json()["session_id"]
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "FIRST_MSG"}]},
    )
    c.post("/api/new")  # 切到新会话，离开 first
    c.post(f"/api/sessions/{first}/switch")  # 切回 first
    h = c.get("/api/history").json()
    assert any("FIRST_MSG" in (m.get("content") or "") for m in h.get("messages", []))
    assert c.get("/api/sessions").json()["current"] == first


def test_delete_session_removes_it():
    c = TestClient(main.app)
    c.post("/api/new")  # 当前 A
    other = c.post("/api/new").json()["session_id"]  # 当前切到 B
    assert other
    r = c.delete(f"/api/sessions/{other}")
    assert r.status_code == 200
    ids = [s["id"] for s in c.get("/api/sessions").json()["sessions"]]
    assert other not in ids


def test_session_title_auto_set_and_listed():
    c = TestClient(main.app)
    c.post("/api/new")
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "如何部署模型服务"}]},
    )
    sessions = c.get("/api/sessions").json()["sessions"]
    titles = [s["title"] for s in sessions]
    assert any("如何部署模型服务" in t for t in titles)


def test_get_session_by_id_returns_messages():
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "SESSION_TITLE_MARKER"}]},
    )
    r = c.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == sid
    assert "SESSION_TITLE_MARKER" in data["title"]
    assert any("SESSION_TITLE_MARKER" in (m.get("content") or "") for m in data["messages"])

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


def test_settings_default_empty():
    c = TestClient(main.app)
    # 隔离：清空共享 kv（前面用例可能已写入默认模型）
    main.db_store.set_setting("default_model", "")
    r = c.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["default_model"] == ""


def test_settings_set_and_get():
    c = TestClient(main.app)
    r = c.post("/api/settings", json={"default_model": "qwen2.5"})
    assert r.status_code == 200
    assert r.json()["default_model"] == "qwen2.5"
    assert c.get("/api/settings").json()["default_model"] == "qwen2.5"


def test_chat_persists_default_model():
    c = TestClient(main.app)
    c.post("/api/new")
    c.post(
        "/api/chat",
        json={"model": "my-model", "messages": [{"role": "user", "content": "x"}]},
    )
    assert c.get("/api/settings").json()["default_model"] == "my-model"


def test_frontend_wires_settings():
    """R1 新需求验证：前端应接入默认模型记忆（/api/settings + 保存按钮）。"""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "/api/settings" in html
    assert "saveDefaultBtn" in html
    assert "loadSettings" in html
    assert "saveDefaultModel" in html


def test_rename_session_updates_title():
    """R1 新需求验证：POST /api/sessions/{sid}/rename 应更新标题。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    r = c.post(f"/api/sessions/{sid}/rename", json={"title": "我的自定义标题"})
    assert r.status_code == 200
    assert r.json()["title"] == "我的自定义标题"
    got = c.get(f"/api/sessions/{sid}").json()
    assert got["title"] == "我的自定义标题"


def test_clear_session_keeps_session_but_wipes_messages():
    """R1 新需求验证：POST /api/sessions/{sid}/clear 清空消息且保留会话(标题重置)。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "临时对话内容"}]},
    )
    assert main.db_store.count_messages(sid) >= 1
    r = c.post(f"/api/sessions/{sid}/clear")
    assert r.status_code == 200
    assert r.json()["title"] == "新对话"
    # 会话仍存在，但消息已清空
    ids = [s["id"] for s in c.get("/api/sessions").json()["sessions"]]
    assert sid in ids
    assert main.db_store.count_messages(sid) == 0


def test_export_session_returns_markdown():
    """R1 新需求验证：GET /api/sessions/{sid}/export 返回 Markdown 会话记录。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "导出测试问题"}]},
    )
    r = c.get(f"/api/sessions/{sid}/export")
    assert r.status_code == 200
    md = r.json()["markdown"]
    assert "导出测试问题" in md
    assert md.startswith("#")


def test_session_messages_pagination():
    """R1 新需求验证：GET /api/sessions/{sid}/messages 支持 limit/offset 分页。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    # 模拟前端逐步累积历史：每次发送完整历史（含助手回复）
    hist = []
    for i in range(5):
        hist.append({"role": "user", "content": f"问题{i}"})
        hist.append({"role": "assistant", "content": f"回答{i}"})
        c.post("/api/chat", json={"model": "mock", "messages": list(hist)})
    total = main.db_store.count_messages(sid)
    assert total == 10  # 5 轮 × (用户+助手)
    # 第一页：limit=2 -> 仅前 2 条（按 id 升序）
    r1 = c.get(f"/api/sessions/{sid}/messages?limit=2&offset=0")
    assert r1.status_code == 200
    assert r1.json()["count"] == 2
    assert "问题0" in r1.json()["messages"][0]["content"]
    # 第二页：offset=2 -> 第 3、4 条
    r2 = c.get(f"/api/sessions/{sid}/messages?limit=2&offset=2")
    assert r2.json()["count"] == 2
    assert "问题1" in r2.json()["messages"][0]["content"]
    # 不限：返回全部
    r3 = c.get(f"/api/sessions/{sid}/messages")
    assert r3.json()["count"] == 10


def test_chat_rejects_malformed_messages():
    """R2 隐性健壮性：messages 非法（非 {role,content}）应被校验拒绝，而非 500。"""
    c = TestClient(main.app)
    c.post("/api/new")
    # 字符串混入消息列表 -> Pydantic 校验失败（422 而非 500）
    r = c.post("/api/chat", json={"model": "mock", "messages": ["不是消息对象"]})
    assert r.status_code == 422
    # 合法但缺 content（默认空串）应被接受
    r2 = c.post("/api/chat", json={"model": "mock", "messages": [{"role": "user"}]})
    assert r2.status_code == 200


def test_stats_endpoint():
    """R1 新需求验证：GET /api/stats 返回会话/消息总数与当前会话。"""
    c = TestClient(main.app)
    c.post("/api/new")
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "统计测试"}]},
    )
    r = c.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["sessions"], int) and data["sessions"] >= 1
    assert isinstance(data["messages"], int) and data["messages"] >= 1
    assert data["current"]


def test_list_sessions_message_count_matches():
    """R2 验证：list_sessions 的单查询计数与 count_messages 一致（无 N+1 回归）。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "计数校验"}]},
    )
    sessions = c.get("/api/sessions").json()["sessions"]
    target = next(s for s in sessions if s["id"] == sid)
    assert target["message_count"] == main.db_store.count_messages(sid)

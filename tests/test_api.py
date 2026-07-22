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


def _first_mid(sid):
    conn = main.db_store._conn()
    try:
        return conn.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id LIMIT 1", (sid,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_delete_single_message_and_empty_title_reset():
    """R1 新需求验证：DELETE /api/messages/{mid} 删除单条；
    R2 修复：删空后标题应重置为「新对话」。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    main.db_store.save_messages(
        sid,
        [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
    )
    mid = _first_mid(sid)
    r = c.delete(f"/api/messages/{mid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert main.db_store.count_messages(sid) == 1  # 仅删一条

    mid2 = _first_mid(sid)
    r2 = c.delete(f"/api/messages/{mid2}")
    assert r2.status_code == 200
    assert main.db_store.count_messages(sid) == 0
    assert main.db_store.get_title(sid) == "新对话"  # R2 一致性修复


def test_delete_missing_message_returns_404():
    c = TestClient(main.app)
    r = c.delete("/api/messages/99999999")
    assert r.status_code == 404


def test_get_single_message():
    """R1 新需求验证：GET /api/messages/{mid} 获取单条（供编辑定位）。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    main.db_store.save_messages(sid, [{"role": "user", "content": "fetch me"}])
    mid = _first_mid(sid)
    r = c.get(f"/api/messages/{mid}")
    assert r.status_code == 200
    assert r.json()["content"] == "fetch me"
    assert r.json()["session_id"] == sid


def test_edit_message_updates_content():
    """R1 新需求验证：PUT /api/messages/{mid} 就地修订内容。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    main.db_store.save_messages(
        sid,
        [{"role": "user", "content": "原问题"},
         {"role": "assistant", "content": "原回答"}],
    )
    mid = _first_mid(sid)
    r = c.put(f"/api/messages/{mid}", json={"content": "修订后的问题"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert main.db_store.get_messages(sid)[0]["content"] == "修订后的问题"


def test_edit_first_user_message_synced_title():
    """R2 修复验证：编辑首条 user 消息应同步会话标题。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    main.db_store.save_messages(sid, [{"role": "user", "content": "初始标题来源"}])
    mid = _first_mid(sid)
    c.put(f"/api/messages/{mid}", json={"content": "新的标题来源"})
    assert main.db_store.get_title(sid) == "新的标题来源"


def test_edit_missing_message_404():
    c = TestClient(main.app)
    r = c.put("/api/messages/99999999", json={"content": "x"})
    assert r.status_code == 404

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


def test_search_finds_message_across_sessions():
    """R1 新需求验证：GET /api/search 跨会话检索消息内容。"""
    c = TestClient(main.app)
    c.post("/api/new")
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "SEARCHABLE_KEYWORD_XYZ"}]},
    )
    r = c.get("/api/search?q=SEARCHABLE_KEYWORD_XYZ")
    assert r.status_code == 200
    results = r.json()["results"]
    assert any("SEARCHABLE_KEYWORD_XYZ" in (x.get("content") or "") for x in results)
    assert results[0]["session_id"]


def test_search_empty_query_returns_empty():
    c = TestClient(main.app)
    r = c.get("/api/search?q=")
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_models_endpoint_is_cached():
    """R2 验证：/api/models 命中 5s TTL 缓存，避免重复打 Ollama。"""
    c = TestClient(main.app)
    r = c.get("/api/models")
    assert r.status_code == 200
    # 调用后缓存应被填充，且内容与响应一致
    assert main._MODELS_CACHE["data"] == r.json()["models"]


def test_chat_non_streaming_returns_json():
    """R1 新需求验证：?stream=0 返回完整 JSON 回复（非 SSE），便于 API 消费。"""
    c = TestClient(main.app)
    c.post("/api/new")
    r = c.post(
        "/api/chat?stream=0",
        json={"model": "mock", "messages": [{"role": "user", "content": "非流式你好"}]},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert isinstance(data["reply"], str) and len(data["reply"]) > 0
    assert "非流式你好" in data["reply"]  # 回显用户输入
    assert data["model"] == "mock"


def test_search_escapes_like_wildcards():
    """R2 隐性正确性：搜索词中的 % / _ 应被当字面量，而非 LIKE 通配符。"""
    import urllib.parse

    c = TestClient(main.app)
    c.post("/api/new")
    # 下划线：字面 user_name 应命中，username（无下划线）不应被 _ 通配命中
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [
            {"role": "user", "content": "user_name 应被字面匹配"},
            {"role": "user", "content": "username 不应被下划线通配命中"},
        ]},
    )
    r = c.get("/api/search?q=" + urllib.parse.quote("user_name"))
    contents = [x["content"] for x in r.json()["results"]]
    assert any("user_name" in x for x in contents)
    assert not any("username" in x for x in contents)

    # 百分号：字面 50% 应命中，5000 不应被 % 通配命中
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [
            {"role": "user", "content": "折扣 50% off"},
            {"role": "user", "content": "价格 5000 元"},
        ]},
    )
    r2 = c.get("/api/search?q=" + urllib.parse.quote("50%"))
    contents2 = [x["content"] for x in r2.json()["results"]]
    assert any("50% off" in x for x in contents2)
    assert not any("5000" in x for x in contents2)


def test_new_session_becomes_current():
    """R2 验证：/api/new 之后，新会话必须成为当前会话（chat 写入它而非旧会话）。"""
    import db as db_store

    c = TestClient(main.app)
    old = c.post("/api/new").json()["session_id"]
    # 在旧会话写入内容
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "OLD_SESSION_MARKER"},
    ]})
    assert db_store.count_messages(old) == 1
    # 开新会话
    new_sid = c.post("/api/new").json()["session_id"]
    assert new_sid != old
    # 当前会话指针应已切到新会话
    assert c.get("/api/current").json()["session_id"] == new_sid
    # 新会话此刻为空
    assert db_store.count_messages(new_sid) == 0
    # 后续 chat 写入新会话，旧会话不受影响（仍为 1 条）
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "NEW_SESSION_MARKER"},
    ]})
    assert db_store.count_messages(new_sid) == 1
    assert db_store.count_messages(old) == 1


def test_session_messages_pagination_still_passes():
    """回归：分页测试在新会话切换修复后应通过（此前 10 条断言得 4 条）。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    hist = []
    for i in range(5):
        hist.append({"role": "user", "content": f"问题{i}"})
        hist.append({"role": "assistant", "content": f"回答{i}"})
        c.post("/api/chat", json={"model": "mock", "messages": list(hist)})
    assert main.db_store.count_messages(sid) == 10


def test_fork_session_copies_messages_and_title():
    """R1：克隆会话应复制全部消息与标题，且新旧会话互不影响。"""
    import db as db_store

    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post("/api/sessions/" + sid + "/rename", json={"title": "FORK_SOURCE"})
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "FORK_MARKER_A"},
        {"role": "assistant", "content": "FORK_MARKER_B"},
    ]})
    assert db_store.count_messages(sid) == 2

    r = c.post("/api/sessions/" + sid + "/fork", params={"title": "FORKED"})
    assert r.status_code == 200
    data = r.json()
    new_sid = data["id"]
    assert new_sid != sid
    assert data["title"] == "FORKED"
    # 新会话含原会话全部消息
    assert db_store.count_messages(new_sid) == 2
    new_msgs = db_store.get_messages(new_sid)
    assert any("FORK_MARKER_A" in m["content"] for m in new_msgs)
    # 切到新会话后再追加消息，验证分叉独立性（fork 不自动切换当前会话）
    c.post("/api/sessions/" + new_sid + "/switch")
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "FORK_MARKER_A"},
        {"role": "assistant", "content": "FORK_MARKER_B"},
        {"role": "user", "content": "EXTRA_AFTER_FORK"},
    ]})
    assert db_store.count_messages(new_sid) == 3
    assert db_store.count_messages(sid) == 2  # 源会话未变


def test_fork_unknown_session_returns_404():
    """R1：克隆不存在的会话应返回 404，而非静默创建空会话。"""
    c = TestClient(main.app)
    r = c.post("/api/sessions/does_not_exist/fork")
    assert r.status_code == 404


def test_session_messages_include_id():
    """R2 验证：分页消息接口每条都应带 id，供前端定位/编辑/删除具体消息。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "ID_CHECK_QUESTION"},
        {"role": "assistant", "content": "ID_CHECK_ANSWER"},
    ]})
    r = c.get(f"/api/sessions/{sid}/messages")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 2
    assert all("id" in m for m in msgs)
    assert any(m["content"] == "ID_CHECK_QUESTION" and m["role"] == "user" for m in msgs)


def test_session_messages_role_filter():
    """R1 验证：role=user 只返回用户消息，role=assistant 只返回助手消息。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "ROLE_Q1"},
        {"role": "assistant", "content": "ROLE_A1"},
        {"role": "user", "content": "ROLE_Q2"},
    ]})
    ru = c.get(f"/api/sessions/{sid}/messages?role=user")
    um = ru.json()["messages"]
    assert all(m["role"] == "user" for m in um)
    assert len(um) == 2
    ra = c.get(f"/api/sessions/{sid}/messages?role=assistant")
    am = ra.json()["messages"]
    assert all(m["role"] == "assistant" for m in am)
    assert len(am) == 1
    # 非法 role 被忽略（等价不过滤）
    rbad = c.get(f"/api/sessions/{sid}/messages?role=system")
    assert rbad.json()["count"] == 3

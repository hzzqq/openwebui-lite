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


def test_delete_current_session_returns_new_current():
    """R2 隐性一致性验证：删除当前会话后，接口应返回「删除后的当前会话 id」，
    而非被删会话 id，避免前端把已删会话误认为仍在进行中。"""
    c = TestClient(main.app)
    cur = c.post("/api/new").json()["session_id"]  # 当前 = cur
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "临时问题"}]},
    )
    r = c.delete(f"/api/sessions/{cur}")
    assert r.status_code == 200
    returned = r.json()["session_id"]
    assert returned != cur  # 不应再返回已删会话
    # 返回的 id 应当就是新的当前会话指针
    assert c.get("/api/current").json()["session_id"] == returned


def test_list_sessions_title_filter():
    """R1 新需求验证：?title= 按标题过滤会话列表。"""
    c = TestClient(main.app)
    c.post("/api/new")
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "苹果供应链分析"}]},
    )
    c.post("/api/new")
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "新能源汽车销量"}]},
    )
    # 只筛标题含「苹果」的会话
    r = c.get("/api/sessions", params={"title": "苹果"})
    assert r.status_code == 200
    titles = [s["title"] for s in r.json()["sessions"]]
    assert any("苹果" in t for t in titles)
    assert not any("新能源" in t for t in titles)
    # 无匹配也应返回 200 且为空列表（而非报错）
    r2 = c.get("/api/sessions", params={"title": "不存在的标题xyz"})
    assert r2.status_code == 200
    assert r2.json()["sessions"] == []


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


def test_chat_falls_back_to_session_model():
    """R1 新能力验证：未显式传 model 时，chat 应回退到本会话已存模型。

    R2 修复验证：此前仅保存模型、从不回退，导致不重复传 model 的后续对话
    丢失会话模型记忆（per-session 模型名形同虚设）。
    """
    c = TestClient(main.app)
    # 重置全局默认模型与会话模型，保证回退链末端得到确定值（mock 模式返回 'mock'）
    main.db_store.set_setting("default_model", "")
    c.post("/api/new")
    r = c.post(
        "/api/chat?stream=0",
        json={"messages": [{"role": "user", "content": "用会话模型回复"}]},
    )
    assert r.status_code == 200
    assert r.json()["model"] == "mock"  # 无显式/会话/全局模型时，mock 模式回退 'mock'


def test_chat_falls_back_to_stored_session_model():
    """R1 验证：为会话 set_model 后，不传 model 的 chat 使用会话记忆的模型。"""
    c = TestClient(main.app)
    main.db_store.set_setting("default_model", "")  # 排除全局默认干扰，专测会话级回退
    sid = c.post("/api/new").json()["session_id"]
    main.db_store.set_model(sid, "session-model-x")
    r = c.post(
        "/api/chat?stream=0",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.json()["model"] == "session-model-x"


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


def test_export_session_returns_json():
    """R1 新需求验证：GET /api/sessions/{sid}/export?format=json 返回结构化消息列表。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "导出JSON问题"}]},
    )
    r = c.get(f"/api/sessions/{sid}/export?format=json")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["id"] == sid
    assert isinstance(body["messages"], list) and len(body["messages"]) >= 1
    assert any(m.get("role") == "user" and "导出JSON问题" in m.get("content", "")
                   for m in body["messages"])


def test_export_session_missing_404():
    """R2 一致性：不存在会话导出仍 404（与 get_session 对齐）。"""
    c = TestClient(main.app)
    r = c.get("/api/sessions/nope/export?format=json")
    assert r.status_code == 404


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


def test_search_results_include_id():
    """R2 修复验证：全局检索结果应含消息 id，便于前端定位/跳转/编辑。"""
    c = TestClient(main.app)
    c.post("/api/new")
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "SEARCHABLE_ID_MARKER"}]},
    )
    results = c.get("/api/search?q=SEARCHABLE_ID_MARKER").json()["results"]
    assert results
    assert "id" in results[0] and isinstance(results[0]["id"], int)


def test_search_role_filter_isolates_assistant():
    """R1 验证：?role=assistant 只返回助手消息，与单会话 role 过滤对称。"""
    c = TestClient(main.app)
    c.post("/api/new")
    # 发送一条 user 消息（mock 模式也只存前端传入的历史）
    c.post(
        "/api/chat",
        json={
            "model": "mock",
            "messages": [
                {"role": "user", "content": "ROLE_FILTER_KEYWORD"},
                {"role": "assistant", "content": "ROLE_FILTER_KEYWORD 的回答"},
            ],
        },
    )
    all_hits = c.get("/api/search?q=ROLE_FILTER_KEYWORD").json()["results"]
    assert all_hits  # 命中两条（user + assistant）
    asst = c.get("/api/search?q=ROLE_FILTER_KEYWORD&role=assistant").json()["results"]
    users = c.get("/api/search?q=ROLE_FILTER_KEYWORD&role=user").json()["results"]
    assert asst and all(x["role"] == "assistant" for x in asst)
    assert users and all(x["role"] == "user" for x in users)
    # assistant 结果不应包含 user 那条，反之亦然
    assert len(asst) < len(all_hits)


def test_search_invalid_role_ignored():
    """非法 role 值应被忽略（等价不过滤），不返回 400。"""
    c = TestClient(main.app)
    c.post("/api/new")
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "ROLE_IGNORE_KEYWORD"}]},
    )
    r = c.get("/api/search?q=ROLE_IGNORE_KEYWORD&role=bot")
    assert r.status_code == 200
    assert r.json()["results"]  # 仍按内容命中，未因非法 role 而清空


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


def test_session_messages_returns_total_for_pagination():
    """R1 验证：/api/sessions/{sid}/messages 返回 total（忽略分页的过滤总数）。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "TOTAL_Q1"},
        {"role": "assistant", "content": "TOTAL_A1"},
        {"role": "user", "content": "TOTAL_Q2"},
        {"role": "assistant", "content": "TOTAL_A2"},
    ]})
    r = c.get(f"/api/sessions/{sid}/messages?limit=2&offset=0")
    body = r.json()
    assert body["total"] == 4          # 过滤后共 4 条
    assert body["count"] == 2         # 当页 2 条
    # role 过滤下 total 也随之收窄
    ru = c.get(f"/api/sessions/{sid}/messages?role=user")
    assert ru.json()["total"] == 2


def test_title_rederives_after_clear():
    """R2 修复验证：会话清空后（标题回到哨兵「新对话」），再开聊应能
    根据首条用户消息重新派生真实标题，而不是卡在「新对话」。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "原始标题来源内容"},
    ]})
    assert "原始标题来源内容" in main.db_store.get_title(sid)
    # 清空会话（标题重置为哨兵「新对话」）
    c.post(f"/api/sessions/{sid}/clear")
    assert main.db_store.get_title(sid) == "新对话"
    # 再发送一条新用户消息，标题应重新派生而非停留「新对话」
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "重新派生标题内容"},
    ]})
    new_title = main.db_store.get_title(sid)
    assert new_title != "新对话"
    assert "重新派生标题内容" in new_title


def test_sessions_cleanup_keeps_recent_and_current():
    """R1 验证：批量清理只删最旧的、保留最近 keep 个，且当前会话永不被删。
    对「运行内已有其他会话」做鲁棒处理：用清理前后差值而非绝对计数。"""
    import db as db_store

    c = TestClient(main.app)
    before = len(db_store.list_sessions())
    # 造 5 个新会话（每个聊一句），最新建立的即为当前会话
    sids = []
    for i in range(5):
        r = c.post("/api/new")
        sids.append(r.json()["session_id"])
        c.post("/api/chat", json={"model": "mock",
                                  "messages": [{"role": "user", "content": f"会话 {i}"}]})
    mid = len(db_store.list_sessions())
    assert mid == before + 5  # 确实新增了 5 个
    cur = c.get("/api/current").json()["session_id"]
    assert cur == sids[-1]
    # 保留最近 2 个：删除其余（含运行内既有旧会话）
    r = c.post("/api/sessions/cleanup", params={"keep": 2})
    assert r.status_code == 200
    data = r.json()
    after = db_store.list_sessions()
    after_ids = {s["id"] for s in after}
    # 当前会话必须仍存在（清理永不删除当前会话）
    assert cur in after_ids
    # 正常保留最近 keep=2；若当前会话因 created 同秒并列未进入「最近 2 个」，
    # 会被额外保留为第 3 个（设计如此：当前指针永不被清理悬空）
    assert len(after) in (2, 3)
    assert data["removed"] == mid - len(after)


def test_chat_nonstream_ollama_no_crash_on_done_event(monkeypatch):
    """R2 验证：非流式真实后端（_ollama_stream）末尾的 done 事件 data 为对象，
    旧实现直接把该对象 append 进 parts 再 ''.join 会抛 TypeError -> 500；
    修复后只对字符串 token 累加，能正常返回完整回复。"""
    import json as _json

    c = TestClient(main.app)
    c.post("/api/new")

    async def fake_ollama(model, messages, **kwargs):
        yield main._sse("token", _json.dumps("你好", ensure_ascii=False))
        yield main._sse("token", _json.dumps("世界", ensure_ascii=False))
        yield main._sse("done", _json.dumps({"ok": True}, ensure_ascii=False))

    monkeypatch.setattr(main, "MOCK_LLM", False)
    monkeypatch.setattr(main, "_ollama_stream", fake_ollama)
    r = c.post(
        "/api/chat?stream=0",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["reply"] == "你好世界"


def test_session_messages_search():
    """R1：会话内消息支持 q 子串检索（LIKE 转义），仅返回命中内容。

    注意：openwebui 每次 chat 都会以「完整历史」覆盖式保存会话消息，
    因此本测试在单次 chat 中携带全部消息（模拟前端每次发送完整对话）。
    """
    c = TestClient(main.app)
    r = c.post("/api/new")
    sid = r.json()["session_id"]
    # 一次 chat 携带完整历史（含苹果 / 天气两条 user 消息）
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "苹果公司发布了新产品"},
        {"role": "assistant", "content": "已记录该消息"},
        {"role": "user", "content": "今天天气晴朗"},
    ]})

    r1 = c.get(f"/api/sessions/{sid}/messages", params={"q": "苹果"})
    assert r1.status_code == 200
    m1 = r1.json()["messages"]
    assert len(m1) >= 1
    assert all("苹果" in (m["content"] or "") for m in m1)
    assert r1.json()["q"] == "苹果"

    r2 = c.get(f"/api/sessions/{sid}/messages", params={"q": "天气"})
    m2 = r2.json()["messages"]
    assert all("天气" in (m["content"] or "") for m in m2)


def test_session_messages_search_escapes_wildcard():
    """R2：q 中的 LIKE 通配符 %/_ 按字面量匹配，不会误命中。"""
    c = TestClient(main.app)
    r = c.post("/api/new")
    sid = r.json()["session_id"]
    c.post("/api/chat", json={"model": "mock", "messages": [
        {"role": "user", "content": "完成度 50% 的进度"},
    ]})
    # 搜索字面量 "50%"：若未转义，% 会被当成通配符导致误命中任意消息
    r1 = c.get(f"/api/sessions/{sid}/messages", params={"q": "50%"})
    assert r1.status_code == 200
    assert len(r1.json()["messages"]) == 1
    assert "50%" in r1.json()["messages"][0]["content"]


def test_chat_rejects_invalid_role():
    """R1 验证：role 收口为枚举后，畸形 role(如 'bot')应在边界 422 而非静默落库。"""
    c = TestClient(main.app)
    c.post("/api/new")
    r = c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "bot", "content": "你好"}]},
    )
    assert r.status_code == 422
    # 合法枚举仍正常
    r2 = c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "合法角色"}]},
    )
    assert r2.status_code == 200


def test_chat_passes_generation_params(monkeypatch):
    """R1 验证：temperature/max_tokens 经 ChatRequest 透传到 Ollama options 载荷。"""
    import json as _json

    c = TestClient(main.app)
    c.post("/api/new")

    captured = {}

    async def fake_ollama(model, messages, temperature=None, max_tokens=None, top_p=None):
        captured["options"] = {"temperature": temperature, "max_tokens": max_tokens, "top_p": top_p}
        yield main._sse("token", _json.dumps("ok", ensure_ascii=False))
        yield main._sse("done", _json.dumps({"ok": True}, ensure_ascii=False))

    monkeypatch.setattr(main, "MOCK_LLM", False)
    monkeypatch.setattr(main, "_ollama_stream", fake_ollama)
    r = c.post(
        "/api/chat?stream=0",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "max_tokens": 256,
        },
    )
    assert r.status_code == 200
    assert captured["options"]["temperature"] == 0.7
    assert captured["options"]["max_tokens"] == 256
    assert captured["options"]["top_p"] is None  # 未传 top_p -> 不应注入


def test_chat_omits_default_generation_params(monkeypatch):
    """R1 验证：未传生成参数时，不向 Ollama 注入空 options（沿用模型默认）。"""
    import json as _json

    c = TestClient(main.app)
    c.post("/api/new")

    captured = {}

    async def fake_ollama(model, messages, temperature=None, max_tokens=None, top_p=None):
        captured["payload_has_options"] = (
            temperature is not None or max_tokens is not None or top_p is not None
        )
        yield main._sse("token", _json.dumps("ok", ensure_ascii=False))
        yield main._sse("done", _json.dumps({"ok": True}, ensure_ascii=False))

    monkeypatch.setattr(main, "MOCK_LLM", False)
    monkeypatch.setattr(main, "_ollama_stream", fake_ollama)
    r = c.post(
        "/api/chat?stream=0",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert captured["payload_has_options"] is False


def test_save_messages_caps_content():
    """R2 验证：save_messages 与 update_message 一致，content 超 100000 截断。"""
    import db as db_store

    sid = db_store.new_session()
    huge = "A" * 250000
    db_store.save_messages(sid, [{"role": "user", "content": huge}])
    stored = db_store.get_messages(sid)[0]["content"]
    assert len(stored) == 100000
    # 正常长度不受影响
    db_store.save_messages(sid, [{"role": "user", "content": "短内容"}])
    assert db_store.get_messages(sid)[0]["content"] == "短内容"


def test_list_sessions_respects_limit():
    """R1 验证：GET /api/sessions?limit=N 只返回最近 N 个会话。"""
    import db as db_store

    c = TestClient(main.app)
    before = len(db_store.list_sessions())
    # 造 5 个新会话
    for i in range(5):
        c.post("/api/new")
        c.post("/api/chat", json={"model": "mock",
                                  "messages": [{"role": "user", "content": f"会话 {i}"}]})
    total = len(db_store.list_sessions())
    # limit=3 -> 至多 3 个
    r = c.get("/api/sessions", params={"limit": 3})
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 3
    assert total == before + 5  # 全量未受影响


def test_list_sessions_title_and_limit_combined():
    """R2 验证：?title= 与 ?limit= 同时给出时，limit 应被透传（不再被丢弃）。"""
    import db as db_store

    c = TestClient(main.app)
    c.post("/api/new")
    c.post("/api/chat", json={"model": "mock",
                              "messages": [{"role": "user", "content": "组合过滤苹果专题"}]})
    c.post("/api/new")
    c.post("/api/chat", json={"model": "mock",
                              "messages": [{"role": "user", "content": "组合过滤天气专题"}]})
    # 标题命中「苹果」且 limit=1 -> 仅 1 条
    r = c.get("/api/sessions", params={"title": "苹果", "limit": 1})
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert any("苹果" in s["title"] for s in sessions)


def test_append_message_to_session():
    """R1 新需求验证：POST /api/sessions/{sid}/messages 单条追加并自动派生标题。"""
    c = TestClient(main.app)
    c.post("/api/new")
    cur = c.get("/api/current").json()["session_id"]
    r = c.post(f"/api/sessions/{cur}/messages",
               json={"role": "user", "content": "通过 API 注入的问题"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["role"] == "user"
    assert body.get("id")
    # 与 chat 一致的自动标题：首条 user 消息应派生会话标题
    sess = c.get(f"/api/sessions/{cur}").json()
    assert "通过 API 注入的问题" in sess["title"]
    # 消息确实落库
    msgs = c.get(f"/api/sessions/{cur}/messages").json()["messages"]
    assert any(m["role"] == "user" and "通过 API 注入的问题" in m["content"] for m in msgs)


def test_append_message_invalid_role_422():
    """R2 验证：畸形 role 在边界即 422，不被静默落库。"""
    c = TestClient(main.app)
    c.post("/api/new")
    cur = c.get("/api/current").json()["session_id"]
    r = c.post(f"/api/sessions/{cur}/messages", json={"role": "bot", "content": "x"})
    assert r.status_code == 422


def test_append_message_missing_session_404():
    """R2 验证：会话不存在返回 404，而非静默创建/空成功。"""
    c = TestClient(main.app)
    r = c.post("/api/sessions/nonexistent_sid/messages",
               json={"role": "user", "content": "x"})
    assert r.status_code == 404


def test_get_session_detail_metadata_and_404():
    """R1 验证：GET /api/sessions/{sid} 返回 model/created/message_count 概要；
    会话不存在返回 404（而非幽灵空列表）。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    c.post(
        "/api/chat",
        json={"model": "mock", "messages": [{"role": "user", "content": "DETAIL_MARKER"}]},
    )
    r = c.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == sid
    assert "DETAIL_MARKER" in d["title"]
    assert "model" in d and "created" in d and "message_count" in d
    assert d["message_count"] == 1
    assert any("DETAIL_MARKER" in (m.get("content") or "") for m in d["messages"])
    # 缺失会话 -> 404
    r2 = c.get("/api/sessions/does_not_exist_id")
    assert r2.status_code == 404


def test_get_messages_offset_without_limit():
    """R2 验证：offset 在 limit 未设置时必须生效（此前被静默丢弃）。"""
    c = TestClient(main.app)
    sid = c.post("/api/new").json()["session_id"]
    hist = []
    for i in range(4):
        hist.append({"role": "user", "content": f"Q{i}"})
        hist.append({"role": "assistant", "content": f"A{i}"})
        c.post("/api/chat", json={"model": "mock", "messages": list(hist)})
    # 不限 limit，仅 offset=4 -> 应跳过前 4 条（按 id 升序）
    r = c.get(f"/api/sessions/{sid}/messages?offset=4")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 4  # 共 8 条，跳过 4 条剩 4 条
    # offset 生效：跳过前 4 条(Q0/A0/Q1/A1)，首条应为 Q2
    assert "Q2" in msgs[0]["content"]


def test_db_get_session_detail_none_for_missing():
    """R1 回归：db.get_session_detail 对缺失会话返回 None。"""
    assert main.db_store.get_session_detail("no_such_session") is None


def test_chat_passes_top_p_to_ollama(monkeypatch):
    """R1 验证：top_p 核采样参数经 ChatRequest 透传到 Ollama options 载荷。"""
    import json as _json

    c = TestClient(main.app)
    c.post("/api/new")

    captured = {}

    async def fake_ollama(model, messages, temperature=None, max_tokens=None, top_p=None):
        captured["top_p"] = top_p
        yield main._sse("token", _json.dumps("ok", ensure_ascii=False))
        yield main._sse("done", _json.dumps({"ok": True}, ensure_ascii=False))

    monkeypatch.setattr(main, "MOCK_LLM", False)
    monkeypatch.setattr(main, "_ollama_stream", fake_ollama)
    r = c.post(
        "/api/chat?stream=0",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "hi"}],
            "top_p": 0.9,
        },
    )
    assert r.status_code == 200
    assert captured["top_p"] == 0.9


def test_chat_omits_default_top_p(monkeypatch):
    """R1 验证：未传 top_p 时，不向 Ollama 注入 top_p（沿用模型默认）。"""
    import json as _json

    c = TestClient(main.app)
    c.post("/api/new")

    captured = {}

    async def fake_ollama(model, messages, temperature=None, max_tokens=None, top_p=None):
        captured["top_p_seen"] = top_p is not None
        yield main._sse("token", _json.dumps("ok", ensure_ascii=False))
        yield main._sse("done", _json.dumps({"ok": True}, ensure_ascii=False))

    monkeypatch.setattr(main, "MOCK_LLM", False)
    monkeypatch.setattr(main, "_ollama_stream", fake_ollama)
    r = c.post(
        "/api/chat?stream=0",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert captured["top_p_seen"] is False


def test_export_missing_session_returns_404():
    """R2 修复验证：导出不存在的会话应返回 404，而非 200 空 Markdown。"""
    c = TestClient(main.app)
    r = c.get("/api/sessions/no_such_sid/export")
    assert r.status_code == 404


def test_regenerate_replaces_last_assistant_reply():
    """R1 新需求验证：regenerate 应替换末尾助手回复，不重复堆叠。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    main.db_store.save_messages(sid, [
        {"role": "user", "content": "初次提问"},
        {"role": "assistant", "content": "旧回答"},
    ])
    r = c.post(f"/api/sessions/{sid}/regenerate")
    assert r.status_code == 200
    # 流结束后再读：应为 user + 新 assistant，且不应有两条 assistant
    msgs = main.db_store.get_messages(sid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert "旧回答" not in msgs[1]["content"]
    # 新助手回复为 mock 内容（含用户输入回声）
    assert "初次提问" in msgs[1]["content"]


def test_regenerate_without_existing_assistant_appends():
    """R1：末尾是用户消息时，regenerate 应补一条新助手回复而非重复。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    main.db_store.save_messages(sid, [{"role": "user", "content": "只有问题"}])
    r = c.post(f"/api/sessions/{sid}/regenerate")
    assert r.status_code == 200
    msgs = main.db_store.get_messages(sid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_regenerate_empty_session_returns_400():
    """R1：空会话无法重新生成，返回 400。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    r = c.post(f"/api/sessions/{sid}/regenerate")
    assert r.status_code == 400


def test_delete_title_source_rederives_title():
    """R2 修复验证：删除作为标题来源的首条 user 消息后，标题应重新派生。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    main.db_store.save_messages(sid, [
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "回答一"},
        {"role": "user", "content": "第二个问题"},
    ])
    # 模拟 chat/append 自动派生的标题
    main.db_store.set_title(sid, "第一个问题")
    mid = main.db_store.get_messages(sid)[0]["id"]
    r = c.delete(f"/api/messages/{mid}")
    assert r.status_code == 200
    # 标题应重新派生自新的首条 user 消息「第二个问题」
    assert main.db_store.get_title(sid) == "第二个问题"


def test_delete_first_user_with_custom_title_preserved():
    """R2：自定义标题（rename）在删除首条 user 消息后不被覆盖。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    main.db_store.save_messages(sid, [
        {"role": "user", "content": "第一个问题"},
        {"role": "user", "content": "第二个问题"},
    ])
    main.db_store.set_title(sid, "我的自定义标题")
    mid = main.db_store.get_messages(sid)[0]["id"]
    c.delete(f"/api/messages/{mid}")
    assert main.db_store.get_title(sid) == "我的自定义标题"


def test_backup_includes_empty_session():
    """R1：新创建的空会话也应出现在备份中（message_count=0, messages=[]）。

    注：测试库在整个 pytest 运行中共享，不能假设全局为空，故改为校验
    「刚新建的空会话是否被完整包含在备份里」。
    """
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    r = c.get("/api/backup")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    found = [s for s in data["sessions"] if s["id"] == sid]
    assert found, "新建空会话应出现在备份中"
    assert found[0]["message_count"] == 0
    assert found[0]["messages"] == []


def test_backup_includes_sessions_and_messages():
    """R1：备份应含全部会话及其完整消息（role/content 保留）。"""
    c = TestClient(main.app)
    marker_a = f"备份会话A_{__import__('uuid').uuid4().hex[:8]}"
    marker_b = f"备份会话B_{__import__('uuid').uuid4().hex[:8]}"
    c.post("/api/new")
    c.post("/api/chat", json={"model": "mock",
                              "messages": [{"role": "user", "content": marker_a}]})
    c.post("/api/new")
    c.post("/api/chat", json={"model": "mock",
                              "messages": [{"role": "user", "content": marker_b}]})

    r = c.get("/api/backup")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 2
    by_title = {s["title"]: s for s in data["sessions"]}
    assert marker_a in by_title and marker_b in by_title
    a = by_title[marker_a]
    assert a["message_count"] >= 1
    assert any(m["role"] == "user" and marker_a in (m["content"] or "")
               for m in a["messages"])
    assert "model" in a and "created" in a


def test_append_title_after_assistant_first():
    """R2 修复验证：首条为 assistant 时，后续 user 消息仍应触发自动标题派生。"""
    c = TestClient(main.app)
    sid = main.db_store.new_session()
    main.db_store.append_message(sid, "assistant", "bot greeting")
    main.db_store.append_message(sid, "user", "我的第一个真实问题")
    # 修复前标题停留在空/「新对话」；修复后派生自首条 user 消息
    assert main.db_store.get_title(sid) == "我的第一个真实问题"


def test_api_version():
    """R1 验证：/api/version 暴露应用名与版本，便于前端展示与流水线断言。"""
    c = TestClient(main.app)
    r = c.get("/api/version")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "OpenWebUI Lite"
    assert data["version"] == "0.2.0"

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

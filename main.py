"""
OpenWebUI Lite — 对接本地 Ollama 的轻量 LLM 聊天前端 MVP

后端：FastAPI
- GET  /              托管单页前端 (static/index.html)
- GET  /api/models    返回可用模型列表（拉取 Ollama /api/tags；失败则回退默认）
- POST /api/chat      接收 {model, messages}，流式 SSE 转发 Ollama /api/chat
- GET  /api/history   返回当前会话历史（多轮 messages）
- POST /api/new       开启新对话（持久化层新建会话）
- POST /api/clear     清空历史（同 new）

会话持久化：使用 SQLite（db.py），进程重启后历史不丢失。

离线演示：设置环境变量 MOCK_LLM=1 时，不连 Ollama，
直接以 SSE 分片返回一段预设的中文流式文本。
"""

import json
import os
import time
from typing import Dict, List

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

import db as db_store  # SQLite 会话持久化

# ---------- 配置 ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_INDEX = os.path.join(BASE_DIR, "static", "index.html")
OLLAMA_BASE = os.getenv("OLLAMA_HOST", "http://localhost:11434")
MOCK_LLM = os.getenv("MOCK_LLM", "0") == "1"

app = FastAPI(title="OpenWebUI Lite", version="0.2.0")

# 启动时初始化 SQLite（表结构幂等）
db_store.init()


# ---------- 当前会话辅助 ----------
def _current_sid() -> str:
    return db_store.get_or_create_current()


def _load_session() -> Dict:
    sid = _current_sid()
    return {
        "id": sid,
        "model": db_store.get_model(sid),
        "messages": db_store.get_messages(sid),
    }


# ---------- 默认模型（Ollama 连不上时使用） ----------
DEFAULT_MODELS = ["llama3", "qwen2", "gemma2", "mistral"]


async def _fetch_models() -> List[str]:
    """拉取 Ollama 可用模型，失败回退默认列表。"""
    if MOCK_LLM:
        return ["mock-model (离线演示)"] + DEFAULT_MODELS
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{OLLAMA_BASE}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("name") for m in data.get("models", []) if m.get("name")]
                if models:
                    return models
    except Exception:
        pass
    return DEFAULT_MODELS


# ---------- SSE 工具 ----------
def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


async def _mock_stream(user_msg: str) -> str:
    """离线演示：基于用户输入生成一段预设中文流式文本，分片用 SSE 推送。"""
    text = (
        f"【离线演示模式】你刚才说：{user_msg}\n\n"
        "这是一段由 MOCK_LLM 注入的预设回复。当前环境未连接 Ollama，"
        "但流式渲染、SSE 分片、多轮历史、模型选择等交互均已就绪。\n\n"
        "待你本地启动 Ollama（例如 `ollama run qwen2`）并取消 MOCK_LLM 后，"
        "这里就会替换为真实模型的逐字输出。\n\n"
        "提示：点击右上角「新对话」可清空上下文，重新开始一轮会话；"
        "由于已接入 SQLite，历史在重启服务后依然保留。"
    )
    # 逐字分片，模拟真实 token 流
    for ch in text:
        yield _sse("token", json.dumps(ch, ensure_ascii=False))
        time.sleep(0.012)
    yield _sse("done", json.dumps({"ok": True}, ensure_ascii=False))


async def _ollama_stream(model: str, messages: List[Dict]) -> str:
    """转发到 Ollama /api/chat（stream=true），增量 token 推给前端。"""
    payload = {"model": model, "messages": messages, "stream": True}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", f"{OLLAMA_BASE}/api/chat", json=payload
            ) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    yield _sse("error", json.dumps(f"Ollama 返回 {resp.status_code}: {err.decode('utf-8', 'ignore')}", ensure_ascii=False))
                    return
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        yield _sse("token", json.dumps(token, ensure_ascii=False))
                    if chunk.get("done"):
                        yield _sse("done", json.dumps({"ok": True}, ensure_ascii=False))
                        return
    except Exception as e:
        yield _sse("error", json.dumps(f"连接 Ollama 失败：{e}", ensure_ascii=False))


# ---------- 路由 ----------
@app.get("/", response_class=HTMLResponse)
async def index():
    if os.path.exists(STATIC_INDEX):
        return FileResponse(STATIC_INDEX)
    return HTMLResponse("<h1>static/index.html 未找到</h1>", status_code=404)


class MessageItem(BaseModel):
    """单条消息（输入校验，避免裸 dict 导致保存时 AttributeError 500）。"""
    role: str
    content: str = ""


class ChatRequest(BaseModel):
    """聊天请求体（输入校验，避免裸 JSON 解析导致 500）。"""
    model: str = ""
    messages: List[MessageItem] = Field(default_factory=list)


class SettingsRequest(BaseModel):
    """设置请求体（当前支持默认模型记忆）。"""
    default_model: str = ""


class RenameRequest(BaseModel):
    """会话重命名请求体。"""
    title: str


@app.get("/api/models")
async def models():
    return {"models": await _fetch_models(), "mock": MOCK_LLM}


@app.get("/api/settings")
async def get_settings():
    """读取通用设置（如跨会话默认模型）。"""
    return {"default_model": db_store.get_setting("default_model", "")}


@app.post("/api/settings")
async def post_settings(req: SettingsRequest):
    """写入通用设置（如默认模型），返回更新后的值。"""
    if req.default_model:
        db_store.set_setting("default_model", req.default_model)
    return {"ok": True, "default_model": db_store.get_setting("default_model", "")}


@app.get("/api/health")
async def health():
    """探活/可观测端点：供监控或反向代理健康检查调用。"""
    db_ok = False
    try:
        conn = db_store._conn()
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "mock": MOCK_LLM,
        "ollama_base": OLLAMA_BASE,
        "db": db_ok,
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    model = req.model
    # 还原为内部使用的 dict 列表（已通过 Pydantic 校验，项为合法 {role, content}）
    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    sid = _current_sid()
    if model:
        db_store.set_model(sid, model)
        db_store.set_setting("default_model", model)  # 记忆默认模型（跨会话）
    # 每次前端传完整历史，整体落盘
    if messages:
        db_store.save_messages(sid, messages)
        # 自动标题：首条用户消息 -> 会话标题（可观测性 + 多会话可读性）
        if not db_store.get_title(sid):
            for m in messages:
                if m.get("role") == "user" and m.get("content"):
                    title = m["content"].strip().replace("\n", " ")[:40]
                    if title:
                        db_store.set_title(sid, title)
                    break

    async def event_gen():
        if MOCK_LLM:
            user_text = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    user_text = m.get("content", "")
                    break
            async for chunk in _mock_stream(user_text):
                yield chunk
        else:
            if not model:
                yield _sse("error", json.dumps("请先选择或输入模型名称", ensure_ascii=False))
                return
            async for chunk in _ollama_stream(model, messages):
                yield chunk

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/sessions")
async def list_sessions_ep():
    """多会话管理：列出全部会话及当前会话 id。"""
    return {"sessions": db_store.list_sessions(), "current": _current_sid()}


@app.get("/api/sessions/{sid}")
async def get_session_ep(sid: str):
    """获取单个会话的标题与完整消息（用于「打开历史会话」）。"""
    return {
        "id": sid,
        "title": db_store.get_title(sid),
        "messages": db_store.get_messages(sid),
    }


@app.post("/api/sessions/{sid}/switch")
async def switch_session_ep(sid: str):
    """切换到指定会话（用于「打开历史会话」）。"""
    db_store.switch_session(sid)
    return {"ok": True, "session_id": sid}


@app.delete("/api/sessions/{sid}")
async def delete_session_ep(sid: str):
    """删除指定会话（避免旧会话无限堆积）。"""
    db_store.delete_session(sid)
    return {"ok": True, "session_id": sid}


@app.post("/api/sessions/{sid}/rename")
async def rename_session_ep(sid: str, req: RenameRequest):
    """重命名会话（修正自动标题，提升多会话可读性）。"""
    title = (req.title or "").strip()[:200]
    db_store.set_title(sid, title)
    return {"ok": True, "id": sid, "title": title}


@app.post("/api/sessions/{sid}/clear")
async def clear_session_ep(sid: str):
    """清空会话消息但保留会话本身（重置为新一轮对话）。"""
    db_store.clear_messages(sid)
    return {"ok": True, "id": sid, "title": "新对话"}


@app.get("/api/sessions/{sid}/export")
async def export_session_ep(sid: str):
    """将会话导出为 Markdown 文本（便于存档 / 分享），原样返回消息流转。"""
    msgs = db_store.get_messages(sid)
    title = db_store.get_title(sid) or "对话记录"
    lines = [f"# {title}", ""]
    for m in msgs:
        role = m.get("role", "")
        label = "用户" if role == "user" else ("助手" if role == "assistant" else role)
        lines.append(f"**{label}：**")
        lines.append(m.get("content", ""))
        lines.append("")
    return {"ok": True, "id": sid, "title": title, "markdown": "\n".join(lines)}


@app.get("/api/sessions/{sid}/messages")
async def session_messages_ep(sid: str, limit: int = 0, offset: int = 0):
    """分页返回会话消息（limit<=0 表示不限），便于超长会话按需加载。"""
    limit = limit if limit and limit > 0 else None
    msgs = db_store.get_messages(sid, limit=limit, offset=max(0, offset))
    return {"ok": True, "id": sid, "messages": msgs,
            "count": len(msgs), "limit": limit, "offset": max(0, offset)}


@app.get("/api/history")
async def history():
    sess = _load_session()
    return {"session_id": sess["id"], "messages": sess["messages"], "model": sess["model"]}


@app.post("/api/new")
@app.post("/api/clear")
async def new_session():
    sid = db_store.new_session()
    return {"ok": True, "session_id": sid}


# ---------- 启动说明 ----------
if __name__ == "__main__":
    import uvicorn

    print("OpenWebUI Lite 启动中…")
    print(f"  MOCK_LLM = {MOCK_LLM}")
    print(f"  OLLAMA_HOST = {OLLAMA_BASE}")
    uvicorn.run(app, host="0.0.0.0", port=8000)

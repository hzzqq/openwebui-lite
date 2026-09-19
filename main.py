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
import logging
import os
import time
from typing import Dict, List, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import db as db_store  # SQLite 会话持久化
from log_utils import setup_logging

log = logging.getLogger("openwebui")

# ---------- 配置 ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_INDEX = os.path.join(BASE_DIR, "static", "index.html")
OLLAMA_BASE = os.getenv("OLLAMA_HOST", "http://localhost:11434")
MOCK_LLM = os.getenv("MOCK_LLM", "0") == "1"

# R1 新能力：诊断日志（访问/错误）写 stderr 或 OPENWEBUI_LOG_FILE，不污染 stdout。
# 级别由 OPENWEBUI_LOG_LEVEL 控制（DEBUG/INFO/WARNING/ERROR），默认 INFO。
try:
    setup_logging(
        os.getenv("OPENWEBUI_LOG_LEVEL", "INFO"),
        os.getenv("OPENWEBUI_LOG_FILE"),
    )
except Exception:
    pass

app = FastAPI(title="OpenWebUI Lite", version="0.2.0")

# R1 新能力：可配置 CORS，便于外部工具 / 不同端口的前端跨域调用本地 LLM。
# 由 OPENWEBUI_CORS_ORIGINS 控制（逗号分隔；默认 "*" 允许全部，适合本地开发）；
# 设为具体源可收紧。凭据仅在非通配时才开启（通配 + 凭据在 CORS 规范下无效）。
_CORS_ENV = os.getenv("OPENWEBUI_CORS_ORIGINS", "*")
_CORS_ORIGINS = [o.strip() for o in _CORS_ENV.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials="*" not in _CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """R1 新能力：请求级访问日志（方法 + 路径 + 状态码 + 耗时），便于线上排查慢请求/异常。"""
    start = time.time()
    response = await call_next(request)
    elapsed_ms = round((time.time() - start) * 1000, 1)
    log.info("%s %s -> %d (%sms)", request.method, request.url.path, response.status_code, elapsed_ms)
    return response


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

# 模型列表短缓存（5s）：避免每次 /api/models 都打 Ollama，缓解性能悬崖
_MODELS_CACHE: "dict" = {"ts": 0.0, "data": None}
MODELS_CACHE_TTL = 5.0


async def _fetch_models() -> List[str]:
    """拉取 Ollama 可用模型，失败回退默认列表。

    隐性性能：原实现每次调用都实时请求 Ollama，前端轮询/多标签页会反复打上游。
    这里加 5s TTL 缓存，命中则直接返回，显著降低对 Ollama 的压力。
    """
    now = time.time()
    if _MODELS_CACHE["data"] is not None and now - _MODELS_CACHE["ts"] < MODELS_CACHE_TTL:
        return _MODELS_CACHE["data"]
    models: List[str]
    if MOCK_LLM:
        models = ["mock-model (离线演示)"] + DEFAULT_MODELS
    else:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{OLLAMA_BASE}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    models = [m.get("name") for m in data.get("models", []) if m.get("name")]
                    if models:
                        _MODELS_CACHE.update(ts=now, data=models)
                        return models
        except Exception:
            pass
        models = DEFAULT_MODELS
    _MODELS_CACHE.update(ts=now, data=models)
    return models


# ---------- SSE 工具 ----------
def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


async def _collect_sse_text(gen) -> str:
    """把 SSE 事件流还原为纯文本（非流式模式复用同一套生成器）。

    事件形如 "event: token\ndata: \"...\"\n\n"；token 事件 data 为字符串，
    done/error 事件 data 为对象（忽略其文本）。用于 stream=0 时拼出完整回复。
    """
    parts = []
    async for evt in gen:
        if "data: " not in evt:
            continue
        payload = evt.split("data: ", 1)[1].strip()
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if isinstance(data, str):
            parts.append(data)
    return "".join(parts)


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


def _build_ollama_options(temperature=None, max_tokens=None, top_p=None) -> "dict":
    """把生成参数收敛为 Ollama options 字典（仅当显式传入时附加，避免覆盖模型默认）。

    R1 抽出的纯函数：chat 与 regenerate 两条生成链路共用同一套「参数 -> options」
    映射，保证两者对 temperature / max_tokens / top_p 的透传口径完全一致（DRY + 一致性）。
    空 options 时返回空 dict，调用方据此决定是否附加。
    """
    options: Dict[str, object] = {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["max_tokens"] = max_tokens
    if top_p is not None:
        options["top_p"] = top_p
    return options


async def _ollama_stream(model: str, messages: List[Dict], temperature=None, max_tokens=None, top_p=None) -> str:
    """转发到 Ollama /api/chat（stream=true），增量 token 推给前端。

    R1 新能力：temperature / max_tokens / top_p 经 options 透传给 Ollama，
    让用户/调用方控制生成温度、长度与核采样（仅当显式传入时附加，避免覆盖模型默认）。
    """
    payload = {"model": model, "messages": messages, "stream": True}
    options = _build_ollama_options(temperature, max_tokens, top_p)
    if options:
        payload["options"] = options
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
    # R1 新能力：role 收口为合法枚举，畸形 role（如 "bot"）在边界即 422，
    # 不再被静默落库（R2 隐性一致性：此前任何字符串都能存，下游统计/渲染易错）。
    role: Literal["system", "user", "assistant"]
    content: str = ""


class ChatRequest(BaseModel):
    """聊天请求体（输入校验，避免裸 JSON 解析导致 500）。"""
    model: str = ""
    messages: List[MessageItem] = Field(default_factory=list)
    # R1 新能力：生成参数透传（与 CLI 侧 ask/chat 一致），便于控制温度/长度/核采样。
    # 默认 None 表示沿用模型默认；经 _ollama_stream 透传到 Ollama options。
    temperature: "float | None" = None
    max_tokens: "int | None" = None
    top_p: "float | None" = None


class SettingsRequest(BaseModel):
    """设置请求体（当前支持默认模型记忆）。"""
    default_model: str = ""


class RenameRequest(BaseModel):
    """会话重命名请求体。"""
    title: str


class EditMessageRequest(BaseModel):
    """编辑单条消息请求体。"""
    content: str = ""


class AppendMessageRequest(BaseModel):
    """向会话追加单条消息的请求体（程序化 API 注入，区别于整体 chat 保存）。"""
    # role 收口为合法枚举：畸形 role 在边界即 422，不再被静默落库。
    role: Literal["system", "user", "assistant"]
    content: str = ""


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


@app.get("/api/version")
async def version():
    """R1 新能力：暴露应用版本与标题，便于前端展示与脚本/流水线断言（如灰度发布
    比对、健康检查附带版本），与 FastAPI 声明的 version 单一来源保持一致。"""
    return {
        "name": app.title,
        "version": app.version,
        "mock": MOCK_LLM,
    }


@app.get("/api/stats")
async def stats():
    """全局统计（可观测性）：会话总数、消息总数、当前会话 id。"""
    s = db_store.get_stats()
    return {
        "sessions": s["sessions"],
        "messages": s["messages"],
        "current": _current_sid(),
    }


@app.get("/api/search")
async def search(q: str = "", limit: int = 50, role: str = ""):
    """跨会话全文检索消息（按内容模糊匹配），便于在历史中定位关键信息。

    R1 新能力：role 过滤（user / assistant）与单会话消息列表的 role 过滤对称；
    非法 role 值忽略不过滤。返回结果含每条消息 id，便于前端跳转/编辑定位。
    """
    if not q:
        return {"results": []}
    limit = max(1, min(int(limit), 200))  # 钳制上限，避免超大结果集拖垮响应
    role_filter = role if role in ("user", "assistant") else None
    results = db_store.search_messages(q, limit=limit, role=role_filter)
    return {"results": results}


@app.post("/api/chat")
async def chat(req: ChatRequest, stream: bool = True):
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    # R2 健壮性：空消息体对 LLM 无意义，且会把空请求打到 Ollama 触发 400；
    # 在边界即拦截为 422，明确提示而非透传底层错误。
    if not messages:
        raise HTTPException(status_code=422, detail="messages 不能为空")

    sid = _current_sid()
    # R1 新能力：模型解析优先级 请求显式 model > 本会话已存模型(per-session) >
    # 全局默认设置。此前仅保存模型、从不回退，导致不重复传 model 的后续对话
    # 丢失会话模型（R2 隐性缺陷：per-session 模型记忆形同虚设）。
    model = (
        req.model
        or db_store.get_model(sid)
        or db_store.get_setting("default_model")
        or None
    )
    if model:
        db_store.set_model(sid, model)
        db_store.set_setting("default_model", model)  # 记忆默认模型（跨会话）
    # 每次前端传完整历史，整体落盘
    if messages:
        db_store.save_messages(sid, messages)
        # 自动标题：首条用户消息 -> 会话标题（可观测性 + 多会话可读性）。
        # R2 修复（隐性状态缺陷）：清空会话后标题会被重置为哨兵值「新对话」
        # （见 clear_messages），而旧逻辑只在「标题为空」时才推导——哨兵值
        # 非空，导致清空后再开聊标题卡在「新对话」、无法重新派生。现把哨兵值
        # 也视为「需要重新派生」，清空后即可随首条用户消息更新真实标题。
        existing_title = db_store.get_title(sid)
        if not existing_title or existing_title == "新对话":
            for m in messages:
                if m.get("role") == "user" and m.get("content"):
                    title = m["content"].strip().replace("\n", " ")[:40]
                    if title:
                        db_store.set_title(sid, title)
                    break

    # 提取用于 mock 的用户文本（非 mock 模式也复用）
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "")
            break

    async def event_gen():
        if MOCK_LLM:
            async for chunk in _mock_stream(user_text):
                yield chunk
        else:
            if not model:
                yield _sse("error", json.dumps("请先选择或输入模型名称", ensure_ascii=False))
                return
            async for chunk in _ollama_stream(
                model, messages, temperature=req.temperature,
                max_tokens=req.max_tokens, top_p=req.top_p
            ):
                yield chunk

    # R1 新需求：非流式模式（stream=0/false）直接返回完整 JSON 回复，
    # 便于程序化 / API 消费者（无需解析 SSE），复用同一套生成器。
    if not stream:
        if not MOCK_LLM and not model:
            return JSONResponse(
                status_code=400,
                content={"error": "请先选择或输入模型名称"},
            )
        if MOCK_LLM:
            reply = await _collect_sse_text(_mock_stream(user_text))
        else:
            parts = []
            async for evt in _ollama_stream(
                model, messages, temperature=req.temperature,
                max_tokens=req.max_tokens, top_p=req.top_p
            ):
                if evt.startswith("event: error"):
                    data = evt.split("data: ", 1)[1].strip()
                    try:
                        err = json.loads(data)
                    except Exception:
                        err = data
                    return JSONResponse(status_code=502, content={"error": err})
                data = evt.split("data: ", 1)[1].strip()
                try:
                    parsed = json.loads(data)
                except Exception:
                    continue
                # R2 修复（隐性崩溃）：Ollama 流末尾会发一个 data 为对象（如
                # {"ok": true}）的 done 事件，若不做类型判断直接 append 再
                # "".join，会因「字符串与 dict 混排」抛 TypeError，导致非流式
                # 真实后端调用直接 500。只有字符串 token 才计入回复正文
                # （与 _collect_sse_text 的 isinstance str 守卫一致）。
                if isinstance(parsed, str):
                    parts.append(parsed)
            reply = "".join(parts)
        return {"reply": reply, "ok": True, "model": model or "mock"}

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
async def list_sessions_ep(title: str = "", limit: int = 0):
    """多会话管理：列出全部会话及当前会话 id。

    R1 新能力：可选 ?title= 关键词，按会话标题过滤（与 /api/search 的消息正文
    检索互补），便于会话多时快速定位目标会话。
    R1 新能力：可选 ?limit= 限制返回会话数（>0 生效），会话极多时只取最近若干，
    与消息分页思路一致，避免一次性拉取全量。
    R2 修复：此前端点调用 search_sessions 时从不透传调用方 limit，导致
    ?title= 与 ?limit= 同时给出时限被丢弃（search_sessions 的 limit 形同虚设）。
    现统一解析 limit 并透传两条路径。
    """
    resolved = limit if limit and limit > 0 else None
    if title:
        sessions = db_store.search_sessions(title, limit=resolved or 50)
    else:
        sessions = db_store.list_sessions(limit=resolved)
    return {"sessions": sessions, "current": _current_sid()}


@app.get("/api/sessions/{sid}")
async def get_session_ep(sid: str):
    """获取单个会话的概要（model/created/title/消息数）与完整消息（用于「打开历史会话」）。

    R1 增强：补全 model/created/message_count 概要字段，前端「会话详情」无需
    再发额外请求；会话不存在时返回 404（而非静默回空列表造成幽灵会话）。
    """
    det = db_store.get_session_detail(sid)
    if not det:
        raise HTTPException(status_code=404, detail="session not found")
    det["messages"] = db_store.get_messages(sid)
    return det


@app.post("/api/sessions/{sid}/switch")
async def switch_session_ep(sid: str):
    """切换到指定会话（用于「打开历史会话」）。"""
    db_store.switch_session(sid)
    return {"ok": True, "session_id": sid}


@app.delete("/api/sessions/{sid}")
async def delete_session_ep(sid: str):
    """删除指定会话（避免旧会话无限堆积）。

    R2 修复：删除当前会话后，返回「删除后的当前会话 id」（可能为新重建的会话），
    而非被删会话 id，避免前端把已删会话误认为仍在进行中。
    """
    new_sid = db_store.delete_session(sid)
    return {"ok": True, "session_id": new_sid}


@app.post("/api/sessions/{sid}/rename")
async def rename_session_ep(sid: str, req: RenameRequest):
    """重命名会话（修正自动标题，提升多会话可读性）。"""
    title = (req.title or "").strip()[:200]
    db_store.set_title(sid, title)
    return {"ok": True, "id": sid, "title": title}


@app.post("/api/sessions/{sid}/fork")
async def fork_session_ep(sid: str, title: str = ""):
    """克隆会话（复制模型/标题/全部消息），用于在不破坏原会话的前提下分叉探索。

    源会话不存在时返回 404（而非静默创建空会话）。
    """
    new_sid = db_store.copy_session(sid, title or None)
    if not new_sid:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, "id": new_sid, "title": db_store.get_title(new_sid)}


@app.post("/api/sessions/{sid}/clear")
async def clear_session_ep(sid: str):
    """清空会话消息但保留会话本身（重置为新一轮对话）。"""
    db_store.clear_messages(sid)
    return {"ok": True, "id": sid, "title": "新对话"}


@app.post("/api/sessions/cleanup")
async def cleanup_sessions_ep(keep: int = 10):
    """批量清理旧会话：保留最近 keep 个，删除其余（当前会话永不被删）。

    R1 新能力：针对「旧会话无限堆积」的整理入口；返回实际删除的会话数，
    便于前端展示「已清理 N 个会话」。
    """
    removed = db_store.cleanup_sessions(keep)
    return {"ok": True, "removed": removed, "current": _current_sid()}


@app.post("/api/sessions/{sid}/pin")
async def pin_session_ep(sid: str, pinned: bool = True):
    """置顶/取消置顶某会话（收藏夹语义）。

    R1 新能力：会话多时把重要会话钉在列表顶部（同类产品标配）。
    pinned 经查询参数传入（默认 True 置顶；?pinned=false 取消置顶）。
    会话不存在返回 404（与 set_pin 口径一致）。
    """
    res = db_store.set_pin(sid, pinned)
    if res is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, **res}


@app.get("/api/sessions/{sid}/export")
async def export_session_ep(sid: str, format: str = "md"):
    """将会话导出为 Markdown 文本（便于存档 / 分享），原样返回消息流转。

    format：导出格式，默认 "md"（人读 Markdown）；"json" 时返回
    结构化 JSON（id / title / messages 列表），与 md 互补——便于程序化
    消费、跨系统迁移、或对接下游分析管线，无需再解析 Markdown。

    R2 修复（隐性一致性缺陷）：原实现对不存在的会话也返回 200 与一段空
    「对话记录」Markdown，与 get_session_ep（缺失即 404）行为不一致，
    会给调用方造成「幽灵会话可导出」的错觉。现先校验会话存在，缺失则 404。
    """
    if not db_store.get_session_detail(sid):
        raise HTTPException(status_code=404, detail="session not found")
    msgs = db_store.get_messages(sid)
    title = db_store.get_title(sid) or "对话记录"
    # R1 新能力：format=json 直接返回结构化消息列表，机器可读。
    if format == "json":
        return {"ok": True, "id": sid, "title": title, "messages": msgs}
    lines = [f"# {title}", ""]
    for m in msgs:
        role = m.get("role", "")
        label = "用户" if role == "user" else ("助手" if role == "assistant" else role)
        lines.append(f"**{label}：**")
        lines.append(m.get("content", ""))
        lines.append("")
    return {"ok": True, "id": sid, "title": title, "markdown": "\n".join(lines)}


@app.get("/api/backup")
async def backup_ep():
    """整库备份导出：一次性导出所有会话及其完整消息为机读 JSON。

    R1 新能力：与单会话 /api/sessions/{sid}/export（Markdown）互补，提供
    全量备份能力（迁移 / 离线分析 / 灾难恢复），无需逐会话调用。返回结构
    {"ok": True, "count": N, "sessions": [{id, title, model, created,
    message_count, messages:[{role, content}]}]}。
    """
    sessions = db_store.export_all_sessions()
    return {"ok": True, "count": len(sessions), "sessions": sessions}


@app.get("/api/sessions/{sid}/messages")
async def session_messages_ep(sid: str, limit: int = 0, offset: int = 0, role: str = "", q: str = ""):
    """分页返回会话消息（limit<=0 表示不限），便于超长会话按需加载。

    R1 新能力：role 过滤（user / assistant），便于「只看用户提问」或
    「只看助手回答」的场景（如导出、复盘、构建训练样本）。
    非法 role 值（非 user/assistant）被忽略，等价不过滤，避免 400 误伤。
    R1 新能力：q 内容子串检索（LIKE，已转义通配符），在单个会话内快速
    定位某条消息，与全局 /api/search 的跨会话检索互补。空 q 不过滤。
    """
    limit = limit if limit and limit > 0 else None
    role_filter = role if role in ("user", "assistant") else None
    q_filter = q if q and q.strip() else None
    msgs = db_store.get_messages(
        sid, limit=limit, offset=max(0, offset), role=role_filter, q=q_filter
    )
    # R1 新能力：返回 total（忽略分页的过滤后总数），便于分页 UI 计算页数，
    # 无需再发一次无 limit 请求自行统计。count 为当页实际条数。
    total = db_store.count_messages_filtered(sid, role=role_filter, q=q_filter)
    return {"ok": True, "id": sid, "messages": msgs,
            "count": len(msgs), "total": total, "limit": limit, "offset": max(0, offset),
            "role": role_filter, "q": q_filter}


@app.post("/api/sessions/{sid}/messages")
async def append_message_ep(sid: str, req: AppendMessageRequest):
    """向指定会话追加一条消息（程序化 API 注入，区别于整体 chat 保存）。

    R1 新能力：无需每次传完整历史即可单条补录消息，适合 API 客户端 / 数据导入。
    R2 一致性：复用 db.append_message（含 role 枚举校验 + 自动标题 + content 截断），
    使通过本接口注入首条 user 消息的会话也能像 chat 一样自动派生标题，
    行为不脱节。会话不存在返回 404；role 非法由 Pydantic 在边界 422。
    """
    result = db_store.append_message(sid, req.role, req.content)
    if result is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, **result}


@app.get("/api/messages/{mid}")
async def get_message_ep(mid: int):
    """获取单条消息（按 id），便于前端定位/编辑某条历史消息。"""
    conn = db_store._conn()
    try:
        row = conn.execute(
            "SELECT session_id, role, content FROM messages WHERE id=?", (mid,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="message not found")
    return {"id": mid, "session_id": row[0], "role": row[1], "content": row[2]}


@app.delete("/api/messages/{mid}")
async def delete_message_ep(mid: int):
    """删除单条消息（区别于清空/删除整个会话）。

    R2 修复：消息不存在时返回 404 而非静默成功；
    删除后若会话已空，标题自动重置为「新对话」（一致性）。
    """
    res = db_store.delete_message(mid)
    if res is None:
        raise HTTPException(status_code=404, detail="message not found")
    return {"ok": True, **res}


@app.put("/api/messages/{mid}")
async def edit_message_ep(mid: int, req: EditMessageRequest):
    """编辑单条消息内容（区别于清空/删除整个会话）。

    R1 新能力：就地修订某条历史消息。
    R2 修复：若编辑的是会话「首条 user 消息」（往往即自动标题来源），
    同步更新会话标题，避免「标题与首条内容脱节」的错觉；
    消息不存在返回 404。
    """
    res = db_store.update_message(mid, req.content)
    if res is None:
        raise HTTPException(status_code=404, detail="message not found")
    return {"ok": True, **res}


@app.delete("/api/sessions/{sid}/messages")
async def clear_messages_ep(sid: str):
    """清空某会话的全部消息（保留会话本身），标题重置为新对话。

    R1 新能力：批量清空消息，区别于删除整个会话（delete_session）。
    R2 一致性：清空后标题重置为「新对话」，与单条删除致空会话的行为一致；
    会话不存在返回 404。
    """
    res = db_store.clear_messages(sid)
    if res is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, **res}


@app.post("/api/sessions/{sid}/regenerate")
async def regenerate_ep(sid: str, temperature: "float | None" = None, max_tokens: "int | None" = None, top_p: "float | None" = None):
    """重新生成最后一条助手回复（常见聊天 UX：对上一条回答不满意时重答）。

    R1 新能力：若会话末尾是助手回复，先删除它再基于其前的历史重新生成；
    若末尾是用户消息，则直接为其补一条新助手回复。生成复用与 chat 相同的
    SSE / Mock 链路，并在流结束后把新回复落盘，保证「重新生成」后历史持久化一致。
    会话不存在 / 无消息 / 无用户上下文时返回 400。

    R2 修复（隐性能力不一致）：原实现重新生成时直接调用
    `_ollama_stream(model, history)`，**完全忽略了** chat 已支持的
    temperature / max_tokens / top_p 生成参数——用户在前端调过低温度/限长后，
    点「重新生成」却以模型默认参数生成，行为与 chat 脱节。现通过查询参数
    temperature / max_tokens / top_p 透传同一套 options（与 chat 共用
    _build_ollama_options，保证口径一致），让重生成与首答遵循相同生成策略。
    """
    msgs = db_store.get_messages(sid)
    if not msgs:
        raise HTTPException(status_code=400, detail="会话无消息，无法重新生成")
    # 末尾若是助手回复，先移除以替换
    if msgs[-1].get("role") == "assistant":
        db_store.delete_message(msgs[-1]["id"])
        msgs = msgs[:-1]
    if not msgs:
        raise HTTPException(status_code=400, detail="没有可作为上下文的用户消息")

    user_text = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            user_text = m.get("content", "")
            break
    history = [{"role": m["role"], "content": m["content"]} for m in msgs]

    async def event_gen():
        token_parts: list = []
        if MOCK_LLM:
            gen = _mock_stream(user_text)
        else:
            model = (
                db_store.get_model(sid)
                or db_store.get_setting("default_model")
                or None
            )
            if not model:
                yield _sse("error", json.dumps("请先选择或输入模型名称", ensure_ascii=False))
                return
            gen = _ollama_stream(
                model, history,
                temperature=temperature, max_tokens=max_tokens, top_p=top_p
            )
        async for chunk in gen:
            # 从 token 事件抽取文本，留待流结束后落盘
            if "data: " in chunk:
                payload = chunk.split("data: ", 1)[1].strip()
                try:
                    data = json.loads(payload)
                except Exception:
                    data = None
                if isinstance(data, str):
                    token_parts.append(data)
            yield chunk
        reply = "".join(token_parts)
        if reply:
            db_store.append_message(sid, "assistant", reply)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/history")
async def history():
    sess = _load_session()
    return {"session_id": sess["id"], "messages": sess["messages"], "model": sess["model"]}


@app.post("/api/new")
@app.post("/api/clear")
async def new_session():
    sid = db_store.new_session()
    # R2 修复：新会话必须显式成为「当前会话」，否则 chat 仍写入旧会话指针，
    # 导致多会话切换/历史恢复场景下消息落到错误会话（test_session_messages_pagination 失败根因）。
    db_store.set_current_sid(sid)
    return {"ok": True, "session_id": sid}


@app.get("/api/current")
async def current_session_ep():
    """R1 新需求：暴露当前会话指针，便于前端/脚本确认「正在与哪个会话对话」。"""
    sid = _current_sid()
    return {"session_id": sid}


# ---------- 启动说明 ----------
if __name__ == "__main__":
    import os, webbrowser, uvicorn

    os.environ.setdefault("MOCK_LLM", "1")
    print("OpenWebUI Lite 启动中…（将自动打开浏览器）")
    print(f"  MOCK_LLM = {MOCK_LLM}")
    print(f"  OLLAMA_HOST = {OLLAMA_BASE}")
    log.info("OpenWebUI Lite 启动 MOCK_LLM=%s OLLAMA_HOST=%s", MOCK_LLM, OLLAMA_BASE)
    webbrowser.open("http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)

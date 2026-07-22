"""OpenWebUI Lite — SQLite 会话持久化。

替代原内存存储，使对话历史在进程重启后不丢失。
表结构：
  sessions(id, model, created)
  messages(id, session_id, role, content, ts)
  kv(k, v)  —— 存「当前会话」指针
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid

# 允许通过环境变量重定向数据库文件（测试隔离 / 自定义路径）。
# 默认落盘于模块同目录的 sessions.db。
DB_PATH = os.environ.get(
    "OPENWEBUI_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db"),
)


def _conn() -> sqlite3.Connection:
    # check_same_thread=False：FastAPI 异步事件循环可能跨线程访问
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        # WAL：写操作走预写日志，显著降低并发写时的「database is locked」
        # synchronous=NORMAL：在性能与持久性间取平衡（MVP 足够）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def init() -> None:
    conn = _conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, model TEXT, created REAL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT, ts REAL
            );
            CREATE TABLE IF NOT EXISTS kv (
                k TEXT PRIMARY KEY, v TEXT
            );
            """
        )
        # 迁移：补 title 列（旧库兼容，已存在则忽略）
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


# ---------- 当前会话指针 ----------
def get_current_sid() -> "str | None":
    conn = _conn()
    try:
        row = conn.execute("SELECT v FROM kv WHERE k='current_session'").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def set_current_sid(sid: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO kv(k, v) VALUES('current_session', ?)", (sid,)
        )
        conn.commit()
    finally:
        conn.close()


def get_or_create_current() -> str:
    sid = get_current_sid()
    if not sid:
        sid = uuid.uuid4().hex
        conn = _conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO sessions(id, model, created) VALUES(?, ?, ?)",
                (sid, "", time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        set_current_sid(sid)
    return sid


# ---------- 会话内容 ----------
def ensure_session(sid: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sessions(id, model, created) VALUES(?, ?, ?)",
            (sid, "", time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def get_messages(
    sid: str, limit: "int | None" = None, offset: int = 0, role: "str | None" = None
) -> list[dict]:
    """返回某会话的消息列表（按 id 升序）。

    limit/offset 用于分页：超大会话无需一次性全部载入（隐性性能/内存隐患）。
    limit 为 None 或 <=0 表示不限制。
    role：可选过滤（"user" / "assistant"），None 表示不过滤。

    R2 修复（一致性/可观测性）：原实现只返回 {role, content}，缺少每条消息的
    id，导致前端通过分页接口拿到消息后无法定位/编辑/删除具体某条（只能再走
    /api/messages/{mid} 但无从得知 mid）。现每条消息都带 id。
    """
    params: list = [sid]
    sql = "SELECT id, role, content FROM messages WHERE session_id=?"
    if role:
        sql += " AND role=?"
        params.append(role)
    sql += " ORDER BY id"
    if limit and limit > 0:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    conn = _conn()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "role": r[1], "content": r[2]} for r in rows]


def save_messages(sid: str, messages: list[dict]) -> None:
    """整体替换该会话的消息（前端每次传完整历史）。"""
    conn = _conn()
    try:
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        for m in messages:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, ts) VALUES(?, ?, ?, ?)",
                (sid, m.get("role", ""), m.get("content", ""), time.time()),
            )
        conn.commit()
    finally:
        conn.close()


def set_model(sid: str, model: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE sessions SET model=? WHERE id=?", (model, sid))
        conn.commit()
    finally:
        conn.close()


def get_model(sid: str) -> str:
    conn = _conn()
    try:
        row = conn.execute("SELECT model FROM sessions WHERE id=?", (sid,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else ""


def new_session() -> str:
    sid = uuid.uuid4().hex
    ensure_session(sid)
    set_current_sid(sid)
    return sid


def copy_session(src_sid: str, title: "str | None" = None) -> "str | None":
    """克隆一个会话：复制其模型、标题与全部消息，返回新会话 id。

    用于「分叉探索」——在某一轮对话基础上开新支线而不破坏原会话。
    源会话不存在时返回 None（调用方应据此返回 404）。
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT model, title FROM sessions WHERE id=?", (src_sid,)
        ).fetchone()
        if not row:
            return None
        src_model, src_title = row
        msgs = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id",
            (src_sid,),
        ).fetchall()
    finally:
        conn.close()

    new_sid = uuid.uuid4().hex
    ensure_session(new_sid)
    if src_model:
        set_model(new_sid, src_model)
    final_title = title if title else (src_title or "")
    set_title(new_sid, final_title)
    copied = [{"role": r, "content": c} for r, c in msgs]
    if copied:
        save_messages(new_sid, copied)
    return new_sid


# ---------------------------------------------------------------------------
# 通用设置（kv 复用，存跨会话偏好，如默认模型）
# ---------------------------------------------------------------------------
def get_setting(k: str, default: str = "") -> str:
    conn = _conn()
    try:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else default


def set_setting(k: str, v: str) -> None:
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, v))
        conn.commit()
    finally:
        conn.close()


def count_messages(sid: str) -> int:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def set_title(sid: str, title: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))
        conn.commit()
    finally:
        conn.close()


def get_title(sid: str) -> str:
    conn = _conn()
    try:
        row = conn.execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else ""


def list_sessions() -> list[dict]:
    """列出全部会话（含消息数），用于多会话管理 UI。

    按建立时间倒序，最近的在前。
    隐性性能：原实现对每个会话各发一次 count_messages 查询（N+1），会话多时
    列表端点明显变慢；这里用一次 LEFT JOIN + GROUP BY 拿到全部消息计数。
    """
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT s.id, s.model, s.created, s.title, COALESCE(m.cnt, 0) "
            "FROM sessions s "
            "LEFT JOIN (SELECT session_id, COUNT(*) AS cnt FROM messages GROUP BY session_id) m "
            "ON m.session_id = s.id "
            "ORDER BY s.created DESC"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for sid, model, created, title, cnt in rows:
        out.append(
            {
                "id": sid,
                "model": model,
                "created": created,
                "title": title or "",
                "message_count": cnt,
            }
        )
    return out


def get_stats() -> dict:
    """返回全局统计（会话总数 / 消息总数），供 /api/stats 等可观测端点使用。

    单连接内两次聚合，避免多次往返。
    """
    conn = _conn()
    try:
        s_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        m_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    return {"sessions": s_count, "messages": m_count}


def search_messages(q: str, limit: int = 50) -> list[dict]:
    """跨会话按内容模糊检索消息（LIKE 匹配），用于历史定位。

    返回 [{"session_id", "title", "role", "content"}, ...]，按插入顺序倒序。

    R2 隐性正确性：用户搜索词若含 LIKE 通配符 `%` / `_`（如「50%」「user_name」），
    原实现直接拼进 `%{q}%`，通配符会被当成模式，导致误命中/漏命中。
    这里先转义（`\\` `%` `_`），再用 ESCAPE 子句声明转义符，使输入按字面量匹配。
    """
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT m.session_id, s.title, m.role, m.content "
            "FROM messages m LEFT JOIN sessions s ON s.id = m.session_id "
            "WHERE m.content LIKE ? ESCAPE '\\' ORDER BY m.id DESC LIMIT ?",
            (like, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"session_id": sid, "title": title or "", "role": role, "content": content}
        for sid, title, role, content in rows
    ]


def switch_session(sid: str) -> str:
    """切换当前会话指针到指定 sid（不存在则先 ensure）。"""
    ensure_session(sid)
    set_current_sid(sid)
    return sid


def delete_session(sid: str) -> None:
    """删除会话及其全部消息；若删的是当前会话，自动重建一个干净当前会话。"""
    conn = _conn()
    try:
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        conn.commit()
    finally:
        conn.close()
    if get_current_sid() == sid:
        new_session()  # 避免 current 指针悬空


def clear_messages(sid: str) -> None:
    """清空某会话的全部消息，但保留会话本身（标题重置为「新对话」）。

    与 delete_session 的区别：删除会移除整个会话；清空只重置对话内容，
    便于在不丢失会话列表/位置的前提下重新开始一轮新对话。
    """
    conn = _conn()
    try:
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        conn.execute(
            "UPDATE sessions SET title='新对话' WHERE id=?", (sid,)
        )
        conn.commit()
    finally:
        conn.close()


def delete_message(mid: int) -> "dict | None":
    """删除单条消息（区别于清空/删除整个会话）。

    R1 新能力：支持逐条删除（如误发/错误回复）。
    R2 修复（隐性一致性缺陷）：此前若用全量 save_messages 覆盖式保存，
    单条删除后若该会话消息归零，会话标题仍停留在旧的自动标题，
    造成「空会话却顶着一条历史标题」的错觉。这里在删除后检测会话
    是否清空，是则把标题重置为「新对话」，与 clear_messages 行为一致。
    消息不存在时返回 None，供端点返回 404（而非静默成功）。
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT session_id, role FROM messages WHERE id=?", (mid,)
        ).fetchone()
        if not row:
            return None
        sid, role = row
        conn.execute("DELETE FROM messages WHERE id=?", (mid,))
        cnt = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)
        ).fetchone()[0]
        title = ""
        if cnt == 0:
            conn.execute("UPDATE sessions SET title='新对话' WHERE id=?", (sid,))
            conn.commit()
            title = "新对话"
        else:
            conn.commit()
    finally:
        conn.close()
    return {"id": mid, "session_id": sid, "role": role,
            "message_count": cnt, "title": title}


def update_message(mid: int, content: str) -> "dict | None":
    """编辑单条消息内容（区别于删除/覆盖式保存）。

    R1 新能力：支持就地修订某条历史消息（如改错别字、补全问题）。
    R2 修复（隐性一致性缺陷）：若该消息是会话的「第一条 user 消息」，
    它往往正是自动标题的来源；改了内容却不同步标题，会造成
    「会话标题与首条内容不一致」的错觉。这里在编辑首条 user 消息且
    内容非空时，自动同步更新会话标题，与 auto-title 行为一致。
    消息不存在返回 None（供端点 404）。
    """
    content = str(content)[:100000]  # 防超大内容撑爆单元格
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT session_id, role FROM messages WHERE id=?", (mid,)
        ).fetchone()
        if not row:
            return None
        sid, role = row
        conn.execute("UPDATE messages SET content=? WHERE id=?", (content, mid))
        title = None
        first = conn.execute(
            "SELECT id, role, content FROM messages WHERE session_id=? "
            "ORDER BY id LIMIT 1", (sid,)
        ).fetchone()
        if first and first[1] == "user" and first[0] == mid:
            new_title = content.strip().replace("\n", " ")[:40]
            if new_title:
                conn.execute(
                    "UPDATE sessions SET title=? WHERE id=?", (new_title, sid)
                )
                title = new_title
        conn.commit()
    finally:
        conn.close()
    return {"id": mid, "session_id": sid, "role": role, "title": title}

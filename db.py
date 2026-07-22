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

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.db")


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


def get_messages(sid: str) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id", (sid,)
        ).fetchall()
    finally:
        conn.close()
    return [{"role": r, "content": c} for r, c in rows]


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


def count_messages(sid: str) -> int:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def list_sessions() -> list[dict]:
    """列出全部会话（含消息数），用于多会话管理 UI。

    按建立时间倒序，最近的在前。
    """
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, model, created FROM sessions ORDER BY created DESC"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for sid, model, created in rows:
        out.append(
            {
                "id": sid,
                "model": model,
                "created": created,
                "message_count": count_messages(sid),
            }
        )
    return out


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

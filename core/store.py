"""会话与消息的本地持久化（SQLite）。

数据文件位置：
  - 源码运行：项目目录下 data/chat.db
  - 打包成 exe：%LOCALAPPDATA%\\LocalAIChat\\data\\chat.db
这样用户装了 exe 之后，卸载重装的边际影响小，数据也不会污染安装目录。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT    NOT NULL DEFAULT '新对话',
    model       TEXT    NOT NULL DEFAULT '',
    system      TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL DEFAULT '',
    stats      TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "model": "",
    "system_prompt": (
        "你是「小墨」，一个运行在本机的本地 AI 助手，说话简洁、直接、用中文。"
        "你不编造信息；不知道就说不知道；涉及隐私或本地文件时优先保护用户数据。"
        "遇到任务会先想清楚再动手，能给出可执行的步骤。"
    ),
    "temperature": 0.7,
    "num_ctx": 8192,
    "top_p": 0.9,
    "keep_alive": "5m",
    "knowledge_enabled": False,
    # 思考强度：0=关闭 1=轻 2=中(拆解+推导) 3=强(显式输出推导过程)
    "thinking_level": 2,
    # Bionic Reading 文本加粗效果
    "bionic_enabled": False,
    # 电脑文件控制（默认关闭，首次开启需同意免责声明）
    "file_control_enabled": False,
    "file_control_disclaimer_accepted": False,
}


def data_dir() -> str:
    import sys

    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "LocalAIChat", "data")
    else:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(path, exist_ok=True)
    return path


class Store:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.path.join(data_dir(), "chat.db")
        self.db_path = db_path
        self._init()

    # ---------------------------------------------------------------- 内部
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        # WAL 让「写入历史」和「流式渲染」不至于互相卡住
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                    (k, json.dumps(v, ensure_ascii=False)),
                )

    # ---------------------------------------------------------------- 设置
    def get_settings(self) -> dict[str, Any]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out = dict(DEFAULT_SETTINGS)
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                pass
        return out

    def set_setting(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def set_settings(self, mapping: dict[str, Any]) -> None:
        for k, v in mapping.items():
            self.set_setting(k, v)

    # ---------------------------------------------------------------- 会话
    def create_session(
        self, title: str = "新对话", model: str = "", system: str = ""
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        sid = uuid.uuid4().hex[:16]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions(id,title,model,system,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (sid, title, model, system, now, now),
            )
        return {
            "id": sid,
            "title": title,
            "model": model,
            "system": system,
            "created_at": now,
            "updated_at": now,
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS msg_count,
                       (SELECT m.content FROM messages m
                        WHERE m.session_id = s.id ORDER BY m.id DESC LIMIT 1) AS last_msg
                FROM sessions s
                ORDER BY s.updated_at DESC
                """
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["preview"] = (d.pop("last_msg") or "").replace("\n", " ")[:60]
            out.append(d)
        return out

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def rename_session(self, session_id: str, title: str) -> None:
        title = (title or "").strip()[:80] or "新对话"
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                (title, int(time.time() * 1000), session_id),
            )

    def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"title", "model", "system"}
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        sets = ", ".join(f"{c}=?" for c in cols)
        vals = [fields[c] for c in cols] + [int(time.time() * 1000), session_id]
        with self._conn() as conn:
            conn.execute(f"UPDATE sessions SET {sets}, updated_at=? WHERE id=?", vals)

    def delete_session(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def clear_all(self) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")

    # ---------------------------------------------------------------- 消息
    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        stats: dict[str, Any] | None = None,
    ) -> int:
        now = int(time.time() * 1000)
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO messages(session_id,role,content,stats,created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    session_id,
                    role,
                    content,
                    json.dumps(stats or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id)
            )
            return int(cur.lastrowid)

    def update_message(
        self,
        msg_id: int,
        content: str,
        stats: dict[str, Any] | None = None,
    ) -> None:
        with self._conn() as conn:
            if stats is None:
                conn.execute("UPDATE messages SET content=? WHERE id=?", (content, msg_id))
            else:
                conn.execute(
                    "UPDATE messages SET content=?, stats=? WHERE id=?",
                    (content, json.dumps(stats, ensure_ascii=False), msg_id),
                )

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, role, content, stats, created_at FROM messages"
                " WHERE session_id=? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["stats"] = json.loads(d["stats"] or "{}")
            except json.JSONDecodeError:
                d["stats"] = {}
            out.append(d)
        return out

    def delete_from(self, session_id: str, msg_id: int) -> None:
        """删除指定消息及其之后的所有消息——「重新生成」时用。"""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM messages WHERE session_id=? AND id>=?",
                (session_id, msg_id),
            )

    def delete_message(self, msg_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE id=?", (msg_id,))

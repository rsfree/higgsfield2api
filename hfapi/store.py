"""本地任务台账（sqlite）。只存我们自己看到的事实：提交时间、上游 id、最后一次快照。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import settings

DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    job_id       TEXT,
    job_set_id   TEXT,
    model_echo   TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    submitted_at INTEGER NOT NULL,
    callback_url TEXT,
    payload      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key      TEXT PRIMARY KEY,
    task_id  TEXT NOT NULL,
    created  INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.task_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(DDL)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # -- 幂等
    def idem_get(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT task_id FROM idempotency WHERE key=?", (key,)).fetchone()
        return row["task_id"] if row else None

    def idem_put(self, key: str, task_id: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO idempotency(key,task_id,created) VALUES(?,?,?)",
                      (key, task_id, int(time.time())))

    # -- 任务
    def put(self, task: dict[str, Any], *, job_id: str | None = None, job_set_id: str | None = None,
            callback_url: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO tasks(id,job_id,job_set_id,model_echo,status,created_at,updated_at,
                                     submitted_at,callback_url,payload)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     status=excluded.status,
                     updated_at=excluded.updated_at,
                     payload=excluded.payload,
                     job_id=COALESCE(excluded.job_id, tasks.job_id),
                     job_set_id=COALESCE(excluded.job_set_id, tasks.job_set_id)""",
                (
                    task["id"], job_id, job_set_id, task["model"], task["status"],
                    int(task["created_at"]), int(task["updated_at"]),
                    int(task["created_at"]), callback_url, json.dumps(task, ensure_ascii=False),
                ),
            )

    def touch_status(self, task_id: str, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def resolve_job_id(self, task_id: str) -> str | None:
        row = self.get(task_id)
        if not row:
            return None
        return row.get("job_id") or row.get("job_set_id") or task_id

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT payload FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["payload"]) for r in rows]

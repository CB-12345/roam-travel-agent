"""Durable local tasks: bounded execution and replayable SSE events in SQLite."""

from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from src.services.errors import public_error
from src.services.observability import RunRecorder, recording

TERMINAL = {"completed", "failed"}


class TaskConflict(ValueError):
    pass


class TaskBusy(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class TaskStore:
    def __init__(self, path: Path, *, recover: bool = True):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._db() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, idempotency_key TEXT UNIQUE,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    result TEXT, error TEXT, trace TEXT
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, name TEXT NOT NULL, data TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(task_id,seq)
                );
            """
            )
        if recover:
            with self.lock, self._db() as db:
                for row in db.execute(
                    "SELECT id FROM tasks WHERE status IN ('pending','running')"
                ).fetchall():
                    error = {
                        "code": "server_restarted",
                        "detail": "服务重启中断了这次任务，原行程已保留。请重新提交；已完成的任务仍可恢复。",
                    }
                    db.execute(
                        "UPDATE tasks SET status='failed',error=?,updated=? WHERE id=?",
                        (_json(error), time.time(), row["id"]),
                    )
                    self._append(db, row["id"], "failed", error)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def fingerprint(kind: str, payload: dict) -> str:
        return hashlib.sha256(_json([kind, payload]).encode()).hexdigest()

    def existing(self, key: str, fingerprint: str) -> str | None:
        if not key:
            return None
        with self.lock, self._db() as db:
            row = db.execute(
                "SELECT id,fingerprint FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
        if row and row["fingerprint"] != fingerprint:
            raise TaskConflict("同一个提交标识对应了不同内容，请刷新后重试。")
        return row["id"] if row else None

    def create(self, kind: str, payload: dict, key: str = "") -> str:
        task_id, now = uuid.uuid4().hex, time.time()
        with self.lock, self._db() as db:
            db.execute(
                "INSERT INTO tasks(id,kind,payload,fingerprint,idempotency_key,status,created,updated) VALUES (?,?,?,?,?,'pending',?,?)",
                (
                    task_id,
                    kind,
                    _json(payload),
                    self.fingerprint(kind, payload),
                    key or None,
                    now,
                    now,
                ),
            )
            self._append(
                db,
                task_id,
                "progress",
                {
                    "stage": "queued",
                    "percent": 3,
                    "title": "已加入规划队列",
                    "detail": "任务已保存，刷新页面后仍可继续查看。",
                },
            )
        return task_id

    @staticmethod
    def _append(db, task_id: str, name: str, data: dict):
        seq = db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO task_events VALUES(?,?,?,?,?)",
            (task_id, seq, name, _json(data), time.time()),
        )

    def progress(self, task_id: str, data: dict):
        with self.lock, self._db() as db:
            row = db.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row and row["status"] not in TERMINAL:
                self._append(db, task_id, "progress", data)
                db.execute(
                    "UPDATE tasks SET updated=? WHERE id=?", (time.time(), task_id)
                )

    def running(self, task_id: str):
        with self.lock, self._db() as db:
            db.execute(
                "UPDATE tasks SET status='running',updated=? WHERE id=?",
                (time.time(), task_id),
            )

    def finish(
        self,
        task_id: str,
        *,
        result: dict | None = None,
        error: dict | None = None,
        trace: dict | None = None,
    ):
        status, event = ("failed", "failed") if error else ("completed", "complete")
        with self.lock, self._db() as db:
            db.execute(
                "UPDATE tasks SET status=?,updated=?,result=?,error=?,trace=? WHERE id=?",
                (
                    status,
                    time.time(),
                    _json(result) if result is not None else None,
                    _json(error) if error else None,
                    _json(trace or {}),
                    task_id,
                ),
            )
            self._append(db, task_id, event, error if error else result)

    def get(self, task_id: str) -> dict | None:
        with self.lock, self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for name in ("payload", "result", "error", "trace"):
            result[name] = json.loads(result[name]) if result[name] else None
        result.pop("fingerprint")
        result.pop("idempotency_key")
        return result

    def by_submission(self, key: str) -> dict | None:
        with self.lock, self._db() as db:
            row = db.execute(
                "SELECT id FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
        return self.get(row["id"]) if row else None

    def events(self, task_id: str, after: int = 0) -> list[dict]:
        with self.lock, self._db() as db:
            rows = db.execute(
                "SELECT seq,name,data,created FROM task_events WHERE task_id=? AND seq>? ORDER BY seq",
                (task_id, after),
            ).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def cleanup(self, ttl: float = 86400):
        with self.lock, self._db() as db:
            db.execute(
                "DELETE FROM tasks WHERE status IN ('completed','failed') AND updated<?",
                (time.time() - ttl,),
            )
            db.execute(
                "DELETE FROM tasks WHERE id IN (SELECT id FROM tasks WHERE status IN ('completed','failed') ORDER BY updated DESC LIMIT -1 OFFSET 200)"
            )


class TaskManager:
    def __init__(self, store: TaskStore, runner: Callable, capacity: int = 4):
        self.store, self.runner = store, runner
        self.jobs: queue.Queue = queue.Queue()
        self.capacity = threading.BoundedSemaphore(capacity)
        self.start_lock = threading.Lock()
        self.worker: threading.Thread | None = None

    def submit(self, kind: str, payload: dict, key: str = "") -> str:
        with self.start_lock:
            self.store.cleanup()
            existing = self.store.existing(key, self.store.fingerprint(kind, payload))
            if existing:
                return existing
            if not self.capacity.acquire(blocking=False):
                raise TaskBusy(
                    "当前已有多个任务正在处理，请稍后再试；这次请求尚未调用模型。"
                )
            try:
                task_id = self.store.create(kind, payload, key)
                self.jobs.put((task_id, kind, payload))
                if self.worker is None or not self.worker.is_alive():
                    self.worker = threading.Thread(
                        target=self._work, name="travel-task-worker", daemon=True
                    )
                    self.worker.start()
                return task_id
            except Exception:
                self.capacity.release()
                raise

    def _work(self):
        while True:
            task_id, kind, payload = self.jobs.get()
            recorder = RunRecorder()
            try:
                self.store.running(task_id)
                with recording(recorder):
                    result = self.runner(
                        kind, payload, lambda data: self.store.progress(task_id, data)
                    )
                self.store.finish(task_id, result=result, trace=recorder.snapshot())
            except Exception as exc:
                self.store.finish(
                    task_id, error=public_error(exc), trace=recorder.snapshot()
                )
            finally:
                self.capacity.release()
                self.jobs.task_done()

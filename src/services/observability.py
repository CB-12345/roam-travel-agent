"""Small local run recorder. Never retain prompts, tool arguments or provider bodies."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable


class RunRecorder:
    def __init__(self):
        self.started = time.perf_counter()
        self.events: list[dict[str, Any]] = []
        self.totals = dict(
            llm_calls=0,
            tool_calls=0,
            http_calls=0,
            failed_calls=0,
            input_tokens=0,
            output_tokens=0,
            token_reports=0,
        )
        self.lock = threading.Lock()

    def record(
        self, kind: str, name: str, duration: float, ok: bool, usage: dict | None = None
    ):
        with self.lock:
            counter = {
                "llm": "llm_calls",
                "tool": "tool_calls",
                "http": "http_calls",
            }.get(kind)
            if counter:
                self.totals[counter] += 1
            self.totals["failed_calls"] += int(not ok)
            event = {
                "kind": kind,
                "name": name[:100],
                "duration_ms": round(duration * 1000, 2),
                "ok": ok,
            }
            if kind == "llm" and usage:
                for key in ("input_tokens", "output_tokens"):
                    value = usage.get(key)
                    if (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and value >= 0
                    ):
                        self.totals[key] += value
                        event[key] = value
                if "input_tokens" in event or "output_tokens" in event:
                    self.totals["token_reports"] += 1
            if len(self.events) < 256:
                self.events.append(event)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "summary": {
                    **self.totals,
                    "elapsed_seconds": round(time.perf_counter() - self.started, 3),
                },
                "events": [dict(item) for item in self.events],
                "note": "Token 仅累计提供方实际返回值；并行调用耗时不能相加作为总耗时。",
            }


_recorder: ContextVar[RunRecorder | None] = ContextVar(
    "travel_run_recorder", default=None
)


@contextmanager
def recording(recorder: RunRecorder):
    token = _recorder.set(recorder)
    try:
        yield recorder
    finally:
        _recorder.reset(token)


def observed_call(kind: str, name: str, call: Callable[[], Any]) -> Any:
    recorder = _recorder.get()
    if recorder is None:
        return call()
    started = time.perf_counter()
    ok, usage = False, None
    try:
        result = call()
        ok = bool(getattr(result, "ok", True))
        if kind == "llm":
            usage = getattr(result, "usage_metadata", None)
            if not isinstance(usage, dict):
                metadata = getattr(result, "response_metadata", {}) or {}
                reported = (
                    metadata.get("token_usage", {})
                    if isinstance(metadata, dict)
                    else {}
                )
                reported = reported if isinstance(reported, dict) else {}
                usage = {
                    "input_tokens": reported.get("prompt_tokens"),
                    "output_tokens": reported.get("completion_tokens"),
                }
        return result
    finally:
        recorder.record(kind, name, time.perf_counter() - started, ok, usage)

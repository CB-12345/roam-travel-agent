from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.services.observability import RunRecorder, observed_call, recording
from src.services.tasks import TaskBusy, TaskConflict, TaskManager, TaskStore


def test_reopen_preserves_result_and_explicitly_fails_unfinished(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = TaskStore(path)
    done = store.create("plan", {"city": "揭阳"}, "once")
    store.finish(done, result={"plan": {"city": "揭阳"}})
    interrupted = store.create("revision", {})
    store.running(interrupted)
    reopened = TaskStore(path)
    assert reopened.by_submission("once")["result"]["plan"]["city"] == "揭阳"
    assert reopened.get(interrupted)["error"]["code"] == "server_restarted"
    assert reopened.events(interrupted)[-1]["name"] == "failed"
    assert reopened.events(done) == store.events(done)


def test_capacity_and_idempotency_do_not_duplicate_model_work(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def runner(kind, payload, progress):
        calls.append(payload)
        entered.set()
        assert release.wait(5)
        return {"plan": payload}

    store = TaskStore(tmp_path / "tasks.db")
    manager = TaskManager(store, runner, capacity=1)
    try:
        task_id = manager.submit("plan", {"city": "揭阳"}, "same")
        assert entered.wait(3)
        assert manager.submit("plan", {"city": "揭阳"}, "same") == task_id
        with pytest.raises(TaskConflict):
            manager.submit("plan", {"city": "杭州"}, "same")
        with pytest.raises(TaskBusy):
            manager.submit("plan", {"city": "杭州"}, "different")
    finally:
        release.set()
    manager.jobs.join()
    assert len(calls) == 1
    assert store.get(task_id)["status"] == "completed"


def test_failure_redacts_provider_secrets_and_releases_capacity(tmp_path):
    def runner(*args):
        raise RuntimeError(
            "Authorization: Bearer sk-private https://provider.invalid?key=secret"
        )

    store = TaskStore(tmp_path / "tasks.db")
    manager = TaskManager(store, runner, capacity=1)
    task_id = manager.submit("plan", {})
    manager.jobs.join()
    task = store.get(task_id)
    assert task["status"] == "failed"
    assert "sk-private" not in json.dumps(task["error"])
    assert "provider.invalid" not in json.dumps(task["error"])
    second = manager.submit("plan", {})
    manager.jobs.join()
    assert store.get(second)["status"] == "failed"


def test_sse_multiple_readers_replay_and_cursor(monkeypatch, tmp_path):
    import src.app as module

    store = TaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(module, "task_store", store)
    task_id = store.create("plan", {})
    store.progress(task_id, {"stage": "research", "percent": 50})
    store.finish(task_id, result={"plan": {"city": "揭阳"}})
    client = TestClient(module.app)
    url = f"/api/plan/stream/{task_id}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: client.get(url).text, range(2)))
    assert responses[0] == responses[1]
    assert responses[0].count("event: complete") == 1
    tail = client.get(url, headers={"Last-Event-ID": "2"}).text
    assert "id: 3" in tail and "event: progress" not in tail
    assert client.get(f"/api/plan/tasks/{task_id}").json()["status"] == "completed"
    assert "payload" not in client.get(f"/api/plan/tasks/{task_id}/trace").json()


def test_cleanup_removes_only_terminal_records(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    active = store.create("plan", {})
    done = store.create("plan", {})
    store.finish(done, result={})
    store.cleanup(ttl=-1)
    assert store.get(done) is None and store.events(done) == []
    assert store.get(active)["status"] == "pending"


def test_parallel_telemetry_counts_only_reported_tokens():
    recorder = RunRecorder()
    response = SimpleNamespace(
        usage_metadata={"input_tokens": 12, "output_tokens": 4}, content="secret prompt"
    )
    with recording(recorder), ThreadPoolExecutor(max_workers=3) as executor:
        jobs = [
            executor.submit(
                copy_context().run, observed_call, "llm", "test", lambda: response
            )
            for _ in range(3)
        ]
        for job in jobs:
            job.result()
        observed_call("llm", "no-usage", lambda: SimpleNamespace(content="unreported"))
    snapshot = recorder.snapshot()
    assert snapshot["summary"]["llm_calls"] == 4
    assert snapshot["summary"]["token_reports"] == 3
    assert snapshot["summary"]["input_tokens"] == 36
    assert "secret prompt" not in json.dumps(snapshot)


def test_failed_bound_model_call_never_repeats_as_unbound():
    from src.llm import LangChainRuntime

    runtime = object.__new__(LangChainRuntime)
    runtime.settings = SimpleNamespace(
        provider="test", model="test", enabled=True, api_key="test"
    )
    runtime._llm = Mock()
    runtime._build_tool_schemas = Mock(return_value=[{"type": "function"}])
    bound = runtime._llm.bind_tools.return_value
    bound.invoke.side_effect = TimeoutError("private provider timeout")
    recorder = RunRecorder()
    with recording(recorder), pytest.raises(TimeoutError):
        runtime._invoke_tool_loop([], Mock())
    assert bound.invoke.call_count == 1
    runtime._llm.invoke.assert_not_called()
    assert recorder.snapshot()["summary"]["failed_calls"] == 1

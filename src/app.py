from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.config import BASE_DIR, DATA_DIR, STATIC_DIR, ensure_data_dirs, get_settings
from src.models import HealthResponse, TravelRequest, TripPlan
from src.models_backup import TripBackup
from src.models_revision import TravelRevisionRequest, TravelRevisionResponse
from src.models_place_edits import (
    PlaceCandidateRequest,
    PlaceCandidatesResponse,
    PlaceReplacementRequest,
)
from src.services.errors import public_error
from src.services.place_edits import PlaceEditService
from src.services.planner import TravelPlannerService
from src.services.revision import TravelRevisionService
from src.services.places import PlaceLookupRequest, PlaceLookupResponse
from src.services.quality import QualityReport, QualityRequest, accept_plan, audit_plan
from src.services.tasks import TERMINAL, TaskBusy, TaskConflict, TaskManager, TaskStore

settings = get_settings()
ensure_data_dirs()
planner = TravelPlannerService()
revision_service = TravelRevisionService(planner)
place_service = planner.place_service
place_edit_service = PlaceEditService(planner, place_service)
planner_lock = threading.Lock()
task_store = TaskStore(
    Path(os.getenv("TRAVEL_TASK_DB", str(DATA_DIR / "local-runtime" / "tasks.sqlite3")))
)


def _run_task(kind: str, payload: dict, progress) -> dict:
    with planner_lock:
        if kind == "replacement":
            request = PlaceReplacementRequest.model_validate(payload)
            result = place_edit_service.replace(request, progress_callback=progress)
        elif kind == "revision":
            request = TravelRevisionRequest.model_validate(payload)
            result = revision_service.revise(request, progress_callback=progress)
        else:
            request = TravelRequest.model_validate(payload)
            result = planner.build_trip_plan(request, progress_callback=progress)
        if isinstance(result, TravelRevisionResponse):
            if result.status == "revised":
                accept_plan(result.request, result.plan)
            key = "revision"
        else:
            accept_plan(request, result)
            key = "plan"
    return {
        "percent": 100,
        "title": "行程已整理完成",
        "detail": "每日安排、预算和待确认事项已整理完成。",
        key: result.model_dump(mode="json"),
    }


task_manager = TaskManager(task_store, _run_task)
app = FastAPI(title=settings.app_name, version="2.0.0")


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    response = planner.health()
    if planner.settings.amap_key:
        response.available_tools.append("live_amap")
    if planner.settings.qweather_key and planner.settings.qweather_host:
        response.available_tools.append("live_qweather")
    return response


@app.post("/api/plan", response_model=TripPlan)
def create_trip_plan(request: TravelRequest) -> TripPlan:
    try:
        with planner_lock:
            result = planner.build_trip_plan(request)
            accept_plan(request, result)
            return result
    except Exception as exc:
        error = public_error(exc)
        raise HTTPException(
            status_code=422 if isinstance(exc, ValueError) else 503,
            detail=error["detail"],
        ) from None


def _start_task(kind: str, request, idempotency_key: str | None) -> dict[str, str]:
    if idempotency_key and (
        len(idempotency_key) > 100
        or not all(c.isalnum() or c in "-_" for c in idempotency_key)
    ):
        raise HTTPException(status_code=422, detail="提交标识格式不正确。")
    try:
        task_id = task_manager.submit(
            kind, request.model_dump(mode="json"), idempotency_key or ""
        )
        return {"task_id": task_id}
    except TaskBusy as exc:
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": "10"}
        ) from None
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@app.post("/api/plan/tasks")
def create_plan_task(
    request: TravelRequest, idempotency_key: str | None = Header(default=None)
) -> dict[str, str]:
    return _start_task("plan", request, idempotency_key)


@app.post("/api/plan/revisions/tasks")
def create_revision_task(
    request: TravelRevisionRequest, idempotency_key: str | None = Header(default=None)
) -> dict[str, str]:
    return _start_task("revision", request, idempotency_key)


@app.post("/api/plan/places/replace/tasks")
def replace_place_task(
    request: PlaceReplacementRequest, idempotency_key: str | None = Header(default=None)
) -> dict[str, str]:
    return _start_task("replacement", request, idempotency_key)


@app.post("/api/places/resolve", response_model=PlaceLookupResponse)
def resolve_places(request: PlaceLookupRequest) -> PlaceLookupResponse:
    # PlaceService has its own lock and does not mutate planner state.
    return place_service.resolve(request)


@app.post("/api/plan/places/candidates", response_model=PlaceCandidatesResponse)
def place_candidates(request: PlaceCandidateRequest) -> PlaceCandidatesResponse:
    try:
        with planner_lock:
            return place_edit_service.candidates(request)
    except Exception as exc:
        raise HTTPException(
            status_code=422 if isinstance(exc, ValueError) else 503,
            detail=public_error(exc)["detail"],
        ) from None


@app.post("/api/plan/quality", response_model=QualityReport)
def plan_quality(request: QualityRequest) -> QualityReport:
    return audit_plan(request.request, request.plan)


@app.post("/api/trips/import", response_model=TripBackup)
def import_trip(backup: TripBackup) -> TripBackup:
    try:
        backup.validate_context()
        json.dumps(backup.model_dump(mode="json"), allow_nan=False)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="备份中的城市、日期、住宿或数值不完整，请使用应用导出的原始 JSON 备份。",
        ) from None
    backup.plan.quality_report = audit_plan(backup.request, backup.plan).model_dump(
        mode="json"
    )
    return backup


@app.get("/api/demo", response_model=TripBackup)
def demo_trip() -> TripBackup:
    return TripBackup.model_validate_json(
        (BASE_DIR / "examples" / "jieyang-recorded.json").read_text(encoding="utf-8")
    )


@app.get("/api/plan/submissions/{submission_key}")
def submission_task(submission_key: str) -> dict:
    task_store.cleanup()
    task = task_store.by_submission(submission_key)
    if task is None:
        raise HTTPException(
            status_code=404, detail="未找到这次提交，可以重新规划；当前行程已保留。"
        )
    return task


def _task_or_404(task_id: str) -> dict:
    task_store.cleanup()
    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail="任务不存在或已过期；已完成的行程仍可从本地历史打开。",
        )
    return task


@app.get("/api/plan/tasks/{task_id}")
def task_snapshot(task_id: str) -> dict:
    return _task_or_404(task_id)


@app.get("/api/plan/tasks/{task_id}/trace")
def task_trace(task_id: str) -> dict:
    task = _task_or_404(task_id)
    return {
        "task_id": task_id,
        "kind": task["kind"],
        "status": task["status"],
        "created": task["created"],
        "updated": task["updated"],
        "error": task["error"],
        "run": task["trace"],
        "stages": [
            {"seq": e["seq"], "at": e["created"], **e["data"]}
            for e in task_store.events(task_id)
            if e["name"] == "progress"
        ],
    }


@app.get("/api/plan/stream/{task_id}")
def stream_plan_task(
    task_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    _task_or_404(task_id)
    cursor = max(
        after,
        (
            int(last_event_id)
            if last_event_id and last_event_id.isdigit() and len(last_event_id) < 12
            else 0
        ),
    )
    return StreamingResponse(
        _event_stream(task_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _event_stream(task_id: str, cursor: int):
    heartbeat = 0
    while True:
        events = task_store.events(task_id, cursor)
        for event in events:
            cursor = event["seq"]
            yield f"id: {cursor}\nevent: {event['name']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
            if event["name"] in {"complete", "failed"}:
                return
        task = task_store.get(task_id)
        if task is None or (task["status"] in TERMINAL and not events):
            return
        heartbeat += 1
        if heartbeat >= 50:
            heartbeat = 0
            yield ": heartbeat\n\n"
        await asyncio.sleep(0.3)


app.mount(
    "/",
    StaticFiles(directory=STATIC_DIR or BASE_DIR / "static", html=True),
    name="static",
)

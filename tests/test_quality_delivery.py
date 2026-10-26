from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.models import TravelRequest, TripPlan
from src.models_backup import TripBackup
from src.services.quality import accept_plan, audit_plan

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def recorded():
    return TripBackup.model_validate_json(
        (ROOT / "examples/jieyang-recorded.json").read_text(encoding="utf-8")
    )


def test_offline_quality_fault_injection_suite(recorded):
    cases = json.loads(
        (ROOT / "tests/fixtures/quality-cases.json").read_text(encoding="utf-8")
    )
    for case in cases:
        value = recorded.model_dump(mode="json")
        for path, replacement in case.get("edits", []):
            parts = path.split(".")
            parent = value
            for part in parts[:-1]:
                parent = parent[int(part)] if isinstance(parent, list) else parent[part]
            key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
            parent[key] = replacement
        report = audit_plan(
            TravelRequest.model_validate(value["request"]),
            TripPlan.model_validate(value["plan"]),
        )
        found = {issue.code for issue in report.issues}
        assert set(case.get("expect", [])) <= found, case["id"]
        assert not set(case.get("absent", [])) & found, case["id"]
        assert report.errors == case.get("errors", report.errors), case["id"]
        assert report.status == case.get("status", report.status), case["id"]


def test_acceptance_rejects_inconsistent_budget_but_preserves_uncertainty(recorded):
    report = accept_plan(recorded.request, recorded.plan)
    assert report.errors == 0 and report.warnings >= 1
    recorded.plan.budget.total += 100
    with pytest.raises(ValueError, match="行程验收未通过"):
        accept_plan(recorded.request, recorded.plan)


def test_backup_roundtrip_preserves_context_and_does_not_silently_repair(recorded):
    import src.app as module

    client = TestClient(module.app)
    recorded.plan.daily_plans[0].items[3].time_range = "09:00-09:05"
    response = client.post("/api/trips/import", json=recorded.model_dump(mode="json"))
    assert response.status_code == 200
    backup = response.json()
    assert backup["plan"]["daily_plans"][0]["items"][3]["time_range"] == "09:00-09:05"
    assert backup["plan"]["quality_report"]["errors"] > 0
    assert len(backup["place_details"]) == len(recorded.place_details)


def test_import_rejects_invalid_context_and_nonfinite_numbers(recorded):
    import src.app as module

    client = TestClient(module.app)
    backup = recorded.model_dump(mode="json")
    backup["request"]["city"] = "杭州"
    assert client.post("/api/trips/import", json=backup).status_code == 422
    backup = recorded.model_dump(mode="json")
    backup["plan"]["budget"]["total"] = float("nan")
    response = client.post(
        "/api/trips/import",
        content=json.dumps(backup),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_demo_endpoint_never_calls_model_or_providers(monkeypatch):
    import src.app as module

    def forbidden(*args, **kwargs):
        raise AssertionError("recorded demo must not call providers")

    monkeypatch.setattr(module.planner, "build_trip_plan", forbidden)
    monkeypatch.setattr(module.place_service, "resolve", forbidden)
    response = TestClient(module.app).get("/api/demo")
    assert response.status_code == 200
    backup = TripBackup.model_validate(response.json())
    assert backup.mode == "recorded_demo" and backup.recorded_at == "2026-10-24"
    assert audit_plan(backup.request, backup.plan).errors == 0

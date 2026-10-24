"""Opt-in real provider smoke tests against a running app; consumes configured API quota.

No keys are accepted or printed. Run explicitly; never included in offline CI.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models import TravelRequest, TripPlan
from src.services.errors import public_error
from src.services.quality import audit_plan
from src.services.stop_locks import assert_locks_preserved


def inspect_generation(
    request: dict, raw: dict, *, unknown_weather: bool = False
) -> dict:
    plan = TripPlan.model_validate(raw)
    report = audit_plan(TravelRequest.model_validate(request), plan)
    errors = [i.message for i in report.issues if i.severity == "error"]
    if plan.planning_source != "llm_generated":
        errors.append("实际结果没有使用模型生成。")
    if not plan.selected_attractions:
        errors.append("没有安排具体景点。")
    if unknown_weather and any(
        d.weather.high_c is not None or d.weather.low_c is not None
        for d in plan.daily_plans
    ):
        errors.append("远期天气被填入了未核实的温度。")
    return {
        "passed": not errors,
        "errors": errors,
        "quality": report.model_dump(mode="json"),
        "planning_source": plan.planning_source,
        "weather_data_source": plan.weather_data_source,
        "budget_total": plan.budget.total,
        "attractions": [p.name for p in plan.selected_attractions],
    }


def inspect_revision(original: dict, result: dict) -> dict:
    errors = []
    if result.get("status") != "revised":
        return {
            "passed": False,
            "errors": ["指定日期修改未完成。"],
            "status": result.get("status"),
        }
    plan = result["plan"]
    if plan["daily_plans"][0] != original["daily_plans"][0]:
        errors.append("未指定修改的第一天发生了变化。")
    if sum(i["item_type"] == "attraction" for i in plan["daily_plans"][1]["items"]) > 1:
        errors.append("第二天没有保留最多一个景点的限制。")
    try:
        assert_locks_preserved(
            TripPlan.model_validate(original), TripPlan.model_validate(plan)
        )
    except ValueError:
        errors.append("锁定的地点或日期发生变化。")
    check = inspect_generation(result["request"], plan)
    return {
        **check,
        "passed": not errors and check["passed"],
        "errors": errors + check["errors"],
        "unchanged_first_day": plan["daily_plans"][0] == original["daily_plans"][0],
        "status": result["status"],
    }


def run_task(base: str, endpoint: str, payload: dict, label: str, timeout: int) -> dict:
    key = "smoke-" + uuid.uuid4().hex
    for attempt in range(2):
        try:
            response = requests.post(
                base + endpoint,
                json=payload,
                headers={"Idempotency-Key": key},
                timeout=20,
            )
            response.raise_for_status()
            task_id = response.json()["task_id"]
            break
        except requests.RequestException:
            if attempt:
                raise RuntimeError(
                    "测试任务提交状态无法确认；请检查本地服务任务状态。"
                ) from None
    deadline, last_status = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        snapshot = requests.get(base + "/api/plan/tasks/" + task_id, timeout=20)
        snapshot.raise_for_status()
        task = snapshot.json()
        if task["status"] != last_status:
            print(f"{label}: {task['status']}", flush=True)
            last_status = task["status"]
        if task["status"] in {"completed", "failed"}:
            return task
        time.sleep(1)
    raise RuntimeError("测试超时，任务可能仍在后台运行，请通过任务快照确认。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/live-cases.json")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "evals/live-matrix-report.json"
    )
    parser.add_argument(
        "--artifacts-dir", type=Path, default=ROOT / "data/local-runtime/live-smoke"
    )
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    report = {
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Explicit real provider smoke cases. Small convenience sample, not general accuracy or availability.",
        "cases": [],
        "total": len(cases),
        "passed": 0,
        "attempts": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def save():
        report["passed"] = sum(c["passed"] for c in report["cases"])
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    for case in cases:
        start = date.today() + timedelta(days=case.get("offset", 1))
        request = {k: v for k, v in case.items() if k in TravelRequest.model_fields}
        request.update(
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=case["days"] - 1)).isoformat(),
        )
        record = {
            "id": case["id"],
            "description": case["description"],
            "request": request,
            "passed": False,
        }
        report["attempts"] += 1
        try:
            task = run_task(
                args.base_url, "/api/plan/tasks", request, case["id"], args.timeout
            )
            (args.artifacts_dir / (case["id"] + ".json")).write_text(
                json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            record["task_status"] = task["status"]
            record["run"] = (task.get("trace") or {}).get("summary", {})
            if task["status"] != "completed":
                record["errors"] = [task.get("error", {}).get("detail", "生成失败")]
            else:
                plan = task["result"]["plan"]
                record.update(
                    inspect_generation(
                        request,
                        plan,
                        unknown_weather=case.get("expect_unknown_weather", False),
                    )
                )
                if case.get("check_revision") and record["passed"]:
                    locked = copy.deepcopy(plan)
                    first = next(
                        i
                        for i in locked["daily_plans"][0]["items"]
                        if i["item_type"] == "attraction"
                    )
                    locked["place_locks"] = [
                        {
                            "kind": "attraction",
                            "day_index": 1,
                            "name": first["location_name"],
                            "address": first["location_address"],
                        }
                    ]
                    report["attempts"] += 1
                    revision = run_task(
                        args.base_url,
                        "/api/plan/revisions/tasks",
                        {
                            "request": request,
                            "plan": locked,
                            "message": "只修改第2天，最多安排1个景点，保持第1天的全部安排及锁定地点不变。",
                            "history": [],
                            "day_rules": {},
                        },
                        case["id"] + "/revision",
                        args.timeout,
                    )
                    (args.artifacts_dir / (case["id"] + "-revision.json")).write_text(
                        json.dumps(revision, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    if revision["status"] == "completed":
                        record["revision"] = inspect_revision(
                            locked, revision["result"]["revision"]
                        )
                    else:
                        record["revision"] = {
                            "passed": False,
                            "errors": [
                                revision.get("error", {}).get("detail", "修改失败")
                            ],
                        }
                    record["revision"]["run"] = (revision.get("trace") or {}).get(
                        "summary", {}
                    )
                    record["passed"] = record["passed"] and record["revision"]["passed"]
        except Exception as exc:
            record["errors"] = [public_error(exc)["detail"]]
        report["cases"].append(record)
        save()
        print(f"{case['id']}: {'PASS' if record['passed'] else 'FAIL'}", flush=True)
    print(
        f"Live smoke: {report['passed']}/{report['total']} scenarios passed; see {args.output.name}",
        flush=True,
    )
    return int(report["passed"] != report["total"])


if __name__ == "__main__":
    raise SystemExit(main())

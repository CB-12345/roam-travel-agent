"""Independent, deterministic acceptance checks; no model calls or subjective score."""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from src.models import TravelRequest, TripPlan
from src.services.opening_hours import check_opening_visit
from src.services.planning_quality import time_bounds, valid_location
from src.services.stop_locks import same_place, validate_locks


class QualityIssue(BaseModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    day_index: int | None = None


class QualityReport(BaseModel):
    version: str = "1.0"
    status: Literal["passed", "needs_confirmation", "invalid"]
    checked_at: str
    errors: int
    warnings: int
    metrics: dict[str, int | float]
    issues: list[QualityIssue] = Field(default_factory=list)
    note: str = (
        "检查日期、时间、账目和资料完整度；不代表实时营业、预约库存或实际成交价保证。"
    )


class QualityRequest(BaseModel):
    request: TravelRequest
    plan: TripPlan


def audit_plan(request: TravelRequest, plan: TripPlan) -> QualityReport:
    issues: list[QualityIssue] = []

    def issue(
        code: str, message: str, day: int | None = None, *, warning: bool = False
    ):
        # A rule produces one concise finding per day, even for several bad items.
        if not any(i.code == code and i.day_index == day for i in issues):
            issues.append(
                QualityIssue(
                    code=code,
                    severity="warning" if warning else "error",
                    message=message,
                    day_index=day,
                )
            )

    expected = [
        request.start_date + timedelta(days=i) for i in range(request.trip_days)
    ]
    if (
        plan.city != request.city
        or plan.trip_days != request.trip_days
        or [d.date for d in plan.daily_plans] != expected
    ):
        issue("dates", "城市或每日日期与旅行需求不一致。")
    catalog = {p.name: p for p in plan.selected_attractions}
    used: set[str] = set()
    stops = coordinates = meals = concrete_meals = fits = opening_unknown = (
        weather_unknown
    ) = 0
    for day_index, day in enumerate(plan.daily_plans, 1):
        cursor = 7 * 60
        counts: Counter = Counter()
        if day.weather.date != day.date:
            issue("weather_date", "天气日期与旅行日期不一致。", day_index)
        if day.weather.high_c is None or day.weather.low_c is None:
            weather_unknown += 1
        for item in day.items:
            bounds = time_bounds(item.time_range)
            if (
                bounds is None
                or bounds[0] < cursor
                or bounds[0] < 420
                or bounds[1] > 1320
            ):
                issue(
                    "schedule",
                    "存在时间格式错误、重叠或超出 07:00–22:00 的安排。",
                    day_index,
                )
            if bounds:
                cursor = max(
                    cursor,
                    bounds[1]
                    + (
                        10
                        if item.item_type == "transport" and item.duration_min > 0
                        else 0
                    ),
                )
                if (
                    item.item_type == "transport"
                    and bounds[1] - bounds[0] != item.duration_min
                ):
                    issue(
                        "transport_duration",
                        "交通时段与实际交通耗时不一致。",
                        day_index,
                    )
            name = item.location_name or item.title
            point = catalog.get(name) if item.item_type == "attraction" else None
            if item.item_type == "attraction":
                used.add(name)
                if point is None:
                    issue("grounding", "每日景点包含候选总览之外的地点。", day_index)
                elif bounds and bounds[1] - bounds[0] < math.ceil(
                    max(0.5, point.recommended_hours) * 60
                ):
                    issue(
                        "visit_duration",
                        f"「{name}」的安排短于完整建议游玩时长。",
                        day_index,
                    )
            is_meal = item.item_type in {"meal", "food"}
            if is_meal:
                meals += 1
                counts[item.meal_type] += 1
                concrete_meals += int(item.is_route_stop)
                window = {
                    "breakfast": (420, 600),
                    "lunch": (660, 870),
                    "dinner": (1020, 1230),
                }.get(item.meal_type)
                if (
                    bounds
                    and window
                    and (bounds[0] < window[0] or bounds[1] > window[1])
                ):
                    issue("meal_window", "用餐安排超出正常三餐时段。", day_index)
            if item.item_type == "attraction" or (is_meal and item.is_route_stop):
                stops += 1
                location = (
                    item.location
                    if valid_location(item.location)
                    else point.location if point else None
                )
                coordinates += int(valid_location(location))
                check = item.opening_check
                # Always evaluate the final date/time, never trust a cached verdict.
                check = (
                    check_opening_visit(check.raw_text, day.date, item.time_range)
                    if check
                    else None
                )
                if check and check.status in {"conflict", "closed"}:
                    issue(
                        "opening_conflict",
                        f"「{name}」与可核实的开放时段冲突。",
                        day_index,
                    )
                elif check and check.status == "fits":
                    fits += 1
                else:
                    opening_unknown += 1
            if not math.isfinite(item.estimated_cost) or item.estimated_cost < 0:
                issue("item_cost", "地点费用包含无效或负数金额。", day_index)
        missing = [
            label
            for key, label in (
                ("breakfast", "早餐"),
                ("lunch", "午餐"),
                ("dinner", "晚餐"),
            )
            if not counts[key]
        ]
        if missing:
            issue(
                "meal_missing",
                f"尚未安排{'、'.join(missing)}。",
                day_index,
                warning=True,
            )
        if any(count > 1 for count in counts.values()):
            issue("meal_duplicate", "同一天存在重复餐别。", day_index)
    if set(catalog) != used:
        issue("attraction_overview", "景点总览与每日实际安排不一致。")
    if not used:
        issue("no_attractions", "尚未安排具体景点。", warning=True)
    if stops > coordinates:
        issue(
            "coordinates_missing",
            f"{stops - coordinates} 处地点尚缺可靠坐标，出发前请核实具体位置。",
            warning=True,
        )
    if meals > concrete_meals:
        issue(
            "meal_unresolved",
            f"{meals - concrete_meals} 餐仍是区域建议，尚未确定具体店铺。",
            warning=True,
        )
    if opening_unknown:
        issue(
            "opening_unknown",
            f"{opening_unknown} 处地点的开放时间需要进一步确认。",
            warning=True,
        )
    if weather_unknown:
        issue(
            "weather_unavailable",
            f"{weather_unknown} 天尚无可用天气预报。",
            warning=True,
        )

    stays = plan.daily_stays
    if len(stays) != len(expected) or any(
        stay.day_index != i or stay.date != expected[i - 1]
        for i, stay in enumerate(stays, 1)
        if i <= len(expected)
    ):
        issue("stay_dates", "每日住宿链未覆盖全部旅行日期。")
    for index, stay in enumerate(stays):
        if not stay.start_hotel or not stay.end_hotel:
            issue("hotel_missing", "尚未确定完整的每日住宿。", index + 1, warning=True)
        if index and stay.start_hotel and stays[index - 1].end_hotel:
            previous = stays[index - 1].end_hotel
            if not same_place(
                previous.name,
                previous.location.address,
                stay.start_hotel.name,
                stay.start_hotel.location.address,
            ):
                issue("stay_chain", "前一晚住宿与次日出发酒店不一致。", index + 1)
    if sum(stay.charged_night for stay in stays) != request.stay_nights:
        issue("stay_nights", "实际收费住宿晚数与旅行需求不一致。")
    budget = plan.budget
    amounts = [
        budget.hotel,
        budget.attractions,
        budget.food,
        budget.transport,
        budget.contingency,
        budget.total,
    ]
    if any(not math.isfinite(value) or value < 0 for value in amounts):
        issue("budget_invalid", "预算包含无效或负数金额。")
    elif abs(round(sum(amounts[:-1]), 2) - budget.total) > 0.02:
        issue("budget_sum", "预算总额与各类别金额相加不一致。")
    rooms = math.ceil(request.travelers / 2)
    ledger = {
        "hotel": sum(
            stay.end_hotel.nightly_price * rooms
            for stay in stays
            if stay.charged_night and stay.end_hotel
        ),
        "food": sum(
            item.estimated_cost
            for day in plan.daily_plans
            for item in day.items
            if item.item_type in {"meal", "food"}
        ),
        "attractions": sum(
            item.estimated_cost
            for day in plan.daily_plans
            for item in day.items
            if item.item_type == "attraction"
        ),
        "transport": sum(day.total_transport_cost for day in plan.daily_plans),
    }
    if any(
        abs(round(value, 2) - getattr(budget, key)) > 0.02
        for key, value in ledger.items()
    ):
        issue("budget_ledger", "预算与实际安排、房间数或收费晚数不一致。")
    if budget.total > request.budget_max:
        issue(
            "budget_limit",
            f"预计费用超出预算上限 ¥{budget.total - request.budget_max:.0f}，建议调整住宿或活动。",
            warning=True,
        )
    try:
        validate_locks(plan)
    except ValueError:
        issue("locks", "存在已锁定但未保留在对应日期的地点。")
    errors = sum(i.severity == "error" for i in issues)
    warnings = len(issues) - errors
    return QualityReport(
        status="invalid" if errors else "needs_confirmation" if warnings else "passed",
        checked_at=datetime.now(timezone.utc).isoformat(),
        errors=errors,
        warnings=warnings,
        metrics={
            "days": len(plan.daily_plans),
            "stops": stops,
            "stops_with_coordinates": coordinates,
            "meals": meals,
            "concrete_meals": concrete_meals,
            "opening_fits": fits,
            "opening_unknown": opening_unknown,
            "weather_unknown_days": weather_unknown,
        },
        issues=issues,
    )


def accept_plan(request: TravelRequest, plan: TripPlan) -> QualityReport:
    report = audit_plan(request, plan)
    if report.errors:
        first = next(issue for issue in report.issues if issue.severity == "error")
        raise ValueError(
            f"行程验收未通过：{first.message}原行程不会被覆盖，请调整需求后重试。"
        )
    plan.quality_report = report.model_dump(mode="json")
    return report

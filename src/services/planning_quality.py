"""Deterministic checks shared by generation and scoped revisions."""

from __future__ import annotations

import math
import re
from typing import Any

from src.models import Attraction, DayPlan, DayPlanItem, Location
from src.models_opening_hours import DayOpeningHours


MEAL_WINDOWS = {
    "breakfast": (7 * 60, 10 * 60),
    "lunch": (11 * 60, 14 * 60 + 30),
    "dinner": (17 * 60, 20 * 60 + 30),
}
MIN_MEAL_DURATIONS = {"breakfast": 30, "lunch": 45, "dinner": 45}


def valid_location(value: Any) -> bool:
    if isinstance(value, dict):
        lat, lng = value.get("lat", 0), value.get("lng", 0)
    else:
        lat, lng = getattr(value, "lat", 0), getattr(value, "lng", 0)
    try:
        return (
            math.isfinite(float(lat))
            and math.isfinite(float(lng))
            and -90 <= float(lat) <= 90
            and -180 <= float(lng) <= 180
            and (float(lat) != 0 or float(lng) != 0)
        )
    except (TypeError, ValueError):
        return False


def distance_km(first: Location | dict, second: Location | dict) -> float:
    if not valid_location(first) or not valid_location(second):
        return math.inf
    a = first if isinstance(first, dict) else first.model_dump()
    b = second if isinstance(second, dict) else second.model_dump()
    lat1, lat2 = math.radians(float(a["lat"])), math.radians(float(b["lat"]))
    dlat = lat2 - lat1
    dlng = math.radians(float(b["lng"]) - float(a["lng"]))
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    )
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def time_bounds(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(
        r"\s*(\d{1,2}):(\d{2})\s*[-—–~至]\s*(\d{1,2}):(\d{2})\s*", value
    )
    if not match:
        return None
    h1, m1, h2, m2 = map(int, match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    start, end = h1 * 60 + m1, h2 * 60 + m2
    return (start, end) if end > start else None


def schedule_day(
    day: DayPlan,
    attractions: dict[str, Attraction],
    opening_hours: dict[int, DayOpeningHours] | None = None,
) -> bool:
    """Fit actual travel and complete activity durations into 07:00–22:00.

    Work on copies so an impossible day never leaves a half-modified schedule.
    Meal windows are planning defaults, not claims about shop opening hours.
    """
    cursor = 7 * 60
    changed = False
    replacements: list[DayPlanItem] = []
    for index, original in enumerate(day.items):
        item = original.model_copy(deep=True)
        # Provider facts replace any model- or client-supplied check.
        item.opening_check = None
        bounds = time_bounds(item.time_range)
        if item.item_type == "transport":
            if item.duration_min < 0:
                raise ValueError("交通耗时无效，请重新查询路线。")
            duration = item.duration_min
            start = cursor
        else:
            if bounds is None:
                raise ValueError(
                    f"{day.date}「{item.title}」缺少有效的起止时间，请重新规划。"
                )
            start = max(cursor, bounds[0])
            duration = bounds[1] - bounds[0]
            lower, upper = cursor, 22 * 60
            if item.item_type == "attraction":
                attraction = attractions.get(item.location_name or item.title)
                if attraction:
                    duration = max(
                        duration, math.ceil(max(attraction.recommended_hours, 0.5) * 60)
                    )
            if item.item_type in {"meal", "food"}:
                duration = max(duration, MIN_MEAL_DURATIONS.get(item.meal_type, 45))
                window = MEAL_WINDOWS.get(item.meal_type)
                if window:
                    lower, upper = max(lower, window[0]), min(upper, window[1])
                    start = max(start, window[0])
            hours = (opening_hours or {}).get(index)
            if hours and hours.status in {"known", "closed"}:
                feasible = [
                    (max(a, lower), min(b, upper) - duration)
                    for a, b in hours.intervals
                    if max(a, lower) + duration <= min(b, upper)
                ]
                if hours.status == "closed" or not feasible:
                    raise ValueError(
                        f"{day.date}「{item.location_name or item.title}」{hours.summary}，无法容纳完整停留。请更换地点或调整当日安排，原行程不会被覆盖。"
                    )
                later = [max(a, start) for a, b in feasible if max(a, start) <= b]
                start = min(later) if later else min(a for a, _ in feasible)
            if item.item_type in {"meal", "food"}:
                window = MEAL_WINDOWS.get(item.meal_type)
                if window:
                    if start + duration > window[1]:
                        raise ValueError(
                            f"{day.date} 的{dict(breakfast='早餐', lunch='午餐', dinner='晚餐')[item.meal_type]}无法在正常时段完成。请减少当天景点或选择更近的住宿，原行程不会被覆盖。"
                        )
        end = start + duration
        if end > 22 * 60:
            raise ValueError(
                f"{day.date} 的游玩、用餐与交通合计超出 07:00–22:00。请减少当天景点或调整住宿位置，原行程不会被覆盖。"
            )
        item.time_range = (
            f"{start // 60:02}:{start % 60:02}-{end // 60:02}:{end % 60:02}"
        )
        changed |= (
            item.item_type != "transport" and item.time_range != original.time_range
        )
        replacements.append(item)
        cursor = end + (10 if item.item_type == "transport" and duration > 0 else 0)
    day.items = replacements
    for intent in day.meal_intents:
        meal = next(
            (
                item
                for item in day.items
                if item.item_type in {"meal", "food"}
                and item.meal_type == intent.meal_type
            ),
            None,
        )
        if meal:
            intent.time_range = meal.time_range
    return changed

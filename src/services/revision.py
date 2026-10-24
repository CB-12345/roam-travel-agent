from __future__ import annotations

import math
from typing import Any, Callable, TYPE_CHECKING

from src.agents.revision_agent import TripRevisionAgent
from src.models import (
    Attraction,
    AttractionResearch,
    DailyStayPlan,
    HotelResearch,
    Location,
    TravelRequest,
    WeatherResearch,
)
from src.models_revision import (
    DayRevisionRule,
    RevisionChange,
    RevisionIntent,
    TravelRevisionRequest,
    TravelRevisionResponse,
)
from src.services.stop_locks import (
    assert_locks_preserved,
    resolve_target,
    validate_locks,
)
from src.services.places import PlaceLookupRequest, PlaceReference, PlaceService
from src.services.planning_quality import valid_location

if TYPE_CHECKING:
    from src.services.planner import TravelPlannerService


class TravelRevisionService:
    """Apply a validated revision transaction; never mutate the supplied plan."""

    def __init__(self, planner: TravelPlannerService):
        self.planner = planner
        self.agent = TripRevisionAgent(planner.runtime)

    def revise(
        self,
        payload: TravelRevisionRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> TravelRevisionResponse:
        p = self.planner
        validate_locks(payload.plan)
        if not p.runtime.available:
            raise ValueError("模型服务尚未就绪，暂时无法调整行程。")

        def progress(percent: int, title: str, detail: str) -> None:
            p._emit_progress(
                progress_callback,
                stage="revision",
                percent=percent,
                title=title,
                detail=detail,
            )

        progress(10, "理解你的调整", "正在结合当前行程和之前的对话确认修改范围。")
        intent = self.agent.interpret(payload)
        if intent.action != "revise":
            return TravelRevisionResponse(
                status="clarification" if intent.action == "clarify" else "answered",
                message=intent.message,
                request=payload.request,
                plan=payload.plan,
                day_rules=payload.day_rules,
            )

        days = self._target_days(intent, payload.request.trip_days)
        updates = intent.request_updates.model_dump(exclude_none=True)
        updates = {
            key: value
            for key, value in updates.items()
            if getattr(payload.request, key) != value
        }
        request = TravelRequest.model_validate(
            {**payload.request.model_dump(), **updates}
        )
        if (intent.change_hotel or "hotel_style" in updates) and any(
            lock.kind == "hotel" for lock in payload.plan.place_locks
        ):
            raise ValueError("当前住宿已锁定，请先解锁酒店，再调整住宿；原行程已保留。")
        # Global settings need consistent meals, pace, and routes on every day.
        if updates or intent.change_hotel:
            days = list(range(1, request.trip_days + 1))
        rules = {
            key: value.model_copy(deep=True) for key, value in payload.day_rules.items()
        }
        for index in days:
            key = str(index)
            rule = (
                DayRevisionRule()
                if intent.reset_day_rules
                else rules.get(key, DayRevisionRule())
            )
            rule.instruction = (rule.instruction + "\n" + payload.message).strip()[
                -600:
            ]
            if intent.indoor_only is not None:
                rule.indoor_only = intent.indoor_only
            if intent.max_attractions_per_day is not None:
                rule.max_attractions = intent.max_attractions_per_day
            rules[key] = rule

        plan = payload.plan.model_copy(deep=True)
        progress(
            25,
            "核对可用地点",
            "正在检索候选，并检查已有景点、室内要求和每天的数量限制。",
        )
        catalog, sources = self._attraction_catalog(
            request, plan.selected_attractions, intent.attraction_queries
        )
        unchanged_names = {
            item.location_name or item.title
            for index, day in enumerate(plan.daily_plans, 1)
            if index not in days
            for item in day.items
            if item.item_type == "attraction"
        }
        allowed: dict[int, list[Attraction]] = {}
        context = p._build_constraint_context(request)
        avoid = context.get("keywords_to_avoid", {}).get("attractions", [])
        for index in days:
            allowed[index] = [
                item
                for name, item in catalog.items()
                if name not in unchanged_names
                and (not rules[str(index)].indoor_only or self._is_indoor(item))
                and not p._constraint_keyword_violations(
                    f"{name} {item.category} {item.summary}", avoid
                )
            ]
            locked_names = {
                lock.name
                for lock in plan.place_locks
                if lock.kind == "attraction" and lock.day_index == index
            }
            if not locked_names.issubset({item.name for item in allowed[index]}) or (
                rules[str(index)].max_attractions
                and len(locked_names) > rules[str(index)].max_attractions
            ):
                raise ValueError(
                    f"第 {index} 天的锁定景点与本次要求冲突，请先解锁相关景点；原行程已保留。"
                )
            if not allowed[index]:
                return TravelRevisionResponse(
                    status="clarification",
                    message=f"第 {index} 天暂时没有找到符合要求、且未在其他日期安排过的可靠景点。可以放宽地点限制，或说明允许一起调整哪几天。当前行程已保留。",
                    request=payload.request,
                    plan=payload.plan,
                    day_rules=payload.day_rules,
                )

        selection = self.agent.select(
            {
                "latest_user_message": payload.message,
                "target_days": days,
                "request": request.model_dump(mode="json"),
                "day_rules": {key: value.model_dump() for key, value in rules.items()},
                "place_locks": [lock.model_dump() for lock in plan.place_locks],
                "current_assignments": {
                    str(i): [
                        item.location_name
                        for item in plan.daily_plans[i - 1].items
                        if item.item_type == "attraction"
                    ]
                    for i in days
                },
                "allowed_attractions": {
                    str(i): [
                        p._compact_attraction_for_day_context(item) for item in values
                    ]
                    for i, values in allowed.items()
                },
            }
        )
        assignments = self._validate_selection(selection, days, allowed, rules)
        for lock in plan.place_locks:
            if lock.kind == "attraction" and lock.day_index in days:
                if lock.name not in {item.name for item in assignments[lock.day_index]}:
                    raise ValueError(
                        f"「{lock.name}」已锁定，本次调整未保留它。请先解锁或换一种调整方式；原行程已保留。"
                    )
        attractions = AttractionResearch(
            city_overview=plan.overview, selected_attractions=list(catalog.values())
        )
        hotels = HotelResearch(
            candidates=list(
                {
                    hotel.name: hotel
                    for hotel in [
                        *plan.hotel_candidates,
                        plan.recommended_hotel,
                        *[
                            h
                            for stay in plan.daily_stays
                            for h in (stay.start_hotel, stay.end_hotel)
                        ],
                    ]
                    if hotel
                }.values()
            ),
            recommended_hotel=plan.recommended_hotel,
        )
        context["revision_instruction"] = payload.message
        context["revision_day_rules"] = {
            key: value.model_dump() for key, value in rules.items()
        }
        context["revision_budget_note"] = (
            "预算为所有同行人的总预算；餐饮预算需为全体同行人合计，不要篡改候选价格。"
        )

        if intent.change_hotel or ("hotel_style" in updates):
            progress(
                40, "重新筛选住宿", "住宿研究正在核对候选与预算，并同步每日落脚点。"
            )
            hotels = p._build_hotel_research_with_repair(
                request,
                context,
                {**p._hotel_rotation_policy(request), "target_hotel_count": 1},
            )
            hotel = hotels.recommended_hotel
            if hotel is None:
                raise ValueError("暂时没有找到符合要求的住宿，原行程未更改。")
            plan.recommended_hotel = hotel
            plan.hotel_candidates = hotels.candidates
            plan.hotel_data_source = p._source_from_hotel_research(hotels)
            plan.daily_stays = [
                DailyStayPlan(
                    day_index=i + 1,
                    date=day.date,
                    start_hotel=hotel,
                    end_hotel=hotel,
                    charged_night=i < request.stay_nights,
                    night_area=hotel.nearby_area,
                    reason="根据本轮住宿要求调整，连续住宿以减少搬运行李。",
                )
                for i, day in enumerate(plan.daily_plans)
            ]

        skeleton = {"assignments": assignments, "daily_stays": plan.daily_stays}
        if intent.change_hotel or ("hotel_style" in updates):
            p._align_hotels_with_itinerary(request, skeleton, hotels, context)
            plan.recommended_hotel = hotels.recommended_hotel
            plan.hotel_candidates = hotels.candidates
            plan.hotel_data_source = p._source_from_hotel_research(hotels)
            plan.notes.extend(skeleton.get("notes", []))
        weather = WeatherResearch(forecast=[day.weather for day in plan.daily_plans])
        restaurant_catalog: dict[str, list[Any]] = {}
        p._request_transit_preference = request.transit_preference
        for offset, index in enumerate(days):
            progress(
                45 + int(30 * offset / len(days)),
                f"调整第 {index} 天",
                "正在安排景点与用餐，随后重新计算路线。",
            )
            day = p._build_day_plan_with_repair(
                request, index, skeleton, attractions, weather, hotels, context
            )
            # Scenario requests (e.g. rain) must never overwrite sourced forecasts.
            day.weather = plan.daily_plans[index - 1].weather.model_copy(deep=True)
            for lock in plan.place_locks:
                if lock.kind == "attraction" and lock.day_index == index:
                    item = next(
                        item
                        for item in day.items
                        if item.item_type == "attraction"
                        and item.location_name == lock.name
                    )
                    item.location_address = lock.address
            locked_meals = [
                resolve_target(plan, lock).model_copy(deep=True)
                for lock in plan.place_locks
                if lock.kind == "meal" and lock.day_index == index
            ]
            for item in locked_meals:
                if item.is_route_stop and not valid_location(item.location):
                    detail = (
                        PlaceService(p.backend)
                        .resolve(
                            PlaceLookupRequest(
                                city=request.city,
                                places=[
                                    PlaceReference(
                                        key="locked-meal",
                                        name=item.location_name,
                                        address=item.location_address,
                                    )
                                ],
                            )
                        )
                        .places[0]
                    )
                    if not detail.verified or not valid_location(
                        {"lat": detail.lat, "lng": detail.lng}
                    ):
                        raise ValueError(
                            f"暂时无法核实锁定餐厅「{item.location_name}」的位置，请稍后重试；原行程已保留。"
                        )
                    item.location = Location(
                        name=item.location_name,
                        address=detail.address,
                        lat=detail.lat,
                        lng=detail.lng,
                    )
            day_context = p._day_generation_context(
                request, index, skeleton, attractions, weather
            )
            p._materialize_meal_intents(
                day,
                request,
                day_context,
                context,
                restaurant_catalog,
                locked_meals=locked_meals,
            )
            for item in day.items:
                if item.item_type in {
                    "meal",
                    "food",
                } and p._constraint_keyword_violations(
                    f"{item.location_name} {item.summary}",
                    context.get("keywords_to_avoid", {}).get("dining", []),
                ):
                    raise ValueError(
                        "锁定用餐地点与本次饮食要求冲突，请先解锁后调整；原行程已保留。"
                    )
            self._validate_day(day, rules[str(index)])
            plan.daily_plans[index - 1] = day

        progress(82, "重算交通和费用", "正在为修改过的日期更新交通，汇总完整行程预算。")
        p._inject_transport_details(
            plan,
            request,
            attractions,
            hotels,
            restaurant_catalog,
            plan.daily_stays,
            day_indices=set(days),
        )
        p._check_plan_schedule(plan, request, attractions, day_indices=set(days))
        p._refresh_plan_budget(plan, request, plan.daily_stays)
        self._validate_result(plan, payload, days, rules, catalog)
        assert_locks_preserved(payload.plan, plan)
        used_names = {
            item.location_name
            for day in plan.daily_plans
            for item in day.items
            if item.item_type == "attraction"
        }
        plan.selected_attractions = [
            item for name, item in catalog.items() if name in used_names
        ]
        source_text = "、".join(
            "地图检索" if mode == "live" else "内置参考数据" for mode in sorted(sources)
        )
        note = f"最近一次调整使用{source_text or '原行程地点'}；费用为估算，开放时间和预约情况需出发前确认。"
        plan.notes = [
            item for item in plan.notes if not item.startswith("最近一次调整使用")
        ] + [note]
        # The existing overview describes the original selection. Avoid presenting it as the revised route.
        if sources:
            plan.attraction_data_source = (
                "live"
                if sources == {"live"} and payload.plan.attraction_data_source == "live"
                else "fallback"
            )
        warnings = []
        if plan.budget.total > request.budget_max:
            warnings.append(
                f"重新核算后预计 ¥{plan.budget.total:,.0f}，仍高于预算上限 ¥{plan.budget.total - request.budget_max:,.0f}，需要继续调整住宿或活动。"
            )
        if any(rules[str(i)].indoor_only for i in days):
            warnings.append(
                "室内场所根据地点名称与类别筛选，仍需确认开放、预约情况；页面天气保留原数据来源。"
            )
        changes = self._changes(payload, request, plan, days)
        progress(96, "整理修改结果", "正在保存本轮变化与费用对比。")
        return TravelRevisionResponse(
            status="revised",
            message=f"已调整第 {'、'.join(map(str, days))} 天的安排，并重新核算交通与预算。",
            request=request,
            plan=plan,
            day_rules=rules,
            changes=changes,
            warnings=warnings,
        )

    @staticmethod
    def _target_days(intent: RevisionIntent, trip_days: int) -> list[int]:
        if not intent.target_days or any(
            index < 1 or index > trip_days for index in intent.target_days
        ):
            raise ValueError("修改日期超出了当前行程，请说明要调整第几天。")
        return sorted(set(intent.target_days))

    def _attraction_catalog(
        self, request: TravelRequest, existing: list[Attraction], queries: list[str]
    ) -> tuple[dict[str, Attraction], set[str]]:
        catalog = {item.name: item.model_copy(deep=True) for item in existing}
        sources: set[str] = set()
        try:
            data = self.planner.backend.search_attractions(
                request.city,
                target_count=24,
                travelers=request.travelers,
                daily_budget=request.budget_max / request.trip_days,
                preferences=",".join(request.preferences),
            )
            sources.add(str(data.get("data_mode", "fallback")))
            for raw in data.get("attractions", []):
                item = Attraction.model_validate(raw)
                catalog.setdefault(item.name, item)
        except (ValueError, TypeError):
            pass
        for query in queries[:3]:
            data = self.planner.backend.search_attraction_pois(
                request.city, query[:40], limit=12
            )
            if data.get("candidates"):
                sources.add(str(data.get("data_mode", "fallback")))
            for raw in data.get("candidates", []):
                name = str(raw.get("name") or "").strip()
                if not name or name in catalog:
                    continue
                catalog[name] = Attraction(
                    candidate_id=str(raw.get("candidate_id") or ""),
                    source_query=query,
                    name=name,
                    category=str(raw.get("type") or raw.get("category") or "景点"),
                    summary=str(
                        raw.get("type") or raw.get("category") or "检索到的景点候选"
                    ),
                    ticket_price=self.planner._estimate_attraction_ticket_price(raw),
                    location=Location(
                        name=name,
                        address=str(raw.get("address") or ""),
                        lat=float(raw.get("lat") or 0),
                        lng=float(raw.get("lng") or 0),
                    ),
                )
        return catalog, sources

    @staticmethod
    def _is_indoor(item: Attraction) -> bool:
        # Do not infer indoors from the search query or model-written rationale.
        return any(
            word in item.name
            for word in (
                "博物馆",
                "美术馆",
                "科技馆",
                "纪念馆",
                "展览馆",
                "艺术馆",
                "海洋馆",
                "水族馆",
            )
        )

    @staticmethod
    def _validate_selection(
        selection: Any,
        days: list[int],
        allowed: dict[int, list[Attraction]],
        rules: dict[str, DayRevisionRule],
    ) -> dict[int, list[Attraction]]:
        indices = [assignment.day_index for assignment in selection.assignments]
        if len(indices) != len(days) or set(indices) != set(days):
            raise ValueError("景点调整范围校验失败，原行程未更改，请重试。")
        assignments: dict[int, list[Attraction]] = {}
        used: set[str] = set()
        for assignment in selection.assignments:
            index = assignment.day_index
            catalog = {item.name: item for item in allowed[index]}
            cap = rules[str(index)].max_attractions or 6
            if len(assignment.attraction_names) > cap:
                raise ValueError(
                    f"第 {index} 天未满足每天最多 {cap} 个景点的要求，请重试。"
                )
            assignments[index] = []
            for name in assignment.attraction_names:
                if name not in catalog or name in used:
                    raise ValueError(
                        "景点调整含未核实或重复地点，原行程未更改，请重试。"
                    )
                used.add(name)
                assignments[index].append(catalog[name])
        return assignments

    @staticmethod
    def _validate_day(day: Any, rule: DayRevisionRule) -> None:
        count = sum(item.item_type == "attraction" for item in day.items)
        if rule.max_attractions and count > rule.max_attractions:
            raise ValueError("行程超过了本轮要求的景点数量，请重试。")
        for item in day.items:
            if not math.isfinite(item.estimated_cost) or item.estimated_cost < 0:
                raise ValueError("修改后的费用存在异常，原行程未更改。")
            if item.item_type not in {
                "attraction",
                "meal",
                "food",
                "rest",
                "free_time",
            }:
                raise ValueError("修改后出现了未核实的活动类型，原行程未更改。")
            if item.item_type in {"rest", "free_time"}:
                item.is_route_stop = False

    def _validate_result(
        self,
        plan: Any,
        payload: TravelRevisionRequest,
        days: list[int],
        rules: dict[str, DayRevisionRule],
        catalog: dict[str, Attraction],
    ) -> None:
        seen = set()
        for index, day in enumerate(plan.daily_plans, 1):
            if index not in days and day != payload.plan.daily_plans[index - 1]:
                raise ValueError("修改超出了指定日期，原行程未更改。")
            for item in day.items:
                if item.item_type != "attraction":
                    continue
                if index in days and (
                    item.location_name not in catalog
                    or (
                        rules[str(index)].indoor_only
                        and not self._is_indoor(catalog[item.location_name])
                    )
                ):
                    raise ValueError(
                        "调整后的景点没有满足地点或室内要求，原行程未更改。"
                    )
                if item.location_name in seen:
                    raise ValueError("修改后存在跨天重复景点，原行程未更改。")
                seen.add(item.location_name)
        if not all(
            math.isfinite(value) and value >= 0
            for value in plan.budget.model_dump().values()
        ):
            raise ValueError("重新核算的预算异常，原行程未更改。")

    @staticmethod
    def _changes(
        payload: TravelRevisionRequest,
        request: TravelRequest,
        plan: Any,
        days: list[int],
    ) -> list[RevisionChange]:
        changes = []

        def day_text(day: Any) -> str:
            return "；".join(
                f"{item.time_range} {item.title}"
                for item in day.items
                if item.item_type != "transport"
            )

        for index in days:
            before, after = (
                payload.plan.daily_plans[index - 1],
                plan.daily_plans[index - 1],
            )
            if before != after:
                changes.append(
                    RevisionChange(
                        section=f"第 {index} 天 · {after.date.isoformat()}",
                        before=day_text(before),
                        after=day_text(after),
                    )
                )
        old_hotel = payload.plan.recommended_hotel
        new_hotel = plan.recommended_hotel
        if old_hotel != new_hotel:
            changes.append(
                RevisionChange(
                    section="推荐住宿",
                    before=old_hotel.name if old_hotel else "未指定",
                    after=new_hotel.name if new_hotel else "未指定",
                )
            )
        if payload.request.budget_max != request.budget_max:
            changes.append(
                RevisionChange(
                    section="预算上限",
                    before=f"¥{payload.request.budget_max:,.0f}",
                    after=f"¥{request.budget_max:,.0f}",
                )
            )
        if payload.plan.budget.total != plan.budget.total:
            changes.append(
                RevisionChange(
                    section="预计总花费",
                    before=f"¥{payload.plan.budget.total:,.0f}",
                    after=f"¥{plan.budget.total:,.0f}",
                )
            )
        return changes

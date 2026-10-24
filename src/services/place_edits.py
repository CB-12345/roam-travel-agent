"""Verified alternatives and transactional replacement of one planned place."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, Callable

from src.models import (
    Attraction,
    AttractionResearch,
    DayPlanItem,
    HotelOption,
    HotelResearch,
    Location,
    MealIntent,
    RestaurantOption,
    RoutePlan,
)
from src.models_place_edits import (
    PlaceCandidate,
    PlaceCandidateRequest,
    PlaceCandidatesResponse,
    PlaceReplacementRequest,
)
from src.models_revision import DayRevisionRule, RevisionChange, TravelRevisionResponse
from src.services.places import (
    PlaceLookupRequest,
    PlaceReference,
    PlaceService,
    amap_url,
)
from src.services.planning_quality import distance_km, time_bounds, valid_location
from src.services.revision import TravelRevisionService
from src.services.stop_locks import (
    assert_locks_preserved,
    hotel_days,
    is_locked,
    resolve_target,
    same_place,
    validate_locks,
)

if TYPE_CHECKING:
    from src.services.planner import TravelPlannerService


class PlaceEditService:
    TTL_SECONDS = 600

    def __init__(self, planner: TravelPlannerService, places: PlaceService):
        self.planner = planner
        self.places = places
        self._offers: dict[str, tuple[float, str, Any]] = {}

    @staticmethod
    def _fingerprint(payload: PlaceCandidateRequest) -> str:
        data = {
            key: getattr(payload, key).model_dump(mode="json")
            for key in ("request", "plan", "target")
        }
        data["day_rules"] = {
            key: value.model_dump() for key, value in payload.day_rules.items()
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _context(self, payload: PlaceCandidateRequest):
        validate_locks(payload.plan)
        current = resolve_target(payload.plan, payload.target)
        if is_locked(payload.plan, payload.target):
            raise ValueError("这个地点已锁定，请先解锁，再查看替换候选。")
        affected = (
            hotel_days(payload.plan, payload.target)
            if payload.target.kind == "hotel"
            else [payload.target.day_index]
        )
        return current, affected

    def _location(self, payload: PlaceCandidateRequest, item: Any) -> Location:
        if valid_location(getattr(item, "location", None)):
            return item.location
        if isinstance(item, DayPlanItem) and item.item_type == "attraction":
            point = next(
                (
                    p
                    for p in payload.plan.selected_attractions
                    if p.name == item.location_name
                ),
                None,
            )
            if point and valid_location(point.location):
                return point.location
        if isinstance(item, DayPlanItem) and not item.is_route_stop:
            day = payload.plan.daily_plans[payload.target.day_index - 1]
            stay = payload.plan.daily_stays[payload.target.day_index - 1]
            if payload.target.meal_type == "breakfast" and stay.start_hotel:
                return stay.start_hotel.location
            names = [p.location_name for p in day.items if p.item_type == "attraction"]
            point = next(
                (p for p in payload.plan.selected_attractions if p.name in names), None
            )
            if point:
                return point.location
        name = getattr(item, "name", None) or item.location_name
        address = getattr(item, "location_address", "")
        detail = self.places.resolve(
            PlaceLookupRequest(
                city=payload.request.city,
                places=[PlaceReference(key="edit-anchor", name=name, address=address)],
            )
        ).places[0]
        if not detail.verified or not valid_location(
            {"lat": detail.lat, "lng": detail.lng}
        ):
            raise ValueError(f"暂时无法核实「{name}」的精确位置，当前行程已保留。")
        return Location(
            name=name, address=detail.address, lat=detail.lat, lng=detail.lng
        )

    def candidates(self, payload: PlaceCandidateRequest) -> PlaceCandidatesResponse:
        current, affected = self._context(payload)
        center = self._location(payload, current)
        p, req = self.planner, payload.request
        context = p._build_constraint_context(req)
        kind = payload.target.kind
        used_names = {
            item.location_name
            for day in payload.plan.daily_plans
            for item in day.items
            if item.item_type != "transport"
        }
        used_names.update(
            hotel.name
            for stay in payload.plan.daily_stays
            for hotel in (stay.start_hotel, stay.end_hotel)
            if hotel
        )
        options: list[Any] = []
        if kind == "attraction":
            rule = payload.day_rules.get(
                str(payload.target.day_index), DayRevisionRule()
            )
            original = next(
                (
                    point
                    for point in payload.plan.selected_attractions
                    if point.name == payload.target.name
                ),
                None,
            )
            queries = (
                ["博物馆", "美术馆", "纪念馆"]
                if rule.indoor_only
                else [
                    original.source_query if original else "",
                    original.category if original else "",
                    *[
                        {
                            "humanity": "博物馆",
                            "art": "美术馆",
                            "nature": "公园",
                            "family": "科技馆",
                            "food": "历史文化街区",
                            "shopping": "步行街",
                            "nightlife": "夜市",
                        }[pref]
                        for pref in req.preferences
                    ],
                    "风景名胜",
                ]
            )
            queries = list(dict.fromkeys(query for query in queries if query))
            if not rule.indoor_only and len(queries) > 3:
                queries = queries[:2] + ["风景名胜"]
            for query in queries[:3]:
                result = p.backend.search_attraction_pois(req.city, query, limit=20)
                if result.get("data_mode") != "live":
                    continue
                for raw in result.get("candidates", []):
                    name = str(raw.get("name") or "")
                    try:
                        location = Location(
                            name=name,
                            address=raw.get("address") or "",
                            lat=raw.get("lat") or 0,
                            lng=raw.get("lng") or 0,
                        )
                    except (ValueError, TypeError):
                        continue
                    text = f"{name} {raw.get('type', '')}"
                    poi_type = str(raw.get("type") or "")
                    if any(
                        word in poi_type
                        for word in (
                            "餐饮服务",
                            "住宿服务",
                            "购物服务",
                            "生活服务",
                            "公司企业",
                            "医疗保健",
                            "交通设施",
                            "学校",
                        )
                    ):
                        continue
                    if (
                        not name
                        or name in used_names
                        or distance_km(center, location) > 5
                    ):
                        continue
                    if any(
                        word in name
                        for word in (
                            "停车场",
                            "售票",
                            "游客中心",
                            "卫生间",
                            "服务中心",
                            "出入口",
                            "管理处",
                        )
                    ):
                        continue
                    if any(
                        name.startswith(point.name + separator)
                        for point in payload.plan.selected_attractions
                        for separator in ("-", "·", "（", "(")
                    ) or any(
                        point.name in location.address
                        and distance_km(point.location, location) < 0.3
                        for point in payload.plan.selected_attractions
                    ):
                        # A building or service point inside the same site is not a replacement destination.
                        continue
                    if p._constraint_keyword_violations(
                        text,
                        context.get("keywords_to_avoid", {}).get("attractions", []),
                    ):
                        continue
                    # Search queries alone must not turn a restaurant or hotel into a tourist attraction.
                    if not any(
                        word in text
                        for word in (
                            "风景",
                            "景点",
                            "博物馆",
                            "美术馆",
                            "公园",
                            "文化",
                            "古迹",
                            "纪念馆",
                            "科技馆",
                            "广场",
                            "古城",
                            "古镇",
                            "步行街",
                            "夜市",
                            "展览馆",
                            "寺",
                            "祠",
                            "学宫",
                        )
                    ):
                        continue
                    point = Attraction(
                        name=name,
                        candidate_id=str(raw.get("candidate_id") or ""),
                        source_query=query,
                        category=str(raw.get("type") or query),
                        summary="同一区域的备选游玩地点，开放与预约情况请出发前确认。",
                        ticket_price=p._estimate_attraction_ticket_price(raw),
                        recommended_hours=p._estimate_attraction_hours(
                            str(raw.get("type") or query), query
                        ),
                        location=location,
                    )
                    if not rule.indoor_only or TravelRevisionService._is_indoor(point):
                        options.append(point)
                if len({option.name for option in options}) >= 3:
                    break
        elif kind == "meal":
            day = payload.plan.daily_plans[payload.target.day_index - 1]
            intent = next(
                (
                    intent
                    for intent in day.meal_intents
                    if intent.meal_type == payload.target.meal_type
                ),
                None,
            )
            if intent is None:
                intent = MealIntent(
                    meal_type=payload.target.meal_type or "lunch",
                    time_range=current.time_range,
                    anchor_name=current.location_name,
                    cuisine_intent="本地餐饮",
                    budget_total=current.estimated_cost,
                )
            avoid = list(
                dict.fromkeys(
                    [
                        *intent.must_avoid,
                        *context.get("keywords_to_avoid", {}).get("dining", []),
                    ]
                )
            )
            for query, radius in p._meal_search_terms(intent):
                radius = min(radius, 1500 if req.transport_mode == "walk" else 2500)
                result = p.backend.search_restaurants(
                    req.city,
                    anchor=payload.target.name,
                    anchor_location=center.model_dump(),
                    preferences=query,
                    budget_hint=current.estimated_cost,
                    travelers=req.travelers,
                    radius_m=radius,
                )
                if result.get("data_mode") != "live":
                    continue
                for raw in result.get("restaurants", []):
                    try:
                        option = RestaurantOption.model_validate(raw)
                    except (ValueError, TypeError):
                        continue
                    if (
                        option.name not in used_names
                        and distance_km(center, option.location) <= radius / 1000
                        and p._restaurant_matches_intent(option, avoid)
                        and (
                            payload.target.meal_type == "breakfast"
                            or not any(
                                word in f"{option.name} {option.cuisine}"
                                for word in (
                                    "奶茶",
                                    "咖啡",
                                    "甜品",
                                    "蛋糕",
                                    "烘焙",
                                    "糕点",
                                    "面包",
                                    "冰淇淋",
                                    "糖水",
                                    "茶饮",
                                    "臭豆腐",
                                    "咸蛋黄",
                                    "零食",
                                    "茶业",
                                )
                            )
                        )
                    ):
                        options.append(option)
                if len({option.name for option in options}) >= 3:
                    break
        else:
            result = p.backend.search_hotels(
                req.city,
                budget_min=req.budget_min,
                budget_max=req.budget_max,
                travelers=req.travelers,
                stay_nights=req.stay_nights,
                hotel_style=req.hotel_style,
                limit=12,
                anchor_location=center.model_dump(),
                radius_m=4000,
            )
            if result.get("data_mode") == "live":
                for raw in result.get("candidates", []):
                    try:
                        options.append(HotelOption.model_validate(raw))
                    except (ValueError, TypeError):
                        continue
            options = [
                hotel
                for hotel in options
                if hotel.name not in used_names
                and distance_km(center, hotel.location) <= 4
                and self._hotel_near_days(payload, hotel, affected)
            ]
        options = [
            option
            for option in options
            if valid_location(option.location)
            and option.location.address
            and all(
                math.isfinite(value) and value >= 0
                for value in (
                    getattr(option, "ticket_price", 0),
                    getattr(option, "avg_cost_per_person", 0),
                    getattr(option, "nightly_price", 0),
                )
            )
        ]
        options = list({option.name: option for option in options}.values())
        options.sort(key=lambda option: distance_km(center, option.location))
        self._offers = {
            key: value
            for key, value in self._offers.items()
            if value[0] > time.monotonic()
        }
        while len(self._offers) > 120:
            self._offers.pop(next(iter(self._offers)))
        fingerprint = self._fingerprint(payload)
        candidates = []
        for option in options[:3]:
            token = secrets.token_urlsafe(32)
            self._offers[token] = (
                time.monotonic() + self.TTL_SECONDS,
                fingerprint,
                option.model_copy(deep=True),
            )
            cost = (
                option.ticket_price
                if kind == "attraction"
                else (
                    option.avg_cost_per_person * req.travelers
                    if kind == "meal"
                    else option.nightly_price
                )
            )
            delta = cost - (
                current.nightly_price if kind == "hotel" else current.estimated_cost
            )
            nights = sum(
                stay.charged_night
                for stay in payload.plan.daily_stays
                if stay.day_index in affected
                and stay.end_hotel
                and same_place(
                    stay.end_hotel.name,
                    stay.end_hotel.location.address,
                    payload.target.name,
                    payload.target.address,
                )
            )
            if kind == "hotel":
                delta *= nights * math.ceil(req.travelers / 2)
            candidates.append(
                PlaceCandidate(
                    token=token,
                    name=option.name,
                    kind=kind,
                    location=option.location,
                    estimated_cost=round(cost, 2),
                    cost_delta=round(delta, 2),
                    recommended_hours=getattr(option, "recommended_hours", None),
                    nightly_price_min=getattr(option, "nightly_price_min", None),
                    nightly_price_max=getattr(option, "nightly_price_max", None),
                    reasons=[
                        f"距原地点约 {distance_km(center, option.location):.1f} 公里（直线）",
                        "来自实时地图检索；替换时会校验交通、时间与预算",
                    ],
                    affected_days=affected,
                    map_url=amap_url(
                        req.city, option.name, option.location.lat, option.location.lng
                    ),
                )
            )
        return PlaceCandidatesResponse(
            target=payload.target,
            candidates=candidates,
            affected_days=affected,
            message=(
                "选择一个候选，确认后更新安排。"
                if candidates
                else "附近暂未找到符合条件的可靠备选，可以尝试更换其他地点；当前安排已保留。"
            ),
        )

    @staticmethod
    def _hotel_near_days(payload, hotel, days):
        limit = 4 if payload.request.transport_mode == "walk" else 8
        for index in days:
            names = {
                item.location_name
                for item in payload.plan.daily_plans[index - 1].items
                if item.item_type == "attraction"
            }
            points = [p for p in payload.plan.selected_attractions if p.name in names]
            if (
                points
                and min(distance_km(hotel.location, point.location) for point in points)
                > limit
            ):
                return False
        return True

    def replace(
        self,
        payload: PlaceReplacementRequest,
        progress_callback: Callable | None = None,
    ) -> TravelRevisionResponse:
        current, affected = self._context(payload)
        offer = self._offers.get(payload.candidate_token)
        if (
            not offer
            or offer[0] <= time.monotonic()
            or offer[1] != self._fingerprint(payload)
        ):
            raise ValueError(
                "候选已过期或行程已变化，请重新点击“换一个”；原行程已保留。"
            )
        option = offer[2].model_copy(deep=True)
        p, req = self.planner, payload.request
        plan = payload.plan.model_copy(deep=True)
        p._emit_progress(
            progress_callback,
            stage="replacement",
            percent=20,
            title="替换选中地点",
            detail="正在保留其他地点和锁定安排。",
        )
        if payload.target.kind == "hotel":
            for stay in plan.daily_stays:
                for field in ("start_hotel", "end_hotel"):
                    hotel = getattr(stay, field)
                    if hotel and same_place(
                        hotel.name,
                        hotel.location.address,
                        payload.target.name,
                        payload.target.address,
                    ):
                        setattr(stay, field, option.model_copy(deep=True))
                stay.hotel_changed = bool(
                    stay.charged_night
                    and stay.start_hotel
                    and stay.end_hotel
                    and stay.start_hotel.name != stay.end_hotel.name
                )
                if stay.end_hotel:
                    stay.night_area = stay.end_hotel.nearby_area
                if stay.day_index in affected:
                    stay.reason = "已同步更换这家酒店，沿用原定入住晚数与同住安排。"
            if plan.recommended_hotel and same_place(
                plan.recommended_hotel.name,
                plan.recommended_hotel.location.address,
                payload.target.name,
                payload.target.address,
            ):
                plan.recommended_hotel = option.model_copy(deep=True)
            plan.hotel_candidates = [
                hotel
                for hotel in plan.hotel_candidates
                if not same_place(
                    hotel.name,
                    hotel.location.address,
                    payload.target.name,
                    payload.target.address,
                )
            ] + [option]
            for day in plan.daily_plans:
                for item in day.items:
                    if (
                        item.item_type == "hotel"
                        and item.location_name == payload.target.name
                    ):
                        item.title = item.location_name = option.name
                        item.location_address = option.location.address
                        item.location = option.location.model_copy(deep=True)
        else:
            item = resolve_target(plan, payload.target)
            item.title = item.location_name = option.name
            item.location_address = option.location.address
            item.location = option.location.model_copy(deep=True)
            item.summary = option.summary
            item.is_route_stop = True
            if payload.target.kind == "attraction":
                bounds = time_bounds(item.time_range)
                if bounds is None:
                    raise ValueError(
                        "原地点的时间格式无法识别，请先调整当天安排；原行程已保留。"
                    )
                start, _ = bounds
                end = start + math.ceil(option.recommended_hours * 60)
                if end >= 24 * 60:
                    raise ValueError(
                        "该景点无法在当天完成，请选择其他候选；原行程已保留。"
                    )
                item.time_range = (
                    f"{start // 60:02}:{start % 60:02}-{end // 60:02}:{end % 60:02}"
                )
                item.estimated_cost = option.ticket_price
                plan.selected_attractions.append(option)
            else:
                item.estimated_cost = round(
                    option.avg_cost_per_person * req.travelers, 2
                )
                intent = next(
                    (
                        i
                        for i in plan.daily_plans[
                            payload.target.day_index - 1
                        ].meal_intents
                        if i.meal_type == item.meal_type
                    ),
                    None,
                )
                if intent:
                    item.summary = p._meal_summary(intent, option)
            for intent in plan.daily_plans[payload.target.day_index - 1].meal_intents:
                if intent.anchor_name == payload.target.name:
                    intent.anchor_name = option.name
        p._emit_progress(
            progress_callback,
            stage="replacement",
            percent=50,
            title="更新相邻交通",
            detail="只重查变化的交通段，校准受影响日期的时间与费用。",
        )
        p._request_transit_preference = req.transit_preference
        for index in affected:
            self._recalculate_day(payload, plan, index)
        ordered_names = list(
            dict.fromkeys(
                item.location_name
                for day in plan.daily_plans
                for item in day.items
                if item.item_type == "attraction"
            )
        )
        catalog = {point.name: point for point in plan.selected_attractions}
        plan.selected_attractions = [
            catalog[name] for name in ordered_names if name in catalog
        ]
        attractions = AttractionResearch(
            city_overview=plan.overview, selected_attractions=plan.selected_attractions
        )
        p._check_plan_schedule(plan, req, attractions, day_indices=set(affected))
        p._refresh_plan_budget(plan, req, plan.daily_stays)
        assert_locks_preserved(payload.plan, plan)
        for index, day in enumerate(plan.daily_plans, 1):
            if index not in affected and day != payload.plan.daily_plans[index - 1]:
                raise ValueError("修改超出了目标日期，原行程已保留。")
        warnings = (
            [
                f"预计总费用超过预算上限 ¥{plan.budget.total - req.budget_max:.0f}，可继续调整住宿或活动。"
            ]
            if plan.budget.total > req.budget_max
            else []
        )
        section = (
            "住宿"
            if payload.target.kind == "hotel"
            else f"第 {payload.target.day_index} 天"
        )
        return TravelRevisionResponse(
            status="revised",
            message=f"已将「{payload.target.name}」更换为「{option.name}」，并更新第 {'、'.join(map(str, affected))} 天的时间与费用。",
            request=req,
            plan=plan,
            day_rules=payload.day_rules,
            changes=[
                RevisionChange(
                    section=section, before=payload.target.name, after=option.name
                ),
                RevisionChange(
                    section="预计总费用",
                    before=f"¥{payload.plan.budget.total:.0f}",
                    after=f"¥{plan.budget.total:.0f}",
                ),
            ],
            warnings=warnings,
        )

    def _recalculate_day(self, payload, plan, index):
        from src.services.planner import RouteTarget

        p, req = self.planner, payload.request
        day, before = plan.daily_plans[index - 1], payload.plan.daily_plans[index - 1]
        points = AttractionResearch(
            city_overview="", selected_attractions=plan.selected_attractions
        )
        hotels = HotelResearch(
            candidates=[
                hotel
                for stay in plan.daily_stays
                for hotel in (stay.start_hotel, stay.end_hotel)
                if hotel
            ]
        )
        catalog = p._build_route_target_catalog(points, hotels, {})
        items = p._sort_day_items_by_time(
            [item for item in day.items if item.item_type != "transport"]
        )
        for item in items:
            if valid_location(item.location):
                catalog[item.location_name] = RouteTarget(
                    item.location_name,
                    item.location_address,
                    item.location.lat,
                    item.location.lng,
                )
        targets = p._collect_day_route_targets(
            items, plan.daily_stays[index - 1], catalog
        )
        cached = defaultdict(deque)
        for item in before.items:
            if item.item_type == "transport":
                cached[(item.from_location, item.to_location)].append(item)
        routes = []
        for a, b in zip(targets, targets[1:]):
            if cached[(a.name, b.name)]:
                old = cached[(a.name, b.name)].popleft()
                mode = {
                    "公共交通": "public_transit",
                    "步行": "walk",
                    "打车": "taxi",
                    "自驾": "self_drive",
                    "混合出行": "mixed",
                }.get(old.transport_mode, req.transport_mode)
                routes.append(
                    RoutePlan(
                        city=req.city,
                        mode=mode,
                        origin=a.name,
                        destination=b.name,
                        origin_address=a.address,
                        destination_address=b.address,
                        duration_min=old.duration_min,
                        distance_km=old.distance_km,
                        estimated_cost=old.estimated_cost,
                        transfers=old.transfers,
                        data_mode=old.route_data_mode,
                        fallback_reason=old.route_fallback_reason,
                        summary=old.reason,
                        strategy_label=old.route_strategy,
                        route_segments=old.route_segments,
                        route_alternatives=old.route_alternatives,
                    )
                )
                continue
            verified = []
            for target in (a, b):
                if not valid_location(target):
                    source = next(
                        (item for item in items if item.location_name == target.name),
                        None,
                    )
                    if source is None:
                        raise ValueError("相邻地点坐标不完整，原行程已保留。")
                    location = self._location(payload, source)
                    source.location = location
                    target = RouteTarget(
                        target.name, source.location_address, location.lat, location.lng
                    )
                verified.append(target)
            routes.append(
                p._build_route_plan(
                    req.city,
                    a.name,
                    b.name,
                    req.transport_mode,
                    origin=verified[0],
                    destination=verified[1],
                )
            )
        day.items = p._interleave_transport_items(items, routes)
        day.total_transport_cost = round(
            sum(route.estimated_cost for route in routes), 2
        )
        day.total_transport_time_min = sum(route.duration_min for route in routes)
        day.route_summary = p._refresh_day_route_summary(day, req)

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.models import (
    Attraction,
    AttractionResearch,
    BudgetBreakdown,
    DailyStayPlan,
    DayPlan,
    DayPlanItem,
    HotelOption,
    HotelResearch,
    Location,
    MealIntent,
    PlaceTarget,
    TravelRequest,
    TripPlan,
    WeatherInfo,
)
from src.models_place_edits import PlaceCandidateRequest, PlaceReplacementRequest
from src.services.place_edits import PlaceEditService
from src.services.places import PlaceService
from src.services.planner import TravelPlannerService
from src.services.stop_locks import assert_locks_preserved, is_locked


@pytest.fixture
def edit_context():
    planner = TravelPlannerService()
    planner.backend.amap_key = ""
    planner.backend.qweather_key = ""
    planner.backend.estimate_local_transport = Mock(
        return_value={
            "distance_km": 1,
            "duration_min": 10,
            "estimated_cost": 6,
            "data_mode": "live",
            "summary": "已核验交通",
        }
    )
    req = TravelRequest(
        city="杭州",
        start_date="2026-10-24",
        end_date="2026-10-26",
        budget_min=1000,
        budget_max=3000,
        travelers=3,
    )
    points = [
        Attraction(
            name=f"原博物馆{i}",
            category="博物馆",
            summary="游玩",
            ticket_price=20,
            location=Location(
                name=f"原博物馆{i}",
                address=f"博物路{i}号",
                lat=30.25,
                lng=120.15 + i * 0.001,
            ),
        )
        for i in range(1, 4)
    ]
    hotel = HotelOption(
        name="原酒店",
        style="comfort",
        nightly_price=300,
        summary="住宿",
        nearby_area="市中心",
        location=Location(name="原酒店", address="住宿路1号", lat=30.25, lng=120.15),
    )
    days = []
    for i, point in enumerate(points):
        date = req.start_date + timedelta(days=i)
        items = [
            DayPlanItem(
                time_range="09:00-11:00",
                title=point.name,
                location_name=point.name,
                location_address=point.location.address,
                location=point.location,
                item_type="attraction",
                summary="游玩",
                estimated_cost=20,
            )
        ]
        intents = []
        if i == 0:
            for meal, time in [
                ("breakfast", "07:30-08:00"),
                ("lunch", "12:00-13:00"),
                ("dinner", "18:00-19:00"),
            ]:
                location = Location(
                    name=f"原餐厅{meal}",
                    address=f"餐饮路{meal}号",
                    lat=30.25,
                    lng=120.152,
                )
                items.append(
                    DayPlanItem(
                        time_range=time,
                        title=location.name,
                        location_name=location.name,
                        location_address=location.address,
                        location=location,
                        item_type="meal",
                        meal_type=meal,
                        summary="本地用餐",
                        estimated_cost=90,
                    )
                )
                intents.append(
                    MealIntent(
                        meal_type=meal,
                        time_range=time,
                        anchor_name=point.name,
                        cuisine_intent="本地餐饮",
                        budget_total=90,
                    )
                )
        days.append(
            DayPlan(
                date=date,
                weather=WeatherInfo(
                    date=date,
                    condition="sunny",
                    high_c=24,
                    low_c=18,
                    suggestion="正常出行",
                ),
                route_summary="",
                items=items,
                meal_intents=intents,
            )
        )
    plan = TripPlan(
        city=req.city,
        travel_theme="旅行",
        overview="行程",
        trip_days=3,
        selected_attractions=points,
        recommended_hotel=hotel,
        hotel_candidates=[hotel],
        daily_plans=days,
        daily_stays=[
            DailyStayPlan(
                day_index=i + 1,
                date=day.date,
                start_hotel=hotel,
                end_hotel=hotel,
                charged_night=i < 2,
            )
            for i, day in enumerate(days)
        ],
        budget=BudgetBreakdown(
            hotel=0, food=0, attractions=0, transport=0, contingency=0, total=0
        ),
    )
    planner._inject_transport_details(
        plan,
        req,
        AttractionResearch(city_overview="", selected_attractions=points),
        HotelResearch(candidates=[hotel]),
        {},
        plan.daily_stays,
    )
    planner._check_plan_schedule(
        plan, req, AttractionResearch(city_overview="", selected_attractions=points)
    )
    planner._refresh_plan_budget(plan, req, plan.daily_stays)
    planner.backend.estimate_local_transport.reset_mock()
    planner.backend.search_attraction_pois = Mock(
        return_value={
            "data_mode": "live",
            "candidates": [
                {
                    "name": "新博物馆",
                    "address": "文化路8号",
                    "lat": 30.251,
                    "lng": 120.153,
                    "type": "科教文化服务;博物馆",
                    "candidate_id": "B123456789",
                },
                {
                    "name": "错误餐馆",
                    "address": "其他路",
                    "lat": 30.25,
                    "lng": 120.153,
                    "type": "餐饮服务",
                },
                {
                    "name": "远方博物馆",
                    "address": "其他城市",
                    "lat": 31.25,
                    "lng": 121.15,
                    "type": "博物馆",
                },
                {"name": "坏博物馆", "lat": "bad"},
            ],
        }
    )
    planner.backend.search_restaurants = Mock(
        return_value={
            "data_mode": "live",
            "restaurants": [
                {
                    "name": "新餐厅",
                    "cuisine": "本地餐饮",
                    "avg_cost_per_person": 40,
                    "summary": "餐饮",
                    "location": {
                        "name": "新餐厅",
                        "address": "餐饮路8号",
                        "lat": 30.251,
                        "lng": 120.153,
                    },
                }
            ],
        }
    )
    new_hotel = hotel.model_copy(deep=True)
    new_hotel.name = new_hotel.location.name = "新酒店"
    new_hotel.location.address = "住宿路8号"
    new_hotel.location.lng += 0.002
    new_hotel.nightly_price = 350
    planner.backend.search_hotels = Mock(
        return_value={"data_mode": "live", "candidates": [new_hotel.model_dump()]}
    )
    target = PlaceTarget(
        day_index=1,
        kind="attraction",
        name=points[0].name,
        address=points[0].location.address,
    )
    payload = PlaceCandidateRequest(request=req, plan=plan, target=target)
    return planner, PlaceEditService(planner, PlaceService(planner.backend)), payload


def choose(service, payload):
    offer = service.candidates(payload).candidates[0]
    return PlaceReplacementRequest(**payload.model_dump(), candidate_token=offer.token)


def test_replacement_rejects_closed_candidate_and_keeps_original_trip(edit_context):
    from src.services.places import PlaceDetails, PlaceLookupResponse

    planner, service, payload = edit_context
    replacement = choose(service, payload)
    before = payload.model_dump_json()
    planner.place_service.resolve = Mock(
        side_effect=lambda request: PlaceLookupResponse(
            configured=True,
            places=[
                PlaceDetails(
                    key=ref.key,
                    name=ref.name,
                    map_url="https://uri.amap.com/search",
                    verified=True,
                    opening_hours=(
                        "周六闭馆" if ref.name == "新博物馆" else "24小时营业"
                    ),
                )
                for ref in request.places
            ],
        )
    )
    with pytest.raises(ValueError, match="当日不开放"):
        service.replace(replacement)
    assert payload.model_dump_json() == before


def test_attraction_replacement_only_changes_target_and_adjacent_routes(edit_context):
    planner, service, payload = edit_context
    original = payload.model_dump_json()
    offers = service.candidates(payload)
    assert [item.name for item in offers.candidates] == ["新博物馆"]
    result = service.replace(
        PlaceReplacementRequest(
            **payload.model_dump(), candidate_token=offers.candidates[0].token
        )
    )
    assert result.plan.daily_plans[1:] == payload.plan.daily_plans[1:]
    before_meals = [
        item for item in payload.plan.daily_plans[0].items if item.item_type == "meal"
    ]
    after_meals = [
        item for item in result.plan.daily_plans[0].items if item.item_type == "meal"
    ]
    assert [item.location_name for item in before_meals] == [
        item.location_name for item in after_meals
    ]
    assert planner.backend.estimate_local_transport.call_count == 2
    assert "原博物馆1" not in {point.name for point in result.plan.selected_attractions}
    assert "新博物馆" in {point.name for point in result.plan.selected_attractions}
    assert result.plan.daily_stays == payload.plan.daily_stays
    assert payload.model_dump_json() == original
    budget = result.plan.budget
    assert budget.total == round(
        budget.hotel
        + budget.food
        + budget.attractions
        + budget.transport
        + budget.contingency,
        2,
    )


def test_attraction_candidates_exclude_facilities_inside_existing_site(edit_context):
    planner, service, payload = edit_context
    raw = planner.backend.search_attraction_pois.return_value["candidates"][0]
    planner.backend.search_attraction_pois.return_value["candidates"].extend(
        [
            {**raw, "name": "原博物馆1-展厅"},
            {
                **raw,
                "name": "历史古井",
                "address": "博物路1号原博物馆1内",
                "lng": 120.151,
            },
            {**raw, "name": "博物馆游客中心"},
            {**raw, "name": "学宫广场臭豆腐", "type": "餐饮服务;中餐厅"},
        ]
    )
    assert [item.name for item in service.candidates(payload).candidates] == [
        "新博物馆"
    ]


def test_meal_replacement_preserves_other_meals_and_attractions(edit_context):
    _, service, payload = edit_context
    meal = next(
        item for item in payload.plan.daily_plans[0].items if item.meal_type == "lunch"
    )
    payload.target = PlaceTarget(
        day_index=1,
        kind="meal",
        name=meal.location_name,
        address=meal.location_address,
        meal_type="lunch",
    )
    result = service.replace(choose(service, payload))
    updated = next(
        item for item in result.plan.daily_plans[0].items if item.meal_type == "lunch"
    )
    assert updated.location_name == "新餐厅"
    assert updated.estimated_cost == 120
    assert result.plan.selected_attractions == payload.plan.selected_attractions
    assert result.plan.daily_plans[1:] == payload.plan.daily_plans[1:]
    for before in payload.plan.daily_plans[0].items:
        if before.item_type == "meal" and before.meal_type != "lunch":
            after = next(
                item
                for item in result.plan.daily_plans[0].items
                if item.meal_type == before.meal_type
            )
            assert after == before


def test_hotel_replacement_updates_whole_chain_and_actual_room_nights(edit_context):
    _, service, payload = edit_context
    hotel = payload.plan.recommended_hotel
    payload.target = PlaceTarget(
        day_index=2, kind="hotel", name=hotel.name, address=hotel.location.address
    )
    offers = service.candidates(payload)
    assert offers.affected_days == [1, 2, 3]
    assert offers.candidates[0].cost_delta == 200  # 50 × 2 rooms × 2 charged nights
    result = service.replace(
        PlaceReplacementRequest(
            **payload.model_dump(), candidate_token=offers.candidates[0].token
        )
    )
    assert all(
        stay.start_hotel.name == stay.end_hotel.name == "新酒店"
        for stay in result.plan.daily_stays
    )
    assert result.plan.recommended_hotel.name == "新酒店"
    assert result.plan.budget.hotel == 1400
    assert result.plan.selected_attractions == payload.plan.selected_attractions


@pytest.mark.parametrize("mode", ["fallback", "unavailable"])
@pytest.mark.parametrize("kind", ["attraction", "meal", "hotel"])
def test_no_sample_or_unavailable_data_presented_as_real_candidates(
    edit_context, kind, mode
):
    planner, service, payload = edit_context
    if kind == "hotel":
        hotel = payload.plan.recommended_hotel
        payload.target = PlaceTarget(
            day_index=1, kind=kind, name=hotel.name, address=hotel.location.address
        )
        planner.backend.search_hotels.return_value["data_mode"] = mode
    elif kind == "meal":
        item = next(
            item
            for item in payload.plan.daily_plans[0].items
            if item.meal_type == "lunch"
        )
        payload.target = PlaceTarget(
            day_index=1,
            kind=kind,
            name=item.location_name,
            address=item.location_address,
            meal_type="lunch",
        )
        planner.backend.search_restaurants.return_value["data_mode"] = mode
    else:
        planner.backend.search_attraction_pois.return_value["data_mode"] = mode
    before = payload.plan.model_dump_json()
    result = service.candidates(payload)
    assert result.candidates == []
    assert "保留" in result.message
    assert payload.plan.model_dump_json() == before


@pytest.mark.parametrize("kind", ["attraction", "hotel"])
def test_locked_place_rejected_even_when_hotel_lock_on_other_day(edit_context, kind):
    _, service, payload = edit_context
    if kind == "hotel":
        hotel = payload.plan.recommended_hotel
        payload.target = PlaceTarget(
            day_index=2, kind=kind, name=hotel.name, address=hotel.location.address
        )
    payload.plan.place_locks = [payload.target.model_copy(update={"day_index": 1})]
    assert is_locked(payload.plan, payload.target)
    with pytest.raises(ValueError, match="先解锁"):
        service.candidates(payload)


@pytest.mark.parametrize("reason", ["expired", "modified", "unknown"])
def test_expired_or_stale_candidate_cannot_mutate_plan(edit_context, reason):
    _, service, payload = edit_context
    request = choose(service, payload)
    if reason == "expired":
        old = service._offers[request.candidate_token]
        service._offers[request.candidate_token] = (0, *old[1:])
    elif reason == "modified":
        request.plan.budget.total += 1
    else:
        request.candidate_token = "x" * 40
    before = request.model_dump_json()
    with pytest.raises(ValueError, match="过期或行程已变化"):
        service.replace(request)
    assert request.model_dump_json() == before


def test_impossible_replacement_fails_transactionally(edit_context):
    _, service, payload = edit_context
    request = choose(service, payload)
    service._offers[request.candidate_token][2].recommended_hours = 16
    before = request.model_dump_json()
    with pytest.raises(ValueError):
        service.replace(request)
    assert request.model_dump_json() == before


def test_unrelated_lock_survives_replacement_and_day_movement_is_rejected(edit_context):
    _, service, payload = edit_context
    point = payload.plan.selected_attractions[1]
    payload.plan.place_locks = [
        PlaceTarget(
            day_index=2,
            kind="attraction",
            name=point.name,
            address=point.location.address,
        )
    ]
    result = service.replace(choose(service, payload))
    assert result.plan.place_locks == payload.plan.place_locks
    assert_locks_preserved(payload.plan, result.plan)
    result.plan.daily_plans[1].items = []
    with pytest.raises(ValueError, match="已锁定"):
        assert_locks_preserved(payload.plan, result.plan)


def test_old_history_without_meal_coordinates_resolves_verified_branch(edit_context):
    _, service, payload = edit_context
    meal = next(
        item for item in payload.plan.daily_plans[0].items if item.meal_type == "lunch"
    )
    coords = meal.location
    meal.location = None
    service.places.resolve = Mock(
        return_value=type(
            "Response",
            (),
            {
                "places": [
                    type(
                        "Detail",
                        (),
                        {
                            "verified": True,
                            "lat": coords.lat,
                            "lng": coords.lng,
                            "address": coords.address,
                        },
                    )()
                ]
            },
        )()
    )
    result = service.replace(choose(service, payload))
    assert result.plan.daily_plans[1:] == payload.plan.daily_plans[1:]
    service.places.resolve.assert_called_once()


def test_place_edit_api_uses_sse_task_and_validates_locks(edit_context, monkeypatch):
    import src.app as module

    _, service, payload = edit_context
    monkeypatch.setattr(module, "place_edit_service", service)
    client = TestClient(module.app)
    response = client.post(
        "/api/plan/places/candidates", json=payload.model_dump(mode="json")
    )
    assert response.status_code == 200
    body = {
        **payload.model_dump(mode="json"),
        "candidate_token": response.json()["candidates"][0]["token"],
    }
    task = client.post("/api/plan/places/replace/tasks", json=body).json()
    stream = client.get(f"/api/plan/stream/{task['task_id']}")
    assert '"status": "revised"' in stream.text
    assert '"revision"' in stream.text
    payload.plan.place_locks = [payload.target]
    response = client.post(
        "/api/plan/places/candidates", json=payload.model_dump(mode="json")
    )
    assert response.status_code == 422
    assert "先解锁" in response.json()["detail"]


def test_hotel_lock_preserves_every_original_stay_date(edit_context):
    _, _, payload = edit_context
    hotel = payload.plan.recommended_hotel
    payload.plan.place_locks = [
        PlaceTarget(
            day_index=1, kind="hotel", name=hotel.name, address=hotel.location.address
        )
    ]
    after = payload.plan.model_copy(deep=True)
    after.daily_stays[2].end_hotel = None
    after.daily_stays[2].start_hotel = None
    with pytest.raises(ValueError, match="已锁定"):
        assert_locks_preserved(payload.plan, after)


def test_lunch_candidates_do_not_replace_a_meal_with_sweets(edit_context):
    planner, service, payload = edit_context
    meal = next(
        item for item in payload.plan.daily_plans[0].items if item.meal_type == "lunch"
    )
    payload.target = PlaceTarget(
        day_index=1,
        kind="meal",
        name=meal.location_name,
        address=meal.location_address,
        meal_type="lunch",
    )
    original = planner.backend.search_restaurants.return_value["restaurants"][0]
    planner.backend.search_restaurants.return_value["restaurants"].extend(
        [
            {**original, "name": "咖啡甜品店"},
            {**original, "name": "老字号咸蛋黄"},
        ]
    )
    assert [item.name for item in service.candidates(payload).candidates] == ["新餐厅"]

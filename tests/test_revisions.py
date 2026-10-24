from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.models import (
    Attraction,
    BudgetBreakdown,
    DailyStayPlan,
    DayPlan,
    DayPlanItem,
    HotelOption,
    HotelResearch,
    PlaceTarget,
    RoutePlan,
    TravelRequest,
    TripPlan,
    WeatherInfo,
)
from src.models_revision import (
    DayAttractionAssignment,
    DayRevisionRule,
    RevisionIntent,
    RevisionSelection,
    TravelRevisionRequest,
    TravelRevisionResponse,
)
from src.services.planner import TravelPlannerService
from src.services.revision import TravelRevisionService


@pytest.fixture
def context():
    planner = TravelPlannerService()
    planner.backend.amap_key = ""
    planner.backend.qweather_key = ""
    request = TravelRequest(
        city="杭州",
        start_date="2026-10-24",
        end_date="2026-10-26",
        budget_min=1200,
        budget_max=2500,
        travelers=2,
        pace="relaxed",
    )
    attractions = [
        Attraction.model_validate(raw)
        for raw in planner.backend.search_attractions("杭州", target_count=24)[
            "attractions"
        ]
    ]
    hotel = HotelOption.model_validate(
        planner.backend.search_hotels("杭州", budget_max=2500, stay_nights=2)[
            "candidates"
        ][0]
    )
    days = []
    for index, attraction in enumerate(attractions[:3]):
        day_date = request.start_date + timedelta(days=index)
        days.append(
            DayPlan(
                date=day_date,
                weather=WeatherInfo(
                    date=day_date,
                    condition="sunny",
                    high_c=24,
                    low_c=16,
                    suggestion="正常出游",
                ),
                route_summary="原始路线",
                items=[
                    DayPlanItem(
                        time_range="10:00-12:00",
                        title=attraction.name,
                        item_type="attraction",
                        location_name=attraction.name,
                        summary="原始安排",
                        estimated_cost=attraction.ticket_price,
                    )
                ],
            )
        )
    plan = TripPlan(
        city="杭州",
        travel_theme="杭州三日游",
        overview="原始概览",
        trip_days=3,
        selected_attractions=attractions[:3],
        recommended_hotel=hotel,
        hotel_candidates=[hotel],
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
        daily_plans=days,
        budget=BudgetBreakdown(
            hotel=600, attractions=50, food=200, transport=50, contingency=72, total=972
        ),
    )
    service = TravelRevisionService(planner)
    planner.runtime = SimpleNamespace(available=True)
    service.agent = Mock()

    def plan_day(req, *, day_context, constraint_context):
        assigned = day_context["assigned_attractions"]
        return {
            "date": day_context["date"],
            "weather": day_context["weather"],
            "route_summary": "新路线",
            "items": [
                {
                    "time_range": f"{9 + i * 3:02}:00-{11 + i * 3:02}:00",
                    "title": item["name"],
                    "location_name": item["name"],
                    "item_type": "attraction",
                    "summary": "调整后的安排",
                }
                for i, item in enumerate(assigned)
            ],
            "meal_intents": [
                {
                    "meal_type": meal,
                    "time_range": time,
                    "anchor_name": assigned[0]["name"],
                    "cuisine_intent": "清淡本地餐饮",
                    "budget_total": 80,
                }
                for meal, time in [
                    ("breakfast", "07:30-08:00"),
                    ("lunch", "12:00-13:00"),
                    ("dinner", "18:00-19:00"),
                ]
            ],
        }, {}

    planner.itinerary_agent = SimpleNamespace(
        plan_day=plan_day,
        repair_day=Mock(side_effect=AssertionError("Should not need repair")),
    )
    payload = TravelRevisionRequest(
        request=request, plan=plan, message="第二天最多一个景点"
    )
    service.agent.interpret.return_value = RevisionIntent(
        action="revise",
        message="调整第二天",
        target_days=[2],
        max_attractions_per_day=1,
    )
    museum = next(item.name for item in attractions if "博物馆" in item.name)
    service.agent.select.return_value = RevisionSelection(
        assignments=[DayAttractionAssignment(day_index=2, attraction_names=[museum])]
    )
    return planner, service, payload


def test_scoped_revision_preserves_other_days_and_recalculates_budget(context):
    planner, service, payload = context
    original = payload.model_dump_json()
    result = service.revise(payload)
    assert result.status == "revised"
    assert result.plan.daily_plans[0] == payload.plan.daily_plans[0]
    assert result.plan.daily_plans[2] == payload.plan.daily_plans[2]
    assert result.plan.daily_plans[1] != payload.plan.daily_plans[1]
    assert any(
        item.item_type == "transport" for item in result.plan.daily_plans[1].items
    )
    budget = result.plan.budget
    assert budget.total == round(
        budget.hotel
        + budget.attractions
        + budget.food
        + budget.transport
        + budget.contingency,
        2,
    )
    assert payload.model_dump_json() == original
    assert result.day_rules["2"].max_attractions == 1


def test_followup_preserves_previous_indoor_and_quantity_rules(context):
    _, service, payload = context
    first = service.revise(payload)
    followup = TravelRevisionRequest(
        request=first.request,
        plan=first.plan,
        message="第二天午饭吃清淡一点",
        day_rules={
            "2": DayRevisionRule(
                indoor_only=True, max_attractions=1, instruction="雨天改室内"
            )
        },
    )
    service.agent.interpret.return_value = RevisionIntent(
        action="revise", message="调整用餐", target_days=[2]
    )
    result = service.revise(followup)
    assert result.day_rules["2"].indoor_only is True
    assert result.day_rules["2"].max_attractions == 1
    assert "雨天改室内" in result.day_rules["2"].instruction
    assert result.plan.daily_plans[1].weather == payload.plan.daily_plans[1].weather


@pytest.mark.parametrize(
    "names", [["不存在的景点"], ["中国茶叶博物馆", "中国茶叶博物馆"]]
)
def test_rejects_ungrounded_or_duplicate_places_without_mutating_plan(context, names):
    _, service, payload = context
    original = payload.model_dump_json()
    service.agent.interpret.return_value.max_attractions_per_day = None
    service.agent.select.return_value = RevisionSelection(
        assignments=[DayAttractionAssignment(day_index=2, attraction_names=names)]
    )
    with pytest.raises(ValueError):
        service.revise(payload)
    assert payload.model_dump_json() == original


def test_no_indoor_candidate_asks_for_clarification(context):
    planner, service, payload = context
    # The sole indoor venue is reserved on an unaffected date.
    payload.plan.daily_plans[0].items[0].location_name = "中国茶叶博物馆"
    payload.plan.daily_plans[1].items[0].location_name = "西湖"
    service.agent.interpret.return_value = RevisionIntent(
        action="revise", message="第二天改室内", target_days=[2], indoor_only=True
    )
    result = service.revise(payload)
    assert result.status == "clarification"
    assert result.plan == payload.plan
    assert result.day_rules == payload.day_rules
    service.agent.select.assert_not_called()


@pytest.mark.parametrize(
    "action,status", [("clarify", "clarification"), ("answer", "answered")]
)
def test_questions_and_ambiguous_requests_do_not_change_plan(context, action, status):
    _, service, payload = context
    service.agent.interpret.return_value = RevisionIntent(
        action=action, message="请说明要修改哪一天"
    )
    result = service.revise(payload)
    assert result.status == status
    assert result.plan == payload.plan
    service.agent.select.assert_not_called()


def test_unachievable_budget_is_reported_instead_of_claimed_satisfied(context):
    planner, service, payload = context
    service.agent.interpret.return_value = RevisionIntent(
        action="revise",
        message="降低总预算",
        target_days=[1, 2, 3],
        request_updates={"budget_min": 100, "budget_max": 200},
    )
    names = [item.name for item in payload.plan.selected_attractions]
    service.agent.select.return_value = RevisionSelection(
        assignments=[
            DayAttractionAssignment(day_index=i + 1, attraction_names=[name])
            for i, name in enumerate(names)
        ]
    )
    result = service.revise(payload)
    assert result.request.budget_max == 200
    assert result.plan.budget.total > 200
    assert any("仍高于" in text for text in result.warnings)


def test_plan_context_must_match_dates(context):
    _, _, payload = context
    raw = payload.model_dump(mode="json")
    raw["plan"]["daily_plans"][0]["date"] = "2026-10-23"
    with pytest.raises(ValidationError, match="日期"):
        TravelRevisionRequest.model_validate(raw)


def test_api_rejects_more_than_seven_days(context):
    _, _, payload = context
    with pytest.raises(ValidationError, match="1-7"):
        TravelRequest.model_validate(
            {**payload.request.model_dump(), "end_date": "2026-11-01"}
        )


def test_revision_task_stream_returns_typed_result_and_retains_replay(
    context, monkeypatch
):
    import src.app as app_module

    _, _, payload = context
    expected = TravelRevisionResponse(
        status="answered",
        message="住宿花费最高",
        request=payload.request,
        plan=payload.plan,
    )
    monkeypatch.setattr(
        app_module.revision_service, "revise", lambda *args, **kwargs: expected
    )
    client = TestClient(app_module.app)
    response = client.post(
        "/api/plan/revisions/tasks", json=payload.model_dump(mode="json")
    )
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    stream = client.get(f"/api/plan/stream/{task_id}")
    assert "event: complete" in stream.text
    assert '"revision"' in stream.text
    assert '"status": "answered"' in stream.text
    snapshot = client.get(f"/api/plan/tasks/{task_id}").json()
    assert snapshot["status"] == "completed"
    assert snapshot["result"]["revision"]["status"] == "answered"
    assert client.get(f"/api/plan/stream/{task_id}").text == stream.text


def test_echoed_unchanged_settings_do_not_expand_revision_scope(context):
    _, service, payload = context
    service.agent.interpret.return_value.request_updates.pace = payload.request.pace
    result = service.revise(payload)
    assert result.plan.daily_plans[0] == payload.plan.daily_plans[0]
    assert result.plan.daily_plans[2] == payload.plan.daily_plans[2]


def test_hotel_change_updates_all_days_and_charged_nights(context, monkeypatch):
    planner, service, payload = context
    hotel = payload.plan.recommended_hotel.model_copy(
        update={"name": "测试用经济酒店", "nightly_price": 250}
    )
    monkeypatch.setattr(
        planner,
        "_build_hotel_research_with_repair",
        lambda *args: HotelResearch(candidates=[hotel], recommended_hotel=hotel),
    )
    service.agent.interpret.return_value = RevisionIntent(
        action="revise", message="降低住宿费用", target_days=[2], change_hotel=True
    )
    service.agent.select.return_value = RevisionSelection(
        assignments=[
            DayAttractionAssignment(day_index=i + 1, attraction_names=[item.name])
            for i, item in enumerate(payload.plan.selected_attractions)
        ]
    )
    result = service.revise(payload)
    assert all(
        stay.start_hotel.name == hotel.name and stay.end_hotel.name == hotel.name
        for stay in result.plan.daily_stays
    )
    assert (
        sum(stay.charged_night for stay in result.plan.daily_stays)
        == payload.request.stay_nights
    )
    assert result.plan.budget.hotel == 500
    assert result.plan.recommended_hotel.name == hotel.name


def test_revision_cannot_remove_locked_attraction(context):
    planner, service, payload = context
    item = payload.plan.daily_plans[1].items[0]
    payload.plan.place_locks = [
        PlaceTarget(
            day_index=2,
            kind="attraction",
            name=item.location_name,
            address=item.location_address,
        )
    ]
    # A valid selection can still violate a user's lock.
    scheduled = {point.name for point in payload.plan.selected_attractions}
    other = next(
        point["name"]
        for point in planner.backend.search_attractions("杭州", target_count=24)[
            "attractions"
        ]
        if point["name"] not in scheduled
    )
    service.agent.select.return_value = RevisionSelection(
        assignments=[DayAttractionAssignment(day_index=2, attraction_names=[other])]
    )
    before = payload.model_dump_json()
    with pytest.raises(ValueError, match="已锁定"):
        service.revise(payload)
    assert payload.model_dump_json() == before


def test_revision_preserves_locked_attraction_in_old_history(context):
    _, service, payload = context
    item = payload.plan.daily_plans[1].items[0]
    payload.plan.place_locks = [
        PlaceTarget(day_index=2, kind="attraction", name=item.location_name, address="")
    ]
    service.agent.select.return_value = RevisionSelection(
        assignments=[
            DayAttractionAssignment(day_index=2, attraction_names=[item.location_name])
        ]
    )
    result = service.revise(payload)
    selected = next(
        item
        for item in result.plan.daily_plans[1].items
        if item.item_type == "attraction"
    )
    assert selected.location_name == payload.plan.place_locks[0].name
    assert selected.location_address == ""
    assert selected.location is not None
    assert result.plan.place_locks == payload.plan.place_locks


def test_conversational_revision_reuses_locked_meal(context):
    planner, service, payload = context
    museum = next(
        point for point in payload.plan.selected_attractions if "博物馆" in point.name
    )
    meal = DayPlanItem(
        time_range="12:00-13:00",
        title="已选清淡餐厅",
        item_type="meal",
        meal_type="lunch",
        location_name="已选清淡餐厅",
        location_address="原始分店地址",
        location=museum.location,
        summary="已选本地清淡用餐",
        estimated_cost=88,
    )
    payload.plan.daily_plans[1].items.append(meal)
    payload.plan.place_locks = [
        PlaceTarget(
            day_index=2,
            kind="meal",
            meal_type="lunch",
            name=meal.location_name,
            address=meal.location_address,
        )
    ]
    planner.backend.search_restaurants = Mock(
        return_value={"data_mode": "unavailable", "restaurants": []}
    )
    result = service.revise(payload)
    kept = next(
        item for item in result.plan.daily_plans[1].items if item.meal_type == "lunch"
    )
    assert kept.location_name == meal.location_name
    assert kept.location_address == meal.location_address
    assert kept.estimated_cost == 88
    assert result.plan.place_locks == payload.plan.place_locks


def test_locked_hotel_blocks_conversational_hotel_change_before_research(context):
    planner, service, payload = context
    hotel = payload.plan.recommended_hotel
    payload.plan.place_locks = [
        PlaceTarget(
            day_index=1, kind="hotel", name=hotel.name, address=hotel.location.address
        )
    ]
    service.agent.interpret.return_value = RevisionIntent(
        action="revise", message="换酒店", target_days=[2], change_hotel=True
    )
    planner._build_hotel_research_with_repair = Mock()
    with pytest.raises(ValueError, match="住宿已锁定"):
        service.revise(payload)
    planner._build_hotel_research_with_repair.assert_not_called()


def test_impossible_revision_preserves_original_plan(context, monkeypatch):
    planner, service, payload = context
    original = payload.model_dump_json()

    def long_routes(req, targets):
        return [
            RoutePlan(
                city=req.city,
                mode=req.transport_mode,
                origin=a.name,
                destination=b.name,
                distance_km=55,
                duration_min=381,
                estimated_cost=20,
                summary="跨区交通",
            )
            for a, b in zip(targets, targets[1:])
        ]

    monkeypatch.setattr(planner, "_build_day_route_segments", long_routes)
    with pytest.raises(ValueError, match="无法在正常时段完成|超出"):
        service.revise(payload)
    assert payload.model_dump_json() == original

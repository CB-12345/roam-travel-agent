from datetime import date
from unittest.mock import Mock

import pytest

from src.mcp.travel_backend import TravelDataBackend
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
    TravelRequest,
    TripPlan,
    WeatherInfo,
)
from src.services.planner import TravelPlannerService
from src.services.planning_quality import distance_km, schedule_day, time_bounds
from src.services.opening_hours import parse_opening_hours


def attraction(name="榕城区博物馆", lat=23.532864, lng=116.355282):
    return Attraction(
        name=name,
        category="博物馆",
        summary="城市文化",
        recommended_hours=2,
        location=Location(name=name, address="揭阳市榕城区", lat=lat, lng=lng),
    )


def hotel(name="惠来宾馆", lat=23.028306, lng=116.299151):
    return HotelOption(
        name=name,
        style="budget",
        nightly_price=160,
        summary="参考住宿",
        nearby_area="揭阳",
        location=Location(name=name, address=name, lat=lat, lng=lng),
    )


def request(**changes):
    return TravelRequest.model_validate(
        dict(
            city="揭阳",
            start_date="2026-10-24",
            end_date="2026-10-24",
            budget_min=1500,
            budget_max=2500,
            travelers=2,
            **changes,
        )
    )


def day(items):
    return DayPlan(
        date=date(2026, 10, 24),
        route_summary="",
        items=items,
        weather=WeatherInfo(
            date=date(2026, 10, 24),
            condition="unknown",
            high_c=None,
            low_c=None,
            suggestion="待确认",
        ),
    )


def event(name, time="09:00-11:00", kind="attraction", **kwargs):
    return DayPlanItem(
        time_range=time,
        title=name,
        item_type=kind,
        location_name=name,
        summary="",
        **kwargs,
    )


def test_cross_district_hotel_is_replaced_using_verified_coordinates():
    planner = TravelPlannerService()
    point, far, near = attraction(), hotel(), hotel("榕城酒店", 23.534, 116.356)
    planner.backend.search_hotels = Mock(
        return_value={
            "data_mode": "live",
            "candidates": [far.model_dump(), near.model_dump()],
        }
    )
    stays = [
        DailyStayPlan(
            day_index=1, date=date(2026, 10, 24), start_hotel=far, end_hotel=far
        )
    ]
    skeleton = {
        "assignments": {1: [point]},
        "daily_stays": stays,
        "recommended_hotel": far,
    }
    hotels = HotelResearch(candidates=[far], recommended_hotel=far)
    planner._align_hotels_with_itinerary(request(), skeleton, hotels, {})
    assert distance_km(far.location, point.location) > 50
    assert stays[0].start_hotel == stays[0].end_hotel == near
    assert hotels.recommended_hotel == near
    assert far not in hotels.candidates
    assert (
        planner.backend.search_hotels.call_args.kwargs["anchor_location"]
        == point.location.model_dump()
    )
    assert "距离游玩区域较远" in skeleton["notes"][0]


def test_research_repair_can_preview_long_candidate_addresses():
    planner = TravelPlannerService()
    preview = planner._candidate_pool_preview(
        [
            {"name": "景点", "address": "很长的地址" * 30},
            {
                "name": "酒店",
                "nightly_price": 160,
                "location": {"address": "酒店地址" * 30},
            },
        ]
    )
    assert len(preview) == 2
    assert all(len(item["address_hint"]) == 48 for item in preview)


def test_unavailable_nearby_hotel_does_not_keep_remote_first_hit():
    planner = TravelPlannerService()
    far = hotel()
    planner.backend.search_hotels = Mock(return_value={"candidates": []})
    skeleton = {
        "assignments": {1: [attraction()]},
        "daily_stays": [
            DailyStayPlan(
                day_index=1, date=date(2026, 10, 24), start_hotel=far, end_hotel=far
            )
        ],
    }
    with pytest.raises(ValueError, match="未找到可靠的附近酒店"):
        planner._align_hotels_with_itinerary(
            request(), skeleton, HotelResearch(candidates=[far]), {}
        )


def test_meals_ignore_remote_anchor_and_out_of_radius_restaurants():
    planner = TravelPlannerService()
    point = attraction()
    near = {
        "name": "榕城小吃店",
        "avg_cost_per_person": 35,
        "location": {"name": "榕城小吃店", "lat": 23.534, "lng": 116.356},
    }
    far = {
        "name": "惠来美食店",
        "location": {"name": "惠来美食店", "lat": 23.03, "lng": 116.3},
    }
    planner.backend.search_restaurants = Mock(
        return_value={"data_mode": "live", "restaurants": [far, near]}
    )
    intent = MealIntent(
        meal_type="lunch",
        time_range="12:00-13:00",
        anchor_name="惠来夜市",
        cuisine_intent="本地菜",
    )
    context = {"assigned_attractions": [point.model_dump()]}
    result = planner._search_restaurant_for_intent(intent, request(), context, {})
    assert result.name == "榕城小吃店"
    assert planner.backend.search_restaurants.call_args.kwargs["anchor"] == point.name
    assert (
        planner.backend.search_restaurants.call_args.kwargs["anchor_location"]
        == point.location.model_dump()
    )
    planner.backend.search_restaurants.return_value = {
        "data_mode": "live",
        "restaurants": [far],
    }
    assert planner._search_restaurant_for_intent(intent, request(), context, {}) is None
    planner.backend.search_restaurants.return_value = {
        "data_mode": "fallback",
        "restaurants": [near],
    }
    assert planner._search_restaurant_for_intent(intent, request(), context, {}) is None


@pytest.mark.parametrize(
    "meal_type,time_range,hours,reject_first",
    [
        ("breakfast", "08:00-08:30", "10:00-24:00", True),
        ("lunch", "12:00-13:00", "10:00-24:00", False),
        ("dinner", "18:00-19:00", "11:00-14:00", True),
        ("breakfast", "08:00-09:00", "09:30-10:00", True),
        ("breakfast", "08:00-08:30", "", False),
        ("breakfast", "08:00-08:30", "节假日另行调整", False),
    ],
)
def test_meal_search_filters_known_hours_before_committing_a_restaurant(
    meal_type, time_range, hours, reject_first
):
    planner = TravelPlannerService()
    planner.backend.amap_key = "test-only"
    point = attraction()
    names = ["最近餐厅", "全天餐厅"]
    options = [
        {
            "name": name,
            "location": {
                "name": name,
                "address": "榕城测试地址",
                "lat": point.location.lat + index * 0.0001,
                "lng": point.location.lng,
            },
        }
        for index, name in enumerate(names)
    ]
    planner.backend.search_restaurants = Mock(
        return_value={"data_mode": "live", "restaurants": options}
    )

    def facts(url, parameters, **kwargs):
        name = parameters["keywords"]
        option = options[names.index(name)]
        loc = option["location"]
        return {
            "status": "1",
            "pois": [
                {
                    "name": name,
                    "address": loc["address"],
                    "location": f"{loc['lng']},{loc['lat']}",
                    "biz_ext": {"opentime2": hours if name == names[0] else "全天营业"},
                }
            ],
        }

    planner.backend._amap_get = Mock(side_effect=facts)
    intent = MealIntent(
        meal_type=meal_type,
        time_range=time_range,
        anchor_name=point.name,
        cuisine_intent="本地餐饮",
    )
    context = {"assigned_attractions": [point.model_dump()]}
    result = planner._search_restaurant_for_intent(intent, request(), context, {})
    assert result.name == names[int(reject_first)]
    # A repeated query reuses verified POI facts instead of spending more quota.
    calls = planner.backend._amap_get.call_count
    assert (
        planner._search_restaurant_for_intent(intent, request(), context, {}) == result
    )
    assert planner.backend._amap_get.call_count == calls


def test_meal_materialization_uses_actual_day_and_falls_back_without_fabricated_shop():
    planner = TravelPlannerService()
    planner.backend.amap_key = "test-only"
    point = attraction()
    option = {"name": "周日休息餐厅", "location": point.location.model_dump()}
    planner.backend.search_restaurants = Mock(
        return_value={"data_mode": "live", "restaurants": [option]}
    )
    planner.backend._amap_get = Mock(
        return_value={
            "status": "1",
            "pois": [
                {
                    "name": option["name"],
                    "location": f"{point.location.lng},{point.location.lat}",
                    "biz_ext": {"opentime2": "周日休息"},
                }
            ],
        }
    )
    planned = day([])
    planned.date = date(2026, 10, 25)  # Sunday; request and stale context are Saturday.
    planned.meal_intents = [
        MealIntent(
            meal_type="lunch",
            time_range="12:00-13:00",
            anchor_name=point.name,
            cuisine_intent="本地餐饮",
            budget_total=80,
        )
    ]
    planner._materialize_meal_intents(
        planned,
        request(),
        {"date": "2026-10-24", "assigned_attractions": [point.model_dump()]},
        {},
        {},
    )
    assert len(planned.items) == 1
    item = planned.items[0]
    assert item.is_route_stop is False
    assert item.location is None and item.location_address == ""
    assert item.estimated_cost == 80
    assert planner.backend._amap_get.call_count == 1


def test_locked_meal_is_preserved_without_silent_hours_based_replacement():
    planner = TravelPlannerService()
    planner.backend.search_restaurants = Mock(side_effect=AssertionError("locked meal"))
    meal = event("已锁定餐厅", "12:00-13:00", "meal", meal_type="lunch")
    planned = day([])
    planned.meal_intents = [
        MealIntent(
            meal_type="lunch",
            time_range="11:30-12:30",
            anchor_name="已锁定餐厅",
            cuisine_intent="本地餐饮",
        )
    ]
    planner._materialize_meal_intents(planned, request(), {}, {}, {}, [meal])
    assert planned.items[0].location_name == meal.location_name
    assert planned.items[0].time_range == "11:30-12:30"


def test_lunch_follows_actual_morning_activity_not_assignment_order():
    planner = TravelPlannerService()
    intent = MealIntent(
        meal_type="lunch",
        time_range="12:00-13:00",
        anchor_name="晚间商圈",
        cuisine_intent="本地菜",
    )
    context = {
        "assigned_attractions": [{"name": "上午景点"}, {"name": "下午景点"}],
        "activity_timeline": [
            {"name": "上午景点", "time_range": "09:00-11:30"},
            {"name": "下午景点", "time_range": "14:00-16:00"},
        ],
    }
    assert planner._meal_search_anchors(intent, context)[0] == "上午景点"


def test_branch_names_keep_coordinates_for_meals_and_route_injection():
    planner = TravelPlannerService()
    point = attraction()
    lodging = hotel("维也纳酒店(进贤门店)", 23.534, 116.356)
    intent = MealIntent(
        meal_type="breakfast",
        time_range="08:00-08:30",
        anchor_name=lodging.name,
        cuisine_intent="早餐",
    )
    context = {
        "start_hotel": lodging.model_dump(),
        "assigned_attractions": [point.model_dump()],
    }
    assert planner._meal_search_anchors(intent, context)[0] == lodging.name
    planner.backend.search_restaurants = Mock(
        return_value={"data_mode": "live", "restaurants": []}
    )
    planner._search_restaurant_for_intent(intent, request(), context, {})
    assert (
        planner.backend.search_restaurants.call_args_list[0].kwargs["anchor_location"]
        == lodging.location.model_dump()
    )
    catalog = planner._build_route_target_catalog(
        AttractionResearch(city_overview="", selected_attractions=[point]),
        HotelResearch(candidates=[lodging]),
        {},
    )
    stay = DailyStayPlan(
        day_index=1, date=date(2026, 10, 24), start_hotel=lodging, end_hotel=lodging
    )
    targets = planner._collect_day_route_targets([event(point.name)], stay, catalog)
    assert targets[0].name == targets[-1].name == lodging.name
    assert targets[0].lat == lodging.location.lat
    assert targets[0].lng == lodging.location.lng


def test_transport_pushes_activity_without_shortening_visit():
    point = attraction()
    plan = day(
        [
            event("前往博物馆", "交通 1", "transport", duration_min=140),
            event(point.name),
        ]
    )
    assert schedule_day(plan, {point.name: point})
    assert plan.items[1].time_range == "09:30-11:30"
    assert plan.items[0].duration_min == 140
    assert (
        time_bounds(plan.items[0].time_range)[1] + 10
        == time_bounds(plan.items[1].time_range)[0]
    )


def test_overlong_day_is_rejected_transactionally():
    point = attraction()
    plan = day(
        [
            event("去程", "交通 1", "transport", duration_min=381),
            event(point.name),
            event("午餐", "12:00-13:00", "meal", meal_type="lunch"),
            event("返程", "交通 2", "transport", duration_min=381),
        ]
    )
    before = plan.model_dump_json()
    with pytest.raises(ValueError, match="无法在正常时段完成|超出"):
        schedule_day(plan, {point.name: point})
    assert plan.model_dump_json() == before


@pytest.mark.parametrize(
    "value", ["9点", "09:00-08:00", "25:00-26:00", "09:70-11:00", ""]
)
def test_invalid_activity_time_is_not_treated_as_zero_duration(value):
    with pytest.raises(ValueError, match="有效的起止时间"):
        schedule_day(day([event("景点", value)]), {})


def test_poi_hotel_allowance_does_not_inflate_with_user_budget_or_invent_stars():
    backend = TravelDataBackend(amap_key="test")
    backend.geocode_city = Mock(return_value={"adcode": "445200"})
    backend._amap_get = Mock(
        return_value={
            "status": "1",
            "pois": [
                {
                    "name": "揭阳中欧宾馆",
                    "type": "住宿服务;宾馆酒店",
                    "location": "116.299151,23.028306",
                }
            ],
        }
    )
    cheap = backend.search_hotels("揭阳", budget_max=1000)["candidates"][0]
    expensive = backend.search_hotels("揭阳", budget_max=10000, hotel_style="premium")[
        "candidates"
    ][0]
    assert cheap["nightly_price"] == expensive["nightly_price"] == 160
    assert expensive["nightly_price_min"] == 100
    assert expensive["nightly_price_max"] == 220
    assert expensive["star_level"] == 0
    assert expensive["style"] == "budget"
    assert expensive["booking_url"] == ""
    # Amap's generic "宾馆酒店" category includes ordinary hotels as well.
    backend._amap_get.return_value["pois"][0]["name"] = "名都大酒店"
    generic = backend.search_hotels("揭阳", budget_max=2500)["candidates"][0]
    assert generic["nightly_price"] == 380
    assert generic["style"] == "comfort"


def test_live_restaurant_search_uses_supplied_coordinates_and_rejects_remote_poi():
    backend = TravelDataBackend(amap_key="test")
    backend.geocode_city = Mock(return_value={"adcode": "445200"})
    backend._geocode_poi = Mock(
        side_effect=AssertionError("must not resolve same-name anchor")
    )
    backend._amap_get = Mock(
        return_value={
            "status": "1",
            "pois": [{"name": "远处店", "address": "惠来", "location": "116.3,23.03"}],
        }
    )
    result = backend.search_restaurants(
        "揭阳", anchor="同名博物馆", anchor_location=attraction().location.model_dump()
    )
    assert not result["restaurants"]
    backend._geocode_poi.assert_not_called()


def test_overview_and_budget_only_count_scheduled_places_and_charged_room_nights():
    planner = TravelPlannerService()
    planner.backend.amap_key = ""
    point, spare, lodging = (
        attraction(),
        attraction("未安排景点"),
        hotel("榕城酒店", 23.534, 116.356),
    )
    req = request().model_copy(update={"travelers": 3})
    stays = [
        DailyStayPlan(
            day_index=1, date=req.start_date, start_hotel=lodging, end_hotel=lodging
        )
    ]
    plan = TripPlan(
        city=req.city,
        travel_theme="文化游",
        overview="错误的三个景点",
        trip_days=1,
        selected_attractions=[point, spare],
        recommended_hotel=lodging,
        daily_stays=stays,
        daily_plans=[day([event(point.name, estimated_cost=20)])],
        budget=BudgetBreakdown(
            hotel=0, attractions=0, food=0, transport=0, contingency=0, total=0
        ),
    )
    planner._check_plan_schedule(
        plan,
        req,
        AttractionResearch(city_overview="", selected_attractions=[point, spare]),
    )
    planner._refresh_plan_budget(plan, req, stays)
    assert plan.selected_attractions == [point]
    assert "1 处景点、1 晚住宿" in plan.overview
    assert plan.budget.hotel == 320
    assert plan.budget.total == 367.2
    plan.risk_alerts.append(
        "部分用餐尚未匹配到可靠餐厅，仅提供附近区域建议；确定具体店铺后需复核距离和时间。"
    )
    planner._check_plan_schedule(
        plan,
        req,
        AttractionResearch(city_overview="", selected_attractions=[point, spare]),
    )
    assert not plan.risk_alerts
    assert sum(note.startswith("住宿按 ") for note in plan.notes) == 1
    with pytest.raises(ValueError, match="住宿晚数"):
        planner._calculate_hotel_cost([], req)


def test_opening_time_can_move_a_late_preferred_visit_earlier_without_shortening():
    point = attraction()
    plan = day(
        [
            event("前往", "07:00-08:00", "transport", duration_min=60),
            event(point.name, "16:00-18:00"),
        ]
    )
    assert schedule_day(
        plan, {point.name: point}, {1: parse_opening_hours("09:00-17:00", plan.date)}
    )
    assert plan.items[1].time_range == "09:00-11:00"


def test_split_shop_hours_intersect_with_normal_meal_window_and_update_intent():
    plan = day([event("午餐店", "12:00-13:00", "meal", meal_type="lunch")])
    plan.meal_intents = [
        MealIntent(
            meal_type="lunch",
            time_range="12:00-13:00",
            anchor_name="午餐店",
            cuisine_intent="本地餐饮",
        )
    ]
    assert schedule_day(
        plan, {}, {0: parse_opening_hours("11:00-12:00,17:00-21:00", plan.date)}
    )
    assert plan.items[0].time_range == plan.meal_intents[0].time_range == "11:00-12:00"


@pytest.mark.parametrize("hours", ["09:00-10:00", "周六闭馆", "17:00-21:00"])
def test_impossible_hours_preserve_items_and_meal_intents(hours):
    plan = day(
        [
            event("上午活动", "09:00-11:00"),
            event("午餐店", "12:00-13:00", "meal", meal_type="lunch"),
        ]
    )
    plan.meal_intents = [
        MealIntent(
            meal_type="lunch",
            time_range="12:00-13:00",
            anchor_name="午餐店",
            cuisine_intent="本地餐饮",
        )
    ]
    before = plan.model_dump_json()
    with pytest.raises(ValueError, match="无法容纳完整停留"):
        schedule_day(plan, {}, {1: parse_opening_hours(hours, plan.date)})
    assert plan.model_dump_json() == before


def test_planner_uses_verified_hours_and_overwrites_client_verdict():
    from src.models_opening_hours import OpeningVisitCheck

    planner = TravelPlannerService()
    planner.backend.amap_key = "test-only"
    point = attraction()
    planner.backend._amap_get = Mock(
        return_value={
            "status": "1",
            "pois": [
                {
                    "id": "B123456789",
                    "name": point.name,
                    "address": "揭阳市榕城区",
                    "location": "116.355282,23.532864",
                    "biz_ext": {"opentime2": "10:00-17:00"},
                }
            ],
        }
    )
    item = event(point.name)
    item.opening_check = OpeningVisitCheck(
        date="2026-10-24",
        time_range=item.time_range,
        status="fits",
        message="forged",
        raw_text="24小时营业",
    )
    plan = TripPlan(
        city="揭阳",
        travel_theme="文化游",
        overview="",
        trip_days=1,
        selected_attractions=[point],
        daily_plans=[day([item, event("酒店", "20:00-21:00", "hotel")])],
        budget=BudgetBreakdown(
            hotel=0, attractions=0, food=0, transport=0, contingency=0, total=0
        ),
    )
    planner._check_plan_schedule(
        plan,
        request(),
        AttractionResearch(city_overview="", selected_attractions=[point]),
    )
    final = plan.daily_plans[0].items[0]
    assert final.time_range == "10:00-12:00"
    assert final.opening_check.status == "fits"
    assert final.opening_check.raw_text == "10:00-17:00"
    assert plan.daily_plans[0].items[1].opening_check is None
    assert planner.backend._amap_get.call_count == 1

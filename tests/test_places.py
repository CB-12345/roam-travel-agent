from copy import deepcopy
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from src.mcp.travel_backend import TravelDataBackend
from src.services.places import PlaceLookupRequest, PlaceReference, PlaceService


def poi(**overrides):
    return {
        "id": "B0FFL3A6KT",
        "name": "揭阳古城中山骑楼街",
        "pname": "广东省",
        "cityname": "揭阳市",
        "adname": "榕城区",
        "address": "望江北路26号",
        "location": "116.353638,23.533416",
        "photos": [{"url": "https://store.is.autonavi.com/showpic/example"}],
        **overrides,
    }


def service(*pois, key="test-only"):
    backend = TravelDataBackend(amap_key=key)
    backend._amap_get = Mock(return_value={"status": "1", "pois": list(pois)})
    return PlaceService(backend)


def resolve(svc, **overrides):
    ref = PlaceReference(**{"key": "first", "name": "揭阳古城中山骑楼街", **overrides})
    return svc.resolve(PlaceLookupRequest(city="揭阳", places=[ref])).places[0]


def test_real_poi_id_resolves_full_address_photo_and_coordinate_order():
    svc = service(poi())
    result = resolve(svc, poi_id="B0FFL3A6KT")
    assert result.verified
    assert result.address == "广东省揭阳市榕城区望江北路26号"
    assert result.photo_source == "高德地点照片"
    assert result.photo_url.startswith("https://store.is.autonavi.com/")
    assert parse_qs(urlsplit(result.map_url).query)["position"] == [
        "116.353638,23.533416"
    ]
    assert "key=" not in result.map_url
    assert svc.backend._amap_get.call_args.args[0].endswith("/place/detail")


def test_duplicate_names_are_resolved_by_original_coordinates():
    svc = service(poi(location="116.3,23.0"), poi())
    result = resolve(svc, lat=23.533416, lng=116.353638)
    assert result.verified
    assert result.lat == 23.533416


def test_duplicate_names_are_resolved_by_address_without_guessing_branch():
    svc = service(poi(address="另一条路"), poi())
    assert resolve(svc, address="望江北路26号").verified
    ambiguous = resolve(svc, key="second", address="")
    assert not ambiguous.verified
    assert urlsplit(ambiguous.map_url).path == "/search"


def test_far_away_same_name_does_not_replace_known_coordinates():
    result = resolve(service(poi()), lat=23.0, lng=116.3)
    assert not result.verified
    assert result.lat is None and result.lng is None
    assert "position=" not in result.map_url


def test_conflicting_address_is_not_accepted_even_for_one_search_result():
    result = resolve(service(poi()), address="另一条路99号")
    assert not result.verified
    assert result.address == "另一条路99号"


def test_different_branch_name_is_never_accepted():
    result = resolve(service(poi(name="揭阳古城中山骑楼街(新店)")))
    assert not result.verified


@pytest.mark.parametrize("location", ["0,0", "nan,23", "116,91", "invalid"])
def test_invalid_coordinates_fall_back_to_map_search(location):
    result = resolve(service(poi(location=location)))
    assert not result.verified
    assert not result.photo_url


def test_missing_key_and_provider_failure_keep_original_address():
    svc = service(key="")
    assert resolve(svc, address="原地址").address == "原地址"
    svc.backend._amap_get.assert_not_called()
    svc = service()
    svc.backend._amap_get.return_value = None
    result = resolve(svc, address="原地址")
    assert not result.verified
    assert result.address == "原地址"


def test_missing_photo_stays_empty_and_unsafe_photo_is_skipped():
    result = resolve(service(poi(photos=[])))
    assert result.verified and result.photo_url == ""
    result = resolve(
        service(
            poi(
                photos=[
                    {"url": "javascript:alert(1)"},
                    {"url": "https://user:secret@example.com/photo"},
                    {"url": "https://store.is.autonavi.com/real"},
                ]
            )
        )
    )
    assert result.photo_url == "https://store.is.autonavi.com/real"


def test_cache_reuses_provider_result_but_preserves_request_key_and_data():
    svc = service(poi())
    first = resolve(svc)
    expected = deepcopy(first)
    first.address = "consumer modification"
    second = resolve(svc, key="second")
    assert second.key == "second"
    assert second.address == expected.address
    assert svc.backend._amap_get.call_count == 1
    third = resolve(svc, address="另一分店的地址")
    assert third.key == "first"
    assert svc.backend._amap_get.call_count == 2


def test_complete_address_does_not_repeat_administrative_regions():
    result = resolve(service(poi(address="广东省揭阳市榕城区望江北路26号")))
    assert result.address == "广东省揭阳市榕城区望江北路26号"


def test_hours_cache_stores_facts_but_rechecks_each_date_and_visit():
    raw = "周二至周日09:00-17:00；周一闭馆"
    svc = service(poi(biz_ext={"opentime2": raw, "open_time": "24小时营业"}))
    first = resolve(svc, visits=[{"date": "2026-10-24", "time_range": "09:00-11:00"}])
    assert first.opening_hours == raw
    assert first.visit_checks[0].status == "fits"
    assert first.opening_hours_source == "高德地点开放时间"
    assert first.opening_hours_checked_at
    first.visit_checks[0].message = "consumer modification"
    second = resolve(
        svc,
        key="next",
        visits=[
            {"date": "2026-10-26", "time_range": "09:00-11:00"},
            {"date": "2026-10-24", "time_range": "16:00-18:00"},
        ],
    )
    assert [check.status for check in second.visit_checks] == ["closed", "conflict"]
    assert svc.backend._amap_get.call_count == 1
    assert resolve(svc).visit_checks == []


def test_unmatched_poi_cannot_lend_its_hours_to_a_different_branch():
    svc = service(
        poi(name="揭阳古城中山骑楼街(新店)", biz_ext={"opentime2": "24小时营业"})
    )
    result = resolve(svc, visits=[{"date": "2026-10-24", "time_range": "09:00-11:00"}])
    assert result.opening_hours == ""
    assert result.visit_checks[0].status == "unknown"


def test_legacy_hours_string_is_used_when_regular_hours_are_absent():
    result = resolve(service(poi(biz_ext={"opentime2": [], "open_time": "24小时营业"})))
    assert result.opening_hours == "24小时营业"

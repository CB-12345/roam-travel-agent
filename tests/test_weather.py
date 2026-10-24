from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import pytest
import requests

from src.mcp.qweather import QWeatherClient
from src.mcp.travel_backend import TravelDataBackend
from src.models import TravelRequest, WeatherInfo, WeatherResearch
from src.services.planner import TravelPlannerService


TEST_HOST = "test.re.qweatherapi.com"
TEST_KEY = "test-only-secret"


def response(payload, status=200):
    return Mock(status_code=status, json=Mock(return_value=payload))


def install_forecast(monkeypatch, daily):
    get = Mock(
        side_effect=[
            response(
                {"code": "200", "location": [{"name": "揭阳", "id": "101281901"}]}
            ),
            response(
                {"code": "200", "updateTime": "2026-10-23T20:00+08:00", "daily": daily}
            ),
        ]
    )
    monkeypatch.setattr("src.mcp.qweather.requests.get", get)
    return get


def raw_day(day="2026-10-24", high="28", low="21"):
    return {"fxDate": day, "textDay": "小雨", "tempMax": high, "tempMin": low}


def test_live_forecast_uses_host_header_auth_and_new_geo_path(monkeypatch):
    get = install_forecast(monkeypatch, [raw_day()])
    client = QWeatherClient(TEST_KEY, "https://" + TEST_HOST + "/")

    result = client.forecast("揭阳", "2026-10-24", "2026-10-24")

    assert result["data_source"] == "live_qweather"
    assert result["forecast"][0]["condition"] == "小雨"
    assert result["forecast"][0]["high_c"] == 28
    assert "和风天气" in result["source_note"]
    assert get.call_args_list[0].args[0] == f"https://{TEST_HOST}/geo/v2/city/lookup"
    assert get.call_args_list[1].args[0] == f"https://{TEST_HOST}/v7/weather/7d"
    for call in get.call_args_list:
        assert call.kwargs["headers"] == {"X-QW-Api-Key": TEST_KEY}
        assert "key" not in call.kwargs["params"]
        assert call.kwargs["allow_redirects"] is False
    assert TEST_KEY not in str(result)


def test_partial_forecast_keeps_exact_dates_and_missing_temperatures(monkeypatch):
    install_forecast(
        monkeypatch, [raw_day(), raw_day("2026-10-26", high="0", low="-5")]
    )

    result = QWeatherClient(TEST_KEY, TEST_HOST).forecast(
        "揭阳", "2026-10-24", "2026-10-26"
    )

    assert result["data_source"] == "partial_qweather"
    assert [day["date"] for day in result["forecast"]] == [
        "2026-10-24",
        "2026-10-25",
        "2026-10-26",
    ]
    missing = result["forecast"][1]
    assert missing["condition"] == "unknown"
    assert missing["high_c"] is None and missing["low_c"] is None
    assert result["forecast"][2]["high_c"] == 0


def test_outside_forecast_range_never_reuses_another_dates_weather(monkeypatch):
    install_forecast(monkeypatch, [raw_day()])

    result = QWeatherClient(TEST_KEY, TEST_HOST).forecast(
        "揭阳", "2026-12-01", "2026-12-02"
    )

    assert result["data_mode"] == "unavailable"
    assert all(day["high_c"] is None for day in result["forecast"])
    assert "2026-10-24" in result["message"]


@pytest.mark.parametrize(
    "status,code",
    [(401, "http_401"), (403, "http_403"), (429, "http_429"), (200, "api_204")],
)
def test_provider_errors_are_explained_without_simulated_weather(
    monkeypatch, status, code
):
    get = Mock(return_value=response({"code": "204", "error": TEST_KEY}, status))
    monkeypatch.setattr("src.mcp.qweather.requests.get", get)

    result = QWeatherClient(TEST_KEY, TEST_HOST).forecast(
        "揭阳", "2026-10-24", "2026-10-24"
    )

    assert result["error_code"] == code
    assert result["data_mode"] == "unavailable"
    assert result["forecast"][0]["high_c"] is None
    assert TEST_KEY not in str(result)


def test_network_errors_do_not_expose_request_details(monkeypatch):
    monkeypatch.setattr(
        "src.mcp.qweather.requests.get",
        Mock(side_effect=requests.ConnectionError(TEST_KEY)),
    )

    result = QWeatherClient(TEST_KEY, TEST_HOST).forecast(
        "揭阳", "2026-10-24", "2026-10-24"
    )

    assert result["error_code"] == "network_error"
    assert TEST_KEY not in str(result)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "https://example.com",
        "http://test.re.qweatherapi.com",
        "test.re.qweatherapi.com/path",
        "test.re.qweatherapi.com@other.example",
    ],
)
def test_missing_or_invalid_host_does_not_send_key(monkeypatch, host):
    get = Mock()
    monkeypatch.setattr("src.mcp.qweather.requests.get", get)

    result = QWeatherClient(TEST_KEY, host).forecast("揭阳", "2026-10-24", "2026-10-24")

    assert result["data_mode"] == "unavailable"
    get.assert_not_called()


def test_invalid_daily_record_is_unavailable_without_dropping_valid_dates(monkeypatch):
    install_forecast(
        monkeypatch,
        [
            raw_day(),
            raw_day("2026-10-25", high="wrong"),
            raw_day("2026-10-26", high="10", low="20"),
        ],
    )

    result = QWeatherClient(TEST_KEY, TEST_HOST).forecast(
        "揭阳", "2026-10-24", "2026-10-26"
    )

    assert result["data_mode"] == "partial"
    assert [day["high_c"] for day in result["forecast"]] == [28, None, None]


def test_backend_cache_avoids_duplicate_calls_and_cannot_be_mutated_by_consumer(
    monkeypatch,
):
    get = install_forecast(monkeypatch, [raw_day()])
    backend = TravelDataBackend(qweather_key=TEST_KEY, qweather_host=TEST_HOST)

    first = backend.get_weather_forecast("揭阳", "2026-10-24", "2026-10-24")
    first["forecast"][0]["high_c"] = 99
    second = backend.get_weather_forecast("揭阳", "2026-10-24", "2026-10-24")

    assert second["forecast"][0]["high_c"] == 28
    assert get.call_count == 2


def test_weather_agent_cannot_override_provider_temperatures_or_source(monkeypatch):
    install_forecast(monkeypatch, [raw_day()])
    planner = TravelPlannerService()
    planner.backend = TravelDataBackend(qweather_key=TEST_KEY, qweather_host=TEST_HOST)
    planner.weather_agent.research = Mock(
        return_value=(
            {
                "forecast": [
                    {
                        "date": "2026-10-24",
                        "condition": "晴",
                        "high_c": 99,
                        "low_c": 90,
                        "suggestion": "模型猜测",
                    }
                ],
                "overall_summary": "建议携带雨具",
                "risk_days": [],
                "data_source": "invented",
            },
            {},
        )
    )
    request = TravelRequest(
        city="揭阳",
        start_date="2026-10-24",
        end_date="2026-10-24",
        budget_min=500,
        budget_max=1500,
    )

    result = planner._build_weather_research_with_repair(request, {})

    assert result.forecast[0].high_c == 28
    assert result.forecast[0].condition == "小雨"
    assert planner._source_from_weather_research(result) == "live_qweather"


def test_unavailable_weather_stays_null_through_agent_and_validation(monkeypatch):
    planner = TravelPlannerService()
    planner.backend = TravelDataBackend()
    planner.weather_agent.research = Mock(
        return_value=(
            {"forecast": [], "overall_summary": "和风实时：晴天", "risk_days": []},
            {},
        )
    )
    request = TravelRequest(
        city="揭阳",
        start_date="2026-10-24",
        end_date="2026-10-24",
        budget_min=500,
        budget_max=1500,
    )

    result = planner._build_weather_research_with_repair(request, {})

    assert result.forecast[0].high_c is None
    assert "晴天" not in result.overall_summary
    assert planner._source_from_weather_research(result) == "unavailable"
    assert planner._validate_weather_research(result, request).ok


def test_known_weather_with_null_temperature_is_rejected():
    planner = TravelPlannerService()
    request = TravelRequest(
        city="揭阳",
        start_date="2026-10-24",
        end_date="2026-10-24",
        budget_min=500,
        budget_max=1500,
    )
    weather = WeatherResearch(
        forecast=[
            WeatherInfo(
                date=date(2026, 10, 24),
                condition="晴",
                high_c=None,
                low_c=None,
                suggestion="",
            )
        ]
    )

    assert not planner._validate_weather_research(weather, request).ok


def test_unknown_weather_with_invented_temperature_is_rejected():
    planner = TravelPlannerService()
    request = TravelRequest(
        city="揭阳",
        start_date="2026-10-24",
        end_date="2026-10-24",
        budget_min=500,
        budget_max=1500,
    )
    weather = WeatherResearch(
        forecast=[
            WeatherInfo(
                date=date(2026, 10, 24),
                condition="unknown",
                high_c=30,
                low_c=20,
                suggestion="",
            )
        ]
    )

    assert not planner._validate_weather_research(weather, request).ok

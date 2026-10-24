from datetime import date

import pytest

from src.services.opening_hours import check_opening_visit, parse_opening_hours

SEASONAL = "6月至9月 周一至周日 09:00-17:30；1月至5月,10月至12月 周一至周日 09:00-17:00"


@pytest.mark.parametrize(
    "month,end", [(1, 1020), (6, 1050), (9, 1050), (10, 1020), (12, 1020)]
)
def test_real_amap_seasonal_rule_uses_travel_month(month, end):
    hours = parse_opening_hours(SEASONAL, date(2026, month, 24))
    assert hours.status == "known"
    assert hours.intervals == [(540, end)]


def test_weekday_closure_and_midday_break_are_distinct():
    raw = "周二至周日 09:00-12:00,14:00-17:00（周一闭馆）"
    assert parse_opening_hours(raw, date(2026, 10, 26)).status == "closed"
    assert parse_opening_hours(raw, date(2026, 10, 24)).intervals == [
        (540, 720),
        (840, 1020),
    ]
    assert (
        check_opening_visit(raw, date(2026, 10, 24), "11:00-13:00").status == "conflict"
    )
    assert check_opening_visit(raw, date(2026, 10, 24), "14:00-17:00").status == "fits"


def test_weekday_lists_and_24h_are_supported():
    assert parse_opening_hours(
        "周一、周三 10:00-16:00", date(2026, 10, 26)
    ).intervals == [(600, 960)]
    assert (
        parse_opening_hours("周一,周三 10:00-16:00", date(2026, 10, 26)).status
        == "known"
    )
    for raw in ("24小时营业", "周一至周日 00:00-24:00"):
        assert parse_opening_hours(raw, date(2026, 10, 24)).intervals == [(0, 1440)]
        assert (
            check_opening_visit(raw, date(2026, 10, 24), "21:00-22:00").status == "fits"
        )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "9点到17点",
        "周一至周日09:00-17:00，节假日除外",
        "09:00-17:00，需提前预约",
        "09:00-17:00（16:00停止入馆）",
        "18:00-02:00",
        "18:00-次日02:00",
        "25:00-26:00",
        "09:60-17:00",
        "09:00-24:30",
        "6月至9月09:00-17:00",
        "10月1日-10月7日09:00-17:00",
        "周一闭馆；周一09:00-17:00",
        "工作日09:00-17:00",
        "周一至周日09:00-17:00；周一10:00-16:00",
    ],
)
def test_complex_missing_invalid_and_uncovered_rules_stay_unknown(raw):
    result = check_opening_visit(raw, date(2026, 10, 26), "09:00-11:00")
    assert result.status == "unknown"
    assert result.raw_text == raw


def test_malformed_visit_is_never_reported_as_fitting():
    assert (
        check_opening_visit("24小时营业", date(2026, 10, 24), "25:00-26:00").status
        == "unknown"
    )

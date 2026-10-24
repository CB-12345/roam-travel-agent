"""Conservative interpretation of regular Amap hours, without model inference."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from src.models_opening_hours import DayOpeningHours, OpeningVisitCheck

_MONTH = r"(?:1[0-2]|[1-9])月"
_MONTH_PART = rf"{_MONTH}(?:[至~-]{_MONTH})?"
_MONTHS = re.compile(rf"^({_MONTH_PART}(?:[,、]{_MONTH_PART})*)")
_DAY = r"周[一二三四五六日]"
_WEEK_PART = rf"{_DAY}(?:[至~-](?:周)?[一二三四五六日])?"
_WEEK = re.compile(rf"^({_WEEK_PART}(?:[,、]{_WEEK_PART})*)")
_CLOSED = r"(?:闭馆|闭园|休馆|休园|不开放|不营业|休息|歇业|关闭)"
_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*[-~至]\s*(\d{1,2}):(\d{2})")
_EXCEPTION = re.compile(
    r"节假日|节日|法定|除夕|春节|国庆|中秋|端午|清明|劳动节|元旦|预约|临时|另行|"
    r"暂停|调整|停止|最晚|最后|入园|入馆|入场|售票|旺季|淡季|夏令|冬令|试营业|视情况"
)


def _span(start: int, end: int, cycle: int) -> set[int]:
    return (
        set(range(start, end + 1))
        if end >= start
        else set(range(start, cycle + 1)) | set(range(1, end + 1))
    )


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02}:{minutes % 60:02}"


def parse_opening_hours(raw: str, visit_date: date) -> DayOpeningHours:
    unknown = DayOpeningHours()
    if not raw.strip():
        return unknown
    text = unicodedata.normalize("NFKC", raw).strip()
    if _EXCEPTION.search(text):
        return unknown.model_copy(
            update={"reason": "包含节假日、预约或特殊开放规则，请向场所确认。"}
        )
    text = re.sub(r"星期|礼拜", "周", text)
    text = re.sub(r"周天", "周日", text).replace("周末", "周六至周日")
    text = re.sub(r"[—–～到]", "至", text)
    text = re.sub(r"\s+", "", text).replace("；", ";").replace("，", ",")
    # A common explicit weekday closure may follow the regular rule in brackets.
    text = re.sub(rf"\(({_WEEK_PART}{_CLOSED})\)", r";\1", text)
    text = re.sub(rf"(\d{{1,2}}:\d{{2}}|{_CLOSED}),(?=周)", r"\1;", text)
    selected: list[tuple[int, int]] = []
    matched, closed = False, False
    clauses = [part for part in text.split(";") if part]
    if not clauses:
        return unknown
    for clause in clauses:
        months, weekdays = set(range(1, 13)), set(range(1, 8))
        clause = re.sub(r"^(?:营业时间|开放时间|开放时段|营业时段)[:：]?", "", clause)
        clause = re.sub(r"^全年", "", clause)
        month = _MONTHS.match(clause)
        if month:
            months = set()
            for part in re.split(r"[,、]", month[0]):
                numbers = [int(n) for n in re.findall(r"\d+", part)]
                months |= _span(numbers[0], numbers[-1], 12)
            clause = clause[month.end() :]
        week = _WEEK.match(clause)
        if week:
            weekdays = set()
            for part in re.split(r"[,、]", week[0]):
                numbers = [
                    "一二三四五六日".index(n) + 1
                    for n in re.findall(r"[一二三四五六日]", part)
                ]
                weekdays |= _span(numbers[0], numbers[-1], 7)
            clause = clause[week.end() :]
        clause = re.sub(r"^(?:每天|每日)[:：]?", "", clause).strip(":")
        applies = visit_date.month in months and visit_date.isoweekday() in weekdays
        if re.fullmatch(_CLOSED, clause):
            if applies:
                matched, closed = True, True
            continue
        if re.fullmatch(r"24小时(?:营业|开放)?|全天(?:营业|开放)?", clause):
            intervals = [(0, 1440)]
        else:
            intervals = []
            residue = _RANGE.sub("", clause)
            residue = re.sub(r"营业|开放|[,、/]", "", residue)
            if residue:
                return unknown.model_copy(
                    update={"reason": "开放规则较复杂，请查看原始时间并向场所确认。"}
                )
            for interval in _RANGE.finditer(clause):
                h1, m1, h2, m2 = map(int, interval.groups())
                start, end = h1 * 60 + m1, h2 * 60 + m2
                if (
                    h1 > 23
                    or h2 > 24
                    or m1 > 59
                    or m2 > 59
                    or (h2 == 24 and m2)
                    or end <= start
                ):
                    return unknown.model_copy(
                        update={"reason": "包含跨夜或无效时段，请向场所确认。"}
                    )
                intervals.append((start, end))
            if not intervals:
                return unknown
        if applies:
            if (
                selected
                and intervals != selected
                and any(
                    max(a, c) < min(b, d) for a, b in selected for c, d in intervals
                )
            ):
                return unknown.model_copy(
                    update={"reason": "同日存在重叠的不同规则，请向场所确认适用时间。"}
                )
            matched = True
            selected.extend(intervals)
    if not matched or (closed and selected):
        return unknown.model_copy(
            update={"reason": "规则未覆盖出行日期或存在冲突，请向场所确认。"}
        )
    if closed:
        return DayOpeningHours(
            status="closed",
            summary="当日不开放",
            reason="地图规则明确标注当日闭馆或停业。",
        )
    merged: list[tuple[int, int]] = []
    for start, end in sorted(selected):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return DayOpeningHours(
        status="known",
        intervals=merged,
        summary=(
            "24 小时开放"
            if merged == [(0, 1440)]
            else " / ".join(f"{_clock(a)}–{_clock(b)}" for a, b in merged)
        ),
        reason="按地图常规开放规则核对；临时调整及预约请另行确认。",
    )


def check_opening_visit(
    raw: str,
    visit_date: date,
    time_range: str,
    *,
    source: str = "",
    checked_at: str = "",
) -> OpeningVisitCheck:
    hours = parse_opening_hours(raw, visit_date)
    status = hours.status
    message = hours.reason
    if status == "known":
        match = _RANGE.fullmatch(time_range.strip())
        values = tuple(map(int, match.groups())) if match else ()
        valid = (
            values
            and values[0] < 24
            and values[2] < 24
            and values[1] < 60
            and values[3] < 60
        )
        start, end = (
            (values[0] * 60 + values[1], values[2] * 60 + values[3])
            if valid
            else (0, 0)
        )
        if end <= start:
            status, message = "unknown", "行程时间格式不完整，请核实停留时间。"
        elif any(start >= a and end <= b for a, b in hours.intervals):
            status, message = "fits", "安排在开放时段内"
        else:
            status, message = "conflict", "安排超出开放时段，建议调整时间或更换地点。"
    return OpeningVisitCheck(
        date=visit_date,
        time_range=time_range,
        status=status,
        hours_text=hours.summary if hours.status != "unknown" else "",
        message=message,
        raw_text=raw,
        source=source,
        checked_at=checked_at,
    )

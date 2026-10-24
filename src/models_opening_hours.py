from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class PlaceVisit(BaseModel):
    date: date
    time_range: str = Field(max_length=40)


class DayOpeningHours(BaseModel):
    status: Literal["known", "closed", "unknown"] = "unknown"
    intervals: list[tuple[int, int]] = Field(default_factory=list)
    summary: str = "开放时间待确认"
    reason: str = "未获取到可靠的开放时间。"


class OpeningVisitCheck(PlaceVisit):
    status: Literal["fits", "conflict", "closed", "unknown"]
    hours_text: str = ""
    message: str
    raw_text: str = ""
    source: str = ""
    checked_at: str = ""

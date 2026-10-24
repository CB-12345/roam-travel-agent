from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.models import (
    HotelStyle,
    Pace,
    TransitPreference,
    TransportMode,
    TravelRequest,
    TripPlan,
)


class RevisionMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1200)


class DayRevisionRule(BaseModel):
    instruction: str = Field("", max_length=600)
    indoor_only: bool = False
    max_attractions: int | None = Field(None, ge=1, le=6)


class TravelRevisionRequest(BaseModel):
    request: TravelRequest
    plan: TripPlan
    message: str = Field(min_length=1, max_length=600)
    history: list[RevisionMessage] = Field(default_factory=list, max_length=16)
    day_rules: dict[str, DayRevisionRule] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请输入想调整的内容")
        return value.strip()

    @model_validator(mode="after")
    def check_plan_context(self) -> "TravelRevisionRequest":
        expected = [
            self.request.start_date + timedelta(days=i)
            for i in range(self.request.trip_days)
        ]
        if self.plan.city != self.request.city or self.plan.trip_days != len(expected):
            raise ValueError("行程与原始旅行需求不一致，请重新打开对应行程")
        if [day.date for day in self.plan.daily_plans] != expected:
            raise ValueError("行程日期与旅行需求不一致")
        if len(self.plan.daily_stays) != len(expected):
            raise ValueError("这份行程缺少每日住宿信息，请先重新生成")
        if any(
            key not in {str(i + 1) for i in range(len(expected))}
            for key in self.day_rules
        ):
            raise ValueError("修改规则包含无效的旅行日期")
        return self


class RevisionUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budget_min: int | None = Field(None, ge=100)
    budget_max: int | None = Field(None, ge=100)
    pace: Pace | None = None
    hotel_style: HotelStyle | None = None
    transport_mode: TransportMode | None = None
    transit_preference: TransitPreference | None = None
    extra_preferences: str | None = Field(None, max_length=300)
    taboos: str | None = Field(None, max_length=300)


class RevisionIntent(BaseModel):
    action: Literal["revise", "clarify", "answer"]
    message: str = Field(min_length=1, max_length=800)
    target_days: list[int] = Field(default_factory=list, max_length=7)
    request_updates: RevisionUpdates = Field(default_factory=RevisionUpdates)
    indoor_only: bool | None = None
    max_attractions_per_day: int | None = Field(None, ge=1, le=6)
    reset_day_rules: bool = False
    change_hotel: bool = False
    attraction_queries: list[str] = Field(default_factory=list, max_length=3)


class DayAttractionAssignment(BaseModel):
    day_index: int = Field(ge=1, le=7)
    attraction_names: list[str] = Field(min_length=1, max_length=6)


class RevisionSelection(BaseModel):
    assignments: list[DayAttractionAssignment] = Field(min_length=1, max_length=7)


class RevisionChange(BaseModel):
    section: str
    before: str
    after: str


class TravelRevisionResponse(BaseModel):
    status: Literal["revised", "clarification", "answered"]
    message: str
    request: TravelRequest
    plan: TripPlan
    day_rules: dict[str, DayRevisionRule] = Field(default_factory=dict)
    changes: list[RevisionChange] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

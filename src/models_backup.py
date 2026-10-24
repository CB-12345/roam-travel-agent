from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.models import TravelRequest, TripPlan
from src.models_revision import DayRevisionRule, RevisionMessage, TravelRevisionRequest
from src.services.places import PlaceDetails


class TripBackup(BaseModel):
    version: Literal[1]
    request: TravelRequest
    plan: TripPlan
    day_rules: dict[str, DayRevisionRule] = Field(default_factory=dict, max_length=7)
    conversation: list[RevisionMessage] = Field(default_factory=list, max_length=24)
    place_details: dict[str, PlaceDetails] = Field(default_factory=dict, max_length=150)
    mode: Literal["personal", "recorded_demo"] = "personal"
    recorded_at: str = ""

    def validate_context(self):
        TravelRevisionRequest(
            request=self.request,
            plan=self.plan,
            day_rules=self.day_rules,
            history=self.conversation[-16:],
            message="导入校验",
        )

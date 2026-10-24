from __future__ import annotations

from pydantic import BaseModel, Field

from src.models import Location, PlaceTarget
from src.models_revision import TravelRevisionRequest


class PlaceCandidateRequest(TravelRevisionRequest):
    message: str = "更换一个地点"
    target: PlaceTarget


class PlaceReplacementRequest(PlaceCandidateRequest):
    candidate_token: str = Field(min_length=20, max_length=100)


class PlaceCandidate(BaseModel):
    token: str
    name: str
    kind: str
    location: Location
    estimated_cost: float
    cost_delta: float
    recommended_hours: float | None = None
    nightly_price_min: float | None = None
    nightly_price_max: float | None = None
    reasons: list[str] = Field(default_factory=list)
    affected_days: list[int]
    map_url: str


class PlaceCandidatesResponse(BaseModel):
    target: PlaceTarget
    candidates: list[PlaceCandidate]
    affected_days: list[int]
    message: str
    expires_in_seconds: int = 600

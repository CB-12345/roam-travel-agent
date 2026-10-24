from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, Field

from src.mcp.travel_backend import TravelDataBackend
from src.models_opening_hours import OpeningVisitCheck, PlaceVisit
from src.services.opening_hours import check_opening_visit


class PlaceReference(BaseModel):
    key: str = Field(min_length=1, max_length=240)
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(default="", max_length=400)
    poi_id: str = Field(default="", max_length=30)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    visits: list[PlaceVisit] = Field(default_factory=list, max_length=70)


class PlaceLookupRequest(BaseModel):
    city: str = Field(min_length=1, max_length=40)
    places: list[PlaceReference] = Field(min_length=1, max_length=40)


class PlaceDetails(BaseModel):
    key: str
    name: str
    address: str = ""
    district: str = ""
    poi_id: str = ""
    lat: float | None = None
    lng: float | None = None
    verified: bool = False
    photo_url: str = ""
    photo_source: str = ""
    map_url: str
    opening_hours: str = ""
    opening_hours_source: str = ""
    opening_hours_checked_at: str = ""
    visit_checks: list[OpeningVisitCheck] = Field(default_factory=list)


class PlaceLookupResponse(BaseModel):
    places: list[PlaceDetails]
    configured: bool


def amap_url(
    city: str, name: str, lat: float | None = None, lng: float | None = None
) -> str:
    if lat is not None and lng is not None:
        params = {
            "position": f"{lng:.6f},{lat:.6f}",
            "name": name,
            "coordinate": "gaode",
            "callnative": "1",
            "src": "roam",
        }
        return "https://uri.amap.com/marker?" + urlencode(params)
    return "https://uri.amap.com/search?" + urlencode(
        {"keyword": f"{city} {name}", "city": city, "view": "map", "src": "roam"}
    )


class PlaceService:
    """Resolve an existing plan's places without asking a model to invent metadata."""

    def __init__(self, backend: TravelDataBackend):
        self.backend = backend
        self._cache: dict[tuple, tuple[float, PlaceDetails]] = {}
        self._lock = threading.Lock()

    def resolve(self, request: PlaceLookupRequest) -> PlaceLookupResponse:
        places = []
        # Share the backend's request pacing within a batch and across UI requests.
        with self._lock:
            for ref in request.places:
                cache_key = (
                    request.city,
                    ref.name,
                    ref.address,
                    ref.poi_id,
                    ref.lat,
                    ref.lng,
                )
                cached = self._cache.get(cache_key)
                if cached and time.monotonic() < cached[0]:
                    result = cached[1].model_copy(deep=True, update={"key": ref.key})
                else:
                    result = self._resolve_one(request.city, ref)
                    if len(self._cache) >= 512:
                        self._cache.clear()
                    ttl = 3600 if result.verified else 300
                    # Cache only POI facts, never a verdict for another visit/date.
                    self._cache[cache_key] = (
                        time.monotonic() + ttl,
                        result.model_copy(deep=True),
                    )
                result.visit_checks = [
                    check_opening_visit(
                        result.opening_hours,
                        visit.date,
                        visit.time_range,
                        source=result.opening_hours_source,
                        checked_at=result.opening_hours_checked_at,
                    )
                    for visit in ref.visits
                ]
                places.append(result)
        return PlaceLookupResponse(
            places=places, configured=bool(self.backend.amap_key)
        )

    def _resolve_one(self, city: str, ref: PlaceReference) -> PlaceDetails:
        fallback = PlaceDetails(
            key=ref.key,
            name=ref.name,
            address=ref.address,
            map_url=amap_url(city, ref.name),
        )
        if not self.backend.amap_key:
            return fallback
        payload = None
        if re.fullmatch(r"B[A-Z0-9]{8,15}", ref.poi_id):
            payload = self.backend._amap_get(
                "https://restapi.amap.com/v3/place/detail",
                {"id": ref.poi_id, "extensions": "all"},
                timeout=6,
                retries=0,
                use_cache=False,
            )
        poi = self._match(ref, payload, by_id=True) if payload else None
        if poi is None:
            payload = self.backend._amap_get(
                "https://restapi.amap.com/v3/place/text",
                {
                    "keywords": ref.name,
                    "city": city,
                    "citylimit": "true",
                    "offset": 10,
                    "page": 1,
                    "extensions": "all",
                },
                timeout=6,
                retries=0,
                use_cache=False,
            )
            poi = self._match(ref, payload)
        if poi is None:
            return fallback
        coordinates = self._coordinates(poi)
        if coordinates is None:
            return fallback
        lng, lat = coordinates
        photos = poi.get("photos") or []
        photo = next(
            (
                self._photo_url(item.get("url"))
                for item in photos
                if isinstance(item, dict) and self._photo_url(item.get("url"))
            ),
            "",
        )
        biz = poi.get("biz_ext")
        biz = biz if isinstance(biz, dict) else {}
        # v3 opentime2 describes regular hours; do not use a today-only v5 field.
        hours = next(
            (
                value.strip()
                for value in (biz.get("opentime2"), biz.get("open_time"))
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        if len(hours) > 1500:
            hours = hours[:1470] + "（规则过长，请确认）"
        return PlaceDetails(
            key=ref.key,
            name=self.backend._safe_text(poi.get("name"), ref.name),
            address=self._full_address(poi),
            district=self.backend._safe_text(poi.get("adname")),
            poi_id=self.backend._safe_text(poi.get("id")),
            lat=lat,
            lng=lng,
            verified=True,
            photo_url=photo,
            photo_source="高德地点照片" if photo else "",
            map_url=amap_url(city, ref.name, lat, lng),
            opening_hours=hours,
            opening_hours_source="高德地点开放时间" if hours else "",
            opening_hours_checked_at=datetime.now(timezone.utc).isoformat(),
        )

    def _match(
        self,
        ref: PlaceReference,
        payload: dict[str, Any] | None,
        *,
        by_id: bool = False,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict) or str(payload.get("status")) != "1":
            return None
        raw_pois = payload.get("pois")
        if not isinstance(raw_pois, list):
            return None
        candidates = [
            poi
            for poi in raw_pois
            if isinstance(poi, dict)
            and self._canonical(poi.get("name")) == self._canonical(ref.name)
            and self._coordinates(poi) is not None
        ]
        if by_id:
            return next(
                (poi for poi in candidates if poi.get("id") == ref.poi_id), None
            )
        if ref.lat is not None and ref.lng is not None and (ref.lat or ref.lng):
            distances = [
                (
                    self._distance(ref.lat, ref.lng, *reversed(self._coordinates(poi))),
                    poi,
                )
                for poi in candidates
            ]
            distances.sort(key=lambda pair: pair[0])
            # A different branch or a city-centre estimate must not become an exact pin.
            return distances[0][1] if distances and distances[0][0] <= 0.5 else None
        if ref.address:
            address = self._canonical(ref.address)
            matches = [
                poi
                for poi in candidates
                if (
                    self._canonical(poi.get("address"))
                    and (
                        self._canonical(poi.get("address")) in address
                        or address in self._canonical(self._full_address(poi))
                    )
                )
            ]
            if len(matches) == 1:
                return matches[0]
            # A unique search result still cannot override a conflicting street address.
            return None
        return candidates[0] if len(candidates) == 1 else None

    def _full_address(self, poi: dict[str, Any]) -> str:
        detail = self.backend._safe_text(poi.get("address"))
        regions = []
        for field in ("pname", "cityname", "adname"):
            region = self.backend._safe_text(poi.get(field))
            if region and region not in regions:
                regions.append(region)
        remaining = detail
        for region in regions:
            if remaining.startswith(region):
                remaining = remaining[len(region) :]
        return "".join(regions) + remaining

    @staticmethod
    def _canonical(value: Any) -> str:
        return re.sub(
            r"[\s()（）,，·]", "", value if isinstance(value, str) else ""
        ).casefold()

    @staticmethod
    def _coordinates(poi: dict[str, Any]) -> tuple[float, float] | None:
        try:
            lng, lat = map(float, str(poi.get("location", "")).split(","))
            if (
                math.isfinite(lng)
                and math.isfinite(lat)
                and -180 <= lng <= 180
                and -90 <= lat <= 90
                and (lng or lat)
            ):
                return lng, lat
        except (ValueError, TypeError):
            pass
        return None

    @staticmethod
    def _photo_url(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme in {"https", "http"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            ):
                return value
        except ValueError:
            pass
        return ""

    @staticmethod
    def _distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        lat1, lng1, lat2, lng2 = map(math.radians, (lat1, lng1, lat2, lng2))
        a = (
            math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
        )
        return 6371 * 2 * math.asin(min(1, math.sqrt(a)))

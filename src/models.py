from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from src.models_opening_hours import OpeningVisitCheck

# 旅行偏好标签，和前端preferenceOptions数组的第一个值完全对应
PreferenceTag = Literal[
    "humanity", "art", "nature", "food", "shopping", "nightlife", "family"
]
# 旅行节奏，对应前端pace下拉框
Pace = Literal["relaxed", "balanced", "intense"]
# 酒店风格，对应前端hotelStyle下拉框
HotelStyle = Literal["budget", "comfort", "premium"]
# 出行方式，对应前端transportMode下拉框（比前端多了walk，用于混合出行）
TransportMode = Literal["public_transit", "self_drive", "taxi", "mixed", "walk"]
# 公交偏好，对应前端transitPreference下拉框
TransitPreference = Literal["recommended", "less_walking", "subway_priority", "bus_priority"]
# 餐饮类型：只区分早/中/晚餐，不引入 snack。
MealType = Literal["breakfast", "lunch", "dinner"]


class TravelRequest(BaseModel):
    # 旅行城市：必填，长度1-40个字符
    city: str = Field(..., min_length=1, max_length=40)

    # 开始/结束日期：Pydantic会自动将前端传的"2026-06-10"字符串转成date对象
    start_date: date
    end_date: date

    # 最低/最高预算：必填，最小值100元，防止不合理的预算
    budget_min: int = Field(..., ge=100)
    budget_max: int = Field(..., ge=100)

    # 用户选中的偏好标签：对应前端selectedPreferences集合，默认空列表
    preferences: list[PreferenceTag] = Field(default_factory=list)
    # 额外偏好/忌讳：可选，最大长度300字符，防止用户输入过长
    extra_preferences: str = Field("", max_length=300)
    taboos: str = Field("", max_length=300)

    # 出行人数：默认1人，范围1-10人
    travelers: int = Field(1, ge=1, le=10)

    # 旅行节奏：默认balanced
    pace: Pace = "balanced"

    # 酒店风格：默认comfort（舒适型）
    hotel_style: HotelStyle = "comfort"

    # 出行方式：默认public_transit（公共交通）
    transport_mode: TransportMode = "public_transit"

    # 公交偏好：默认recommended（高德推荐）
    transit_preference: TransitPreference = "recommended"

    # 出发城市：预留字段，当前未使用
    departure_city: str = Field("", max_length=40)

    # mode="after"：在所有单个字段验证通过后再执行
    @model_validator(mode="after")
    def validate_dates_and_budget(self) -> "TravelRequest":
        if self.end_date < self.start_date:
            # 验证结束日期 >= 开始日期
            raise ValueError("结束日期必须大于或等于开始日期")
        if (self.end_date - self.start_date).days >= 7:
            raise ValueError("本工具支持 1-7 天旅行，请缩短行程日期")
        if self.budget_max < self.budget_min:
            # 验证最高预算 >= 最低预算
            raise ValueError("大预算值必须大于或等于最小预算值")
        return self

    # 旅行总天数：例如6月10日到6月12日是3天
    @computed_field
    @property
    def trip_days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    # 住宿晚数：3天旅行住2晚，1天旅行住1晚
    @computed_field
    @property
    def stay_nights(self) -> int:
        return max(self.trip_days - 1, 1)


class Location(BaseModel):
    name: str
    address: str = ""
    lat: float = 0.0
    lng: float = 0.0

    # 字段级验证器：统一格式化所有文本字段
    @field_validator("name", "address", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple, set)):
            parts = [str(item).strip() for item in value if str(item).strip()]
            return " ".join(parts)
        return str(value).strip()


class Attraction(BaseModel):
    # 候选池ID：用于MCP验真，标记这个景点来自哪个POI候选
    candidate_id: str = ""
    # 搜索这个景点的原始查询词
    source_query: str = ""
    # 匹配分数：和用户偏好的匹配度，用于排序
    score: float = 0.0
    # 匹配的用户偏好标签
    matched_preferences: list[str] = Field(default_factory=list)
    # 忌讳检查结果：标记是否包含用户忌讳的内容
    taboo_check: str = ""
    # 景点基本信息
    name: str
    category: str  # 类别：如"历史古迹"、"自然风光"
    tags: list[str] = Field(default_factory=list)  # 标签：如"免费"、"亲子友好"
    summary: str
    recommended_hours: float = 2.0
    ticket_price: float = 0.0
    best_time: str = "daytime"  # 最佳游玩时间：daytime/night
    location: Location


class AttractionSelection(BaseModel):
    candidate_id: str = ""
    name: str
    score: float = 0.0
    matched_preferences: list[str] = Field(default_factory=list)
    taboo_check: str = ""
    reason: str = ""

    @field_validator("candidate_id", "name", "taboo_check", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple, set)):
            parts = [str(item).strip() for item in value if str(item).strip()]
            return " ".join(parts)
        return str(value).strip()


class WeatherInfo(BaseModel):
    date: date
    condition: str
    high_c: int | None
    low_c: int | None
    suggestion: str
    data_source: str = ""


class HotelOption(BaseModel):
    name: str
    style: str
    star_level: int = 3
    nightly_price: float
    nightly_price_min: float | None = None
    nightly_price_max: float | None = None
    price_source: str = "estimated"
    booking_url: str = ""
    summary: str
    nearby_area: str
    location: Location

    # 用于清洗字符串字段，避免高德返回奇怪类型。
    @field_validator("name", "style", "price_source", "booking_url", "summary", "nearby_area", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple, set)):
            parts = [str(item).strip() for item in value if str(item).strip()]
            return " ".join(parts)
        return str(value).strip()


class HotelSelectionResearch(BaseModel):
    candidate_names: list[str] = Field(default_factory=list)
    recommended_hotel_name: str
    selection_reasoning: list[str] = Field(default_factory=list)

    @field_validator("recommended_hotel_name", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class RestaurantOption(BaseModel):
    name: str
    cuisine: str = ""
    avg_cost_per_person: float = 0.0
    price_source: str = "estimated"
    address_source: str = "poi"
    summary: str = ""
    nearby_anchor: str = ""
    location: Location

    @field_validator("name", "cuisine", "price_source", "address_source", "summary", "nearby_anchor", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple, set)):
            parts = [str(item).strip() for item in value if str(item).strip()]
            return " ".join(parts)
        return str(value).strip()


class MealIntent(BaseModel):
    meal_type: MealType
    time_range: str
    anchor_name: str
    anchor_type: str = ""
    cuisine_intent: str
    budget_total: float = 0.0
    must_avoid: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("time_range", "anchor_name", "anchor_type", "cuisine_intent", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (list, tuple, set)):
            parts = [str(item).strip() for item in value if str(item).strip()]
            return " ".join(parts)
        return str(value).strip()


class DailyStayPlan(BaseModel):
    day_index: int
    date: date
    start_hotel: HotelOption | None = None
    end_hotel: HotelOption | None = None
    night_area: str = ""
    charged_night: bool = True
    hotel_changed: bool = False
    reason: str = ""


class BudgetBreakdown(BaseModel):
    hotel: float
    attractions: float
    food: float
    transport: float
    contingency: float
    total: float


class RouteDetailSegment(BaseModel):
    segment_type: str
    instruction: str = ""
    duration_min: int = 0
    distance_m: int = 0
    line_name: str = ""
    direction: str = ""
    on_station: str = ""
    off_station: str = ""
    via_count: int = 0
    via_stops: list[str] = Field(default_factory=list)
    entrance: str = ""
    exit: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class DayPlanItem(BaseModel):
    time_range: str
    title: str
    item_type: str
    meal_type: MealType | str = ""
    location_name: str
    location_address: str = ""
    location: Location | None = None
    opening_check: OpeningVisitCheck | None = None
    summary: str
    estimated_cost: float = 0.0
    reason: str = ""
    is_route_stop: bool = True
    transport_mode: str = ""
    from_location: str = ""
    to_location: str = ""
    duration_min: int = 0
    distance_km: float = 0.0
    transfers: int = 0
    route_segments: list[RouteDetailSegment] = Field(default_factory=list)
    route_strategy: str = ""
    route_data_mode: str = ""
    route_fallback_reason: str = ""
    route_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    expandable: bool = False


class RoutePlan(BaseModel):
    city: str
    mode: TransportMode
    origin: str
    destination: str
    origin_address: str = ""
    destination_address: str = ""
    distance_km: float
    duration_min: int
    estimated_cost: float = 0.0
    transfers: int = 0
    walk_distance_m: int = 0
    tolls: float = 0.0
    data_mode: str = "fallback"
    fallback_reason: str = ""
    summary: str = ""
    strategy_label: str = ""
    traffic_status: str = ""
    route_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    route_segments: list[RouteDetailSegment] = Field(default_factory=list)


class DayPlan(BaseModel):
    date: date
    weather: WeatherInfo
    route_summary: str
    meal_intents: list[MealIntent] = Field(default_factory=list)
    items: list[DayPlanItem] = Field(default_factory=list)
    total_transport_cost: float = 0.0
    total_transport_time_min: int = 0


class AttractionResearch(BaseModel):
    city_overview: str
    selected_attractions: list[Attraction] = Field(default_factory=list)
    selection_reasoning: list[str] = Field(default_factory=list)
    recommended_night_area: str = ""
    search_plan: list[dict[str, Any]] = Field(default_factory=list)
    preference_interpretation: dict[str, Any] = Field(default_factory=dict)
    agent_diagnostics: dict[str, Any] = Field(default_factory=dict)


class AttractionSelectionResearch(BaseModel):
    city_overview: str = ""
    selected_attractions: list[AttractionSelection] = Field(default_factory=list)
    selection_reasoning: list[str] = Field(default_factory=list)
    recommended_night_area: str = ""
    search_plan: list[dict[str, Any]] = Field(default_factory=list)
    preference_interpretation: dict[str, Any] = Field(default_factory=dict)
    agent_diagnostics: dict[str, Any] = Field(default_factory=dict)


class WeatherResearch(BaseModel):
    forecast: list[WeatherInfo] = Field(default_factory=list)
    overall_summary: str = ""
    risk_days: list[str] = Field(default_factory=list)
    data_source: str = ""
    source_note: str = ""


class HotelResearch(BaseModel):
    candidates: list[HotelOption] = Field(default_factory=list)
    recommended_hotel: HotelOption | None = None
    selection_reasoning: list[str] = Field(default_factory=list)


class PlaceTarget(BaseModel):
    day_index: int = Field(ge=1, le=7)
    kind: Literal["attraction", "meal", "hotel"]
    name: str = Field(min_length=1, max_length=120)
    address: str = Field("", max_length=400)
    meal_type: Literal["", "breakfast", "lunch", "dinner"] = ""


class TripPlan(BaseModel):
    city: str
    travel_theme: str
    overview: str
    trip_days: int
    planning_source: str = "program_fallback"
    attraction_data_source: str = "fallback"
    weather_data_source: str = "fallback"
    hotel_data_source: str = "fallback"
    attraction_search_plan: list[dict[str, Any]] = Field(default_factory=list)
    preference_interpretation: dict[str, Any] = Field(default_factory=dict)
    agent_diagnostics: dict[str, Any] = Field(default_factory=dict)
    quality_report: dict[str, Any] = Field(default_factory=dict)
    selected_attractions: list[Attraction] = Field(default_factory=list)
    recommended_hotel: HotelOption | None = None
    hotel_candidates: list[HotelOption] = Field(default_factory=list)
    daily_stays: list[DailyStayPlan] = Field(default_factory=list)
    daily_plans: list[DayPlan] = Field(default_factory=list)
    place_locks: list[PlaceTarget] = Field(default_factory=list, max_length=100)
    budget: BudgetBreakdown
    packing_tips: list[str] = Field(default_factory=list)
    risk_alerts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    llm_enabled: bool
    provider: str
    llm_load_error: str
    available_tools: list[str] = Field(default_factory=list)

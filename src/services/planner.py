from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import date, timedelta
from dataclasses import dataclass
import json
import math
import time
from typing import Any, Callable, Iterable

from pydantic import BaseModel, ValidationError

from src.agents import AttractionSearchAgent, HotelSearchAgent, ItineraryPlanningAgent, WeatherSearchAgent
from src.config import get_settings
from src.llm import LangChainRuntime
from src.mcp.travel_backend import PREFERENCE_LABELS, TravelDataBackend
from src.mcp.travel_tool import TravelBackendTool
from src.mcp.tooling import ToolRegistry, ToolResponse
from src.models import (
    Attraction,
    AttractionResearch,
    AttractionSelectionResearch,
    BudgetBreakdown,
    DailyStayPlan,
    DayPlan,
    DayPlanItem,
    HealthResponse,
    HotelOption,
    HotelResearch,
    HotelSelectionResearch,
    MealIntent,
    RestaurantOption,
    RouteDetailSegment,
    RoutePlan,
    TravelRequest,
    TripPlan,
    WeatherInfo,
    WeatherResearch,
)
from src.utils.json_utils import extract_json_object
from src.services.planning_quality import (
    MEAL_WINDOWS,
    MIN_MEAL_DURATIONS,
    distance_km,
    schedule_day,
    time_bounds,
    valid_location,
)
from src.services.opening_hours import check_opening_visit, parse_opening_hours
from src.services.places import (
    PlaceDetails,
    PlaceLookupRequest,
    PlaceReference,
    PlaceService,
)


# 生成初始化函数，不允许更改
@dataclass(frozen=True)
class RouteTarget:
    name: str
    address: str = ""
    lat: float = 0.0
    lng: float = 0.0


@dataclass
class AgentValidationResult:
    ok: bool
    value: Any | None = None
    errors: list[str] | None = None
    current_count: int = 0

    @property
    def error_list(self) -> list[str]:
        return self.errors or []


class QuotaToolWrapper:
    '''包装一个工具，限制它一轮最多调用多少次'''
    def __init__(
        self,
        tool: Any,
        *,  # 关键字传参，不能随便按位置传。
        max_calls: int,
        duplicate_key_params: tuple[str, ...],  # 用哪些参数判断是否重复
    ) -> None:
        self._tool = tool
        self._base_description = tool.description
        self.name = tool.name
        self._max_calls = max_calls
        self._base_max_calls = max_calls
        self._duplicate_key_params = duplicate_key_params
        self._calls = 0  # 记录本轮已经调用了多少次
        self._seen_keys: set[tuple[str, ...]] = set()  # 已经查过的key
        self._candidate_pool: list[dict[str, Any]] = []  # 候选池
        self._refresh_description()

    def _refresh_description(self) -> None:
        self.description = (
            f"{self._base_description} 调用限制：本轮最多 {self._max_calls} 次；重复查询会返回缓存/拒绝提示。"
        )

    def reset_quota(self) -> None:
        '''重置函数：每次新生成一份旅游计划前，把上一轮状态情掉'''
        self._max_calls = self._base_max_calls
        self._refresh_description()
        self._calls = 0
        self._seen_keys.clear()
        self._candidate_pool.clear()

    def extend_quota(self, extra_calls: int) -> None:
        '''在不清空候选池的前提下，给返工补搜追加少量调用额度。'''
        if extra_calls <= 0:
            return
        self._max_calls += extra_calls
        self._refresh_description()

    def candidate_pool(self) -> list[dict[str, Any]]:
        '''把当前候选池拿出去'''
        # 这里返回的是候选池的复制切片，外部拿到候选池之后，就不会更改内部候选池的信息
        return self._candidate_pool[:]

    # agent调用工具，最终会进入这里的方法
    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        normalized = self._normalize_parameters(parameters)  # 标准化参数
        # 根据指定参数生成查重key
        key = tuple(str(normalized.get(name, "")).strip().lower() for name in self._duplicate_key_params)
        # 判断这次查询是否重复
        if key and key in self._seen_keys:
            return ToolResponse.success(
                text=(
                    '{"quota_error": true, "reason": "duplicate_query", '
                    '"message": "这个高德景点查询已经调用过，请使用已有候选或换一个不同主题的关键词。"}'
                )
            )
        # 判断调用次数是否超过限制
        if self._calls >= self._max_calls:
            return ToolResponse.success(
                text=(
                    '{"quota_error": true, "reason": "tool_call_limit_exceeded", '
                    '"message": "本轮景点 POI 查询次数已达上限，请从已有候选中筛选，不要继续调用。"}'
                )
            )
        self._calls += 1
        # 把这次记录保存
        if key:
            self._seen_keys.add(key)
        # 进入 原始工具
        result = self._tool.run(normalized)
        response = result if isinstance(result, ToolResponse) else ToolResponse.success(text=str(result))
        # 工具返回结果后，把里面的candidates记录到候选池里，方便后续的验真
        self._record_candidates(response.text)
        response.text = self._compact_response_for_agent(response.text)
        return response

    def run_with_timing(self, parameters: dict[str, Any]) -> ToolResponse:
        start_time = time.time()
        try:
            response = self.run(parameters)
        except Exception as exc:
            response = ToolResponse.error(
                code="INTERNAL_ERROR",
                message=f"工具执行失败: {exc}",
            )

        elapsed_ms = int((time.time() - start_time) * 1000)
        if response.stats is None:
            response.stats = {}
        response.stats["time_ms"] = elapsed_ms
        if response.context is None:
            response.context = {}
        response.context["params_input"] = parameters
        response.context["tool_name"] = self.name
        return response

    def get_parameters(self) -> Any:
        return self._tool.get_parameters()

    def validate_parameters(self, parameters: dict[str, Any]) -> bool:
        return self._tool.validate_parameters(parameters)

    def to_dict(self) -> dict[str, Any]:
        data = self._tool.to_dict()
        data["description"] = self.description  # 把描述信息替换成包装器自己的描述
        return data

    def _normalize_parameters(self, parameters: dict[str, Any]) -> dict[str, Any]:
        if "input" in parameters and isinstance(parameters["input"], dict):
            return parameters["input"]
        return parameters

    def _record_candidates(self, result: str) -> None:
        '''从工具返回结果里提取 candidates，并保存到 _candidate_pool。'''
        data = extract_json_object(result)
        if not data:
            return
        seen = {
            str(item.get("candidate_id") or item.get("name") or "").strip()
            for item in self._candidate_pool
            if isinstance(item, dict)
        }
        # 用于名字去重
        seen_names = {
            str(item.get("name") or "").strip()
            for item in self._candidate_pool
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        for item in data.get("candidates") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("candidate_id") or item.get("name") or "").strip()
            name = str(item.get("name") or "").strip()
            # 去重判断
            if not key or key in seen or (name and name in seen_names):
                continue
            # 记录新的key
            seen.add(key)
            if name:
                seen_names.add(name)
            self._candidate_pool.append(item)

    def _compact_response_for_agent(self, result: str) -> str:
        '''保留完整候选池，但只把决策所需摘要返回给 Agent。'''
        data = extract_json_object(result)
        if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
            return result
        compact = dict(data)
        if self.name == "travel_search_attraction_pois":
            compact["candidates"] = [self._compact_attraction_candidate(item) for item in data.get("candidates") or []]
        elif self.name == "travel_search_hotels":
            compact["candidates"] = [self._compact_hotel_candidate(item) for item in data.get("candidates") or []]
        else:
            return result
        compact["full_candidate_note"] = "后端已保存完整候选；Agent 只需使用 candidate_id/name 做选择，不要编造地址或坐标。"
        return json.dumps(compact, ensure_ascii=False)

    def _compact_attraction_candidate(self, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        return {
            "candidate_id": item.get("candidate_id", ""),
            "name": item.get("name", ""),
            "source_query": item.get("source_query", ""),
            "category": item.get("category", "") or item.get("type", ""),
            "address_hint": self._short_text(item.get("address", ""), 48),
            "data_mode": item.get("data_mode", ""),
        }

    def _compact_hotel_candidate(self, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        return {
            "name": item.get("name", ""),
            "style": item.get("style", ""),
            "star_level": item.get("star_level", 0),
            "nightly_price": item.get("nightly_price", 0.0),
            "price_source": item.get("price_source", ""),
            "nearby_area": item.get("nearby_area", ""),
            "address_hint": self._short_text(location.get("address", "") or item.get("address", ""), 48),
        }

    def _short_text(self, value: Any, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(limit - 1, 0)] + "…"


class TravelPlannerService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.runtime = LangChainRuntime(self.settings.llm)
        self.backend = TravelDataBackend(
            amap_key=self.settings.amap_key,
            qweather_key=self.settings.qweather_key,
            qweather_host=self.settings.qweather_host,
        )
        self.place_service = PlaceService(self.backend)
        self.tool_registry = ToolRegistry()
        self.attraction_tool_registry = ToolRegistry()
        self.hotel_tool_registry = ToolRegistry()
        self._attraction_quota_tools: list[QuotaToolWrapper] = []
        self._hotel_candidate_tools: list[QuotaToolWrapper] = []
        self.mcp_tool = TravelBackendTool(self.backend)
        self._register_expanded_tools()

        self.attraction_agent = AttractionSearchAgent(self.runtime, self.attraction_tool_registry)
        self.weather_agent = WeatherSearchAgent(self.runtime, self.tool_registry)
        self.hotel_agent = HotelSearchAgent(self.runtime, self.hotel_tool_registry)
        self.itinerary_agent = ItineraryPlanningAgent(self.runtime, self.tool_registry)
        self._request_transit_preference = "recommended"

    def health(self) -> HealthResponse:
        return HealthResponse(
            status="ok",
            llm_enabled=self.runtime.available,
            provider=self.runtime.provider_label,
            llm_load_error=self.runtime.load_error,
            available_tools=self.tool_registry.list_tools(),
        )

    def build_trip_plan(
        self,
        request: TravelRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> TripPlan:
        self._emit_progress(
            progress_callback,
            stage="validate",
            percent=8,
            title="校验旅行需求",
            detail="正在检查城市、日期、预算、人数和出行方式。",
        )
        normalized_city = self._normalize_city_input(request.city)
        if normalized_city is None:
            raise ValueError("城市名称编码异常，请重新输入清晰的中文或英文城市名，例如：广州 / Beijing。")

        request = request.model_copy(update={"city": normalized_city})
        request = self._normalize_preference_inputs(request)
        if not self.backend.is_supported_city(request.city):
            raise ValueError(
                f"暂不支持城市“{request.city}”的自动规划：它既不在内置城市列表中，也无法通过地图服务识别。"
            )

        self._request_transit_preference = request.transit_preference
        constraint_context = self._build_constraint_context(request)
        hotel_rotation_policy = self._hotel_rotation_policy(request)

        self._emit_progress(
            progress_callback,
            stage="attraction_profile",
            percent=15,
            title="准备景点候选",
            detail="正在读取城市画像和可用景点基础数据。",
        )
        attraction_profile = self._attraction_profile_research(request)
        attraction_source = self._source_from_attraction_research(attraction_profile)
        if not self.runtime.available:
            raise ValueError("景点 Agent 不可用：当前 LLM 未启用或 API Key 不可用，因此不能由 AttractionSearchAgent 自主生成景点候选。")

        self._emit_progress(
            progress_callback,
            stage="research_agents",
            percent=24,
            title="并行研究景点、天气和酒店",
            detail="AttractionSearchAgent、WeatherSearchAgent 和 HotelSearchAgent 正在并行生成并验真候选。",
        )
        if not self.runtime.available:
            raise ValueError("酒店 Agent 不可用：当前 LLM 未启用或 API Key 不可用，因此不能由 HotelSearchAgent 自主生成酒店候选。")

        attraction_research, weather_research, hotel_research = self._run_research_agents_parallel(
            request,
            attraction_profile,
            constraint_context,
            hotel_rotation_policy,
        )
        attraction_source = self._source_from_attraction_research(attraction_research)
        weather_source = self._source_from_weather_research(weather_research)
        hotel_source = self._source_from_hotel_research(hotel_research)

        self._emit_progress(
            progress_callback,
            stage="meal",
            percent=58,
            title="准备最终行程编排",
            detail="最终 Agent 将生成每日行程和餐饮意图，餐饮 POI 会在每日行程通过校验后统一落地。",
        )
        restaurant_catalog: dict[str, list[RestaurantOption]] = {}

        self._emit_progress(
            progress_callback,
            stage="itinerary",
            percent=70,
            title="行程 Agent 编排",
            detail="正在把景点、酒店、餐饮和天气组合成每日行程。",
        )
        llm_plan = self._build_trip_plan_with_repair(
            request,
            attraction_research,
            weather_research,
            hotel_research,
            constraint_context,
            hotel_rotation_policy,
            restaurant_catalog,
        )
        daily_stays = llm_plan.daily_stays
        llm_plan.planning_source = "llm_generated"
        llm_plan.attraction_data_source = attraction_source
        llm_plan.weather_data_source = weather_source
        if weather_research.source_note:
            llm_plan.notes.append(weather_research.source_note)
        for forecast in weather_research.forecast:
            if forecast.condition == "unknown":
                warning = f"{forecast.date.isoformat()} 暂无天气预报，出发前请再次查询并准备室内替代方案。"
                if warning not in llm_plan.risk_alerts:
                    llm_plan.risk_alerts.append(warning)
        llm_plan.hotel_data_source = (
            self._source_from_hotel_research(hotel_research) or hotel_source
        )
        llm_plan.attraction_search_plan = attraction_research.search_plan
        llm_plan.preference_interpretation = attraction_research.preference_interpretation
        llm_plan.agent_diagnostics = attraction_research.agent_diagnostics
        llm_plan.hotel_candidates = hotel_research.candidates[:]
        self._emit_progress(
            progress_callback,
            stage="route",
            percent=84,
            title="高德路线规划",
            detail="正在核对地点距离、交通耗时和每日时间安排。",
        )
        self._inject_transport_details(
            llm_plan,
            request,
            attraction_research,
            hotel_research,
            restaurant_catalog,
            daily_stays,
        )
        self._check_plan_schedule(llm_plan, request, attraction_research)
        self._refresh_plan_budget(llm_plan, request, daily_stays)
        self._emit_progress(
            progress_callback,
            stage="finalize",
            percent=96,
            title="整理可视化报告",
            detail="正在核对每日安排、住宿晚数和费用。",
        )
        return llm_plan

    def _emit_progress(
        self,
        progress_callback: Callable[[dict[str, Any]], None] | None,
        *,
        stage: str,
        percent: int,
        title: str,
        detail: str,
    ) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                {
                    "stage": stage,
                    "percent": percent,
                    "title": title,
                    "detail": detail,
                }
            )
        except Exception:
            return

    def _run_research_agents_parallel(
        self,
        request: TravelRequest,
        attraction_profile: AttractionResearch,
        constraint_context: dict[str, Any],
        hotel_rotation_policy: dict[str, Any],
    ) -> tuple[AttractionResearch, WeatherResearch, HotelResearch]:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(
                    copy_context().run,
                    self._build_attraction_research_with_repair,
                    request,
                    attraction_profile,
                    constraint_context,
                ): "景点 Agent",
                executor.submit(
                    copy_context().run,
                    self._build_weather_research_with_repair,
                    request,
                    constraint_context,
                ): "天气 Agent",
                executor.submit(
                    copy_context().run,
                    self._build_hotel_research_with_repair,
                    request,
                    constraint_context,
                    hotel_rotation_policy,
                ): "酒店 Agent",
            }

            results: dict[str, AttractionResearch | WeatherResearch | HotelResearch] = {}
            for future in as_completed(futures):
                label = futures[future]
                try:
                    results[label] = future.result()
                except Exception as exc:
                    raise ValueError(f"{label} 并行研究失败：{exc}") from exc

        attraction = results.get("景点 Agent")
        weather = results.get("天气 Agent")
        hotel = results.get("酒店 Agent")
        if not isinstance(attraction, AttractionResearch):
            raise ValueError("景点 Agent 并行研究失败：未返回 AttractionResearch。")
        if not isinstance(weather, WeatherResearch):
            raise ValueError("天气 Agent 并行研究失败：未返回 WeatherResearch。")
        if not isinstance(hotel, HotelResearch):
            raise ValueError("酒店 Agent 并行研究失败：未返回 HotelResearch。")
        return attraction, weather, hotel

    def _register_expanded_tools(self) -> None:
        for tool in self.mcp_tool.get_expanded_tools():
            self.tool_registry.register_tool(tool)
            if tool.name == "travel_search_attraction_pois":
                quota_tool = QuotaToolWrapper(
                    tool,
                    max_calls=8,
                    duplicate_key_params=("city", "query"),
                )
                self._attraction_quota_tools.append(quota_tool)
                self.attraction_tool_registry.register_tool(quota_tool)
            elif tool.name == "travel_get_city_profile":
                self.attraction_tool_registry.register_tool(tool)
            elif tool.name == "travel_search_hotels":
                hotel_tool = QuotaToolWrapper(
                    tool,
                    max_calls=8,
                    duplicate_key_params=("city", "budget_min", "budget_max", "hotel_style", "area_hint", "search_focus"),
                )
                self._hotel_candidate_tools.append(hotel_tool)
                self.hotel_tool_registry.register_tool(hotel_tool)

    def _reset_attraction_tool_quotas(self) -> None:
        for tool in self._attraction_quota_tools:
            tool.reset_quota()

    def _extend_attraction_tool_quotas(self, extra_calls: int) -> None:
        for tool in self._attraction_quota_tools:
            tool.extend_quota(extra_calls)

    def _reset_hotel_tool_quotas(self) -> None:
        for tool in self._hotel_candidate_tools:
            tool.reset_quota()

    def _attraction_candidate_pool(self) -> list[dict[str, Any]]:
        pool: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tool in self._attraction_quota_tools:
            for item in tool.candidate_pool():
                key = str(item.get("candidate_id") or item.get("name") or "").strip()
                if key and key not in seen:
                    seen.add(key)
                    pool.append(item)
        return pool

    def _hotel_candidate_pool(self) -> list[dict[str, Any]]:
        pool: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tool in self._hotel_candidate_tools:
            for item in tool.candidate_pool():
                key = str(item.get("name") or item.get("candidate_id") or "").strip()
                if key and key not in seen:
                    seen.add(key)
                    pool.append(item)
        return pool

    def _attraction_profile_research(self, request: TravelRequest) -> AttractionResearch:
        profile = self.backend.get_city_profile(request.city)
        return AttractionResearch(
            city_overview=profile["profile"],
            selected_attractions=[],
            selection_reasoning=[
                "LLM 可用时，景点搜索由 AttractionSearchAgent 调用 MCP 工具完成；程序这里只准备城市概况。",
            ],
            recommended_night_area=profile["night_area"],
            preference_interpretation={
                "extra_preferences": request.extra_preferences,
                "taboos": request.taboos,
            },
        )

    def _build_constraint_context(self, request: TravelRequest) -> dict[str, Any]:
        positive_labels = [PREFERENCE_LABELS.get(pref, pref) for pref in request.preferences]
        extra_positive = self._split_user_intent_text(request.extra_preferences)
        negative = self._split_user_intent_text(request.taboos)
        joined_negative = "，".join(negative)
        joined_positive = "，".join(positive_labels + extra_positive)

        diet_constraints: list[str] = []
        mobility_constraints: list[str] = []
        hotel_constraints: list[str] = []
        dining_keywords_to_avoid: list[str] = []
        attraction_keywords_to_avoid: list[str] = []

        if any(token in joined_negative for token in ("不吃辣", "不要辣", "不能吃辣", "忌辣", "怕辣")):
            diet_constraints.extend(["不吃辣", "偏清淡"])
            dining_keywords_to_avoid.extend(["辣", "麻辣", "川菜", "湘菜", "辣炒", "麻辣烫", "香辣"])
        if any(token in joined_negative for token in ("素食", "吃素", "不吃肉")):
            diet_constraints.append("素食或少肉")
        if any(token in joined_negative for token in ("清真", "不吃猪")):
            diet_constraints.append("清真或避开猪肉")
        if any(token in joined_negative for token in ("不想爬山", "不要爬山", "少爬山", "不徒步", "少走路")):
            mobility_constraints.append("减少爬山和高强度步行")
            attraction_keywords_to_avoid.extend(["爬山", "登山", "徒步", "山岳", "山顶"])
        if any(token in joined_negative for token in ("怕吵", "安静", "不要吵", "睡眠浅")):
            hotel_constraints.append("住宿优先安静，避开过度夜生活核心噪音区")
        if any(token in joined_positive for token in ("美食", "小吃", "夜市")):
            hotel_constraints.append("兼顾餐饮聚集区，但不能牺牲用户忌讳")
        if any(token in joined_positive for token in ("夜生活", "酒吧", "夜景")):
            hotel_constraints.append("可靠近夜间活动区域，但要兼顾安全和安静")
        if request.transit_preference == "less_walking":
            mobility_constraints.append("公共交通少步行优先")

        return {
            "positive_preferences": positive_labels + extra_positive,
            "negative_constraints": negative,
            "diet_constraints": diet_constraints,
            "mobility_constraints": mobility_constraints,
            "hotel_constraints": hotel_constraints,
            "budget": {"min": request.budget_min, "max": request.budget_max},
            "pace": request.pace,
            "travelers": request.travelers,
            "hotel_style": request.hotel_style,
            "transport_mode": request.transport_mode,
            "transit_preference": request.transit_preference,
            "keywords_to_avoid": {
                "dining": sorted(set(dining_keywords_to_avoid)),
                "attractions": sorted(set(attraction_keywords_to_avoid)),
            },
            "agent_instructions": {
                "attraction": "景点选择必须遵守负向约束，例如不想爬山就避开明显登山/徒步/山岳强度景点。",
                "hotel": "酒店选择必须结合预算、节奏、交通便利性、安静程度和夜间区域偏好。",
                "weather": "天气建议要结合用户约束，例如少走路、怕晒、老人儿童等。",
                "itinerary": "最终餐饮、每日节奏、酒店换住和预算都必须落实用户偏好与忌讳。",
            },
        }

    def _hotel_rotation_policy(self, request: TravelRequest) -> dict[str, Any]:
        interval = {"intense": 1, "balanced": 2, "relaxed": 3}.get(request.pace, 2)
        stay_nights = max(request.stay_nights, 0)
        target = math.ceil(stay_nights / interval) if stay_nights else 0
        return {
            "pace": request.pace,
            "stay_nights": stay_nights,
            "interval_nights": interval,
            "target_hotel_count": max(target, 1 if stay_nights else 0),
            "rule": f"{self._pace_label(request.pace)}节奏：每 {interval} 晚更换一次住宿；最后一天不新增住宿晚数。",
        }

    def _build_attraction_research_with_repair(
        self,
        request: TravelRequest,
        profile: AttractionResearch,
        constraint_context: dict[str, Any],
    ) -> AttractionResearch:
        self._reset_attraction_tool_quotas()
        target_count = self._target_attraction_count(request)
        previous_data: dict[str, Any] | None = None
        errors: list[str] = []
        current_count = 0
        max_attempts = 3
        best_grounded: AttractionResearch | None = None

        for attempt in range(max_attempts):
            if attempt == 0:
                data, diagnostics = self.attraction_agent.research(
                    request,
                    constraint_context=constraint_context,
                    target_count=target_count,
                )
            else:
                self._extend_attraction_tool_quotas(self._repair_attraction_tool_budget(target_count, current_count))
                data, diagnostics = self.attraction_agent.repair(
                    request,
                    errors=errors,
                    candidate_pool_preview=self._candidate_pool_preview(self._attraction_candidate_pool()),
                    previous_data=previous_data,
                    constraint_context=constraint_context,
                    target_count=target_count,
                    current_count=current_count,
                    shortage_count=max(target_count - current_count, 0),
                )
            previous_data = data

            schema = self._validate_schema(data, AttractionSelectionResearch, "景点 Agent")
            if not schema.ok:
                errors = schema.error_list
                continue
            candidate = schema.value
            assert isinstance(candidate, AttractionSelectionResearch)
            candidate.agent_diagnostics = {**diagnostics, **dict(candidate.agent_diagnostics or {})}

            grounding = self._validate_agent_attraction_research(candidate, profile, self._attraction_candidate_pool(), constraint_context)
            current_count = grounding.current_count
            if not grounding.ok:
                errors = grounding.error_list
                continue

            grounded = grounding.value
            assert isinstance(grounded, AttractionResearch)
            count_result = self._validate_attraction_quantity(grounded, target_count)
            current_count = count_result.current_count
            if not count_result.ok:
                if 0 < current_count < target_count:
                    best_grounded = grounded
                errors = count_result.error_list
                continue
            return grounded

        if best_grounded is not None and best_grounded.selected_attractions:
            best_grounded.agent_diagnostics = {
                **dict(best_grounded.agent_diagnostics or {}),
                "quantity_shortage_degraded": True,
                "target_attraction_count": target_count,
                "final_verified_attraction_count": len(best_grounded.selected_attractions),
                "attraction_shortage_count": max(target_count - len(best_grounded.selected_attractions), 0),
                "repair_rounds_used": max_attempts - 1,
                "quantity_errors": errors[:8],
            }
            return best_grounded

        raise ValueError("景点 Agent 多次修复后仍未通过三层校验：" + "；".join(errors[:8]))

    def _build_weather_research_with_repair(
        self,
        request: TravelRequest,
        constraint_context: dict[str, Any],
    ) -> WeatherResearch:
        verified = self.backend.get_weather_forecast(
            request.city, request.start_date.isoformat(), request.end_date.isoformat()
        )
        previous_data: dict[str, Any] | None = None
        errors: list[str] = []
        max_attempts = 3
        for attempt in range(max_attempts):
            if attempt == 0:
                data, _diagnostics = self.weather_agent.research(request, constraint_context=constraint_context)
            else:
                data, _diagnostics = self.weather_agent.repair(
                    request,
                    errors=errors,
                    previous_data=previous_data,
                    constraint_context=constraint_context,
                )
            # Weather facts come from the provider; the agent supplies travel advice.
            if isinstance(data, dict):
                data = {
                    **data,
                    "forecast": verified["forecast"],
                    "data_source": verified["data_source"],
                    "source_note": verified["source_note"],
                }
                if verified["data_mode"] == "unavailable":
                    data["overall_summary"] = verified["message"]
                    data["risk_days"] = [
                        f"{item['date']} 暂无天气预报，出发前再次查询。"
                        for item in verified["forecast"]
                    ]
            previous_data = data
            schema = self._validate_schema(data, WeatherResearch, "天气 Agent")
            if not schema.ok:
                errors = schema.error_list
                continue
            weather = schema.value
            assert isinstance(weather, WeatherResearch)
            grounding = self._validate_weather_research(weather, request)
            if not grounding.ok:
                errors = grounding.error_list
                continue
            return weather
        raise ValueError("天气 Agent 多次修复后仍未通过三层校验：" + "；".join(errors[:8]))

    def _build_hotel_research_with_repair(
        self,
        request: TravelRequest,
        constraint_context: dict[str, Any],
        rotation_policy: dict[str, Any],
    ) -> HotelResearch:
        self._reset_hotel_tool_quotas()
        target_count = int(rotation_policy.get("target_hotel_count", 1) or 1)
        previous_data: dict[str, Any] | None = None
        errors: list[str] = []
        current_count = 0
        max_attempts = 3

        for attempt in range(max_attempts):
            if attempt == 0:
                data, _diagnostics = self.hotel_agent.research(
                    request,
                    constraint_context=constraint_context,
                    rotation_policy=rotation_policy,
                )
            else:
                data, _diagnostics = self.hotel_agent.repair(
                    request,
                    errors=errors,
                    candidate_pool_preview=self._candidate_pool_preview(self._hotel_candidate_pool()),
                    previous_data=previous_data,
                    constraint_context=constraint_context,
                    rotation_policy=rotation_policy,
                    current_count=current_count,
                    shortage_count=max(target_count - current_count, 0),
                )
            previous_data = data

            schema = self._validate_schema(data, HotelSelectionResearch, "酒店 Agent")
            if not schema.ok:
                errors = schema.error_list
                continue
            candidate = schema.value
            assert isinstance(candidate, HotelSelectionResearch)
            grounding = self._validate_agent_hotel_research(candidate, self._hotel_candidate_pool(), constraint_context)
            current_count = grounding.current_count
            if not grounding.ok:
                errors = grounding.error_list
                continue
            grounded = grounding.value
            assert isinstance(grounded, HotelResearch)
            count_result = self._validate_hotel_quantity(grounded, target_count)
            current_count = count_result.current_count
            if not count_result.ok:
                errors = count_result.error_list
                continue
            return grounded

        raise ValueError("酒店 Agent 多次修复后仍未通过三层校验：" + "；".join(errors[:8]))

    def _build_trip_plan_with_repair(
        self,
        request: TravelRequest,
        attraction_research: AttractionResearch,
        weather_research: WeatherResearch,
        hotel_research: HotelResearch,
        constraint_context: dict[str, Any],
        hotel_rotation_policy: dict[str, Any],
        restaurant_catalog: dict[str, list[RestaurantOption]],
    ) -> TripPlan:
        try:
            return self._build_trip_plan_by_day_with_repair(
                request,
                attraction_research,
                weather_research,
                hotel_research,
                constraint_context,
                hotel_rotation_policy,
                restaurant_catalog,
            )
        except ValueError as exc:
            raise ValueError("按天生成最终行程失败：" + str(exc)) from exc

    def _build_trip_plan_by_day_with_repair(
        self,
        request: TravelRequest,
        attraction_research: AttractionResearch,
        weather_research: WeatherResearch,
        hotel_research: HotelResearch,
        constraint_context: dict[str, Any],
        hotel_rotation_policy: dict[str, Any],
        restaurant_catalog: dict[str, list[RestaurantOption]],
    ) -> TripPlan:
        skeleton = self._build_skeleton_with_repair(
            request,
            attraction_research,
            weather_research,
            hotel_research,
            constraint_context,
            hotel_rotation_policy,
        )
        self._align_hotels_with_itinerary(
            request, skeleton, hotel_research, constraint_context
        )
        daily_plans = self._build_day_plans_parallel(
            request,
            skeleton,
            attraction_research,
            weather_research,
            hotel_research,
            constraint_context,
        )

        recommended_hotel = skeleton["recommended_hotel"]
        plan = TripPlan(
            city=request.city,
            travel_theme=str(skeleton["travel_theme"]),
            overview=str(skeleton["overview"]),
            trip_days=request.trip_days,
            planning_source="llm_generated_by_day",
            selected_attractions=attraction_research.selected_attractions[:],
            recommended_hotel=recommended_hotel,
            hotel_candidates=hotel_research.candidates[:],
            daily_stays=skeleton["daily_stays"],
            daily_plans=daily_plans,
            budget=BudgetBreakdown(
                hotel=0.0,
                attractions=0.0,
                food=0.0,
                transport=0.0,
                contingency=0.0,
                total=0.0,
            ),
            packing_tips=list(skeleton.get("packing_tips") or self._packing_tips(weather_research)),
            risk_alerts=list(skeleton.get("risk_alerts") or weather_research.risk_days),
            notes=list(skeleton.get("notes") or []),
        )
        self._resolve_meals_for_daily_plans(
            plan.daily_plans,
            request,
            skeleton,
            attraction_research,
            weather_research,
            constraint_context,
            restaurant_catalog,
        )
        self._refresh_plan_budget(plan, request, plan.daily_stays)
        grounding = self._validate_trip_plan_grounding(
            plan,
            request,
            attraction_research,
            hotel_research,
            {},
            constraint_context,
            hotel_rotation_policy,
        )
        if not grounding.ok:
            raise ValueError("按天生成最终行程未通过校验：" + "；".join(grounding.error_list[:10]))
        assert isinstance(grounding.value, TripPlan)
        return grounding.value

    def _build_skeleton_with_repair(
        self,
        request: TravelRequest,
        attraction_research: AttractionResearch,
        weather_research: WeatherResearch,
        hotel_research: HotelResearch,
        constraint_context: dict[str, Any],
        hotel_rotation_policy: dict[str, Any],
    ) -> dict[str, Any]:
        previous_data: dict[str, Any] | None = None
        errors: list[str] = []
        max_attempts = 3
        for attempt in range(max_attempts):
            if attempt == 0:
                data, _diagnostics = self.itinerary_agent.plan_skeleton(
                    request,
                    attraction_research,
                    weather_research,
                    hotel_research,
                    constraint_context=constraint_context,
                    hotel_rotation_policy=hotel_rotation_policy,
                )
            else:
                data, _diagnostics = self.itinerary_agent.repair_skeleton(
                    request,
                    attraction_research,
                    weather_research,
                    hotel_research,
                    errors=errors,
                    previous_data=previous_data,
                    constraint_context=constraint_context,
                    hotel_rotation_policy=hotel_rotation_policy,
                )
            previous_data = data
            normalized = self._normalize_skeleton_data(data, request, attraction_research, hotel_research)
            if normalized.ok:
                assert isinstance(normalized.value, dict)
                return normalized.value
            errors = normalized.error_list
        raise ValueError("最终行程骨架多次修复后仍未通过校验：" + "；".join(errors[:10]))

    def _build_day_plans_parallel(
        self,
        request: TravelRequest,
        skeleton: dict[str, Any],
        attraction_research: AttractionResearch,
        weather_research: WeatherResearch,
        hotel_research: HotelResearch,
        constraint_context: dict[str, Any],
    ) -> list[DayPlan]:
        max_workers = max(1, min(3, request.trip_days))
        results: dict[int, DayPlan] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._build_day_plan_with_repair,
                    request,
                    day_index,
                    skeleton,
                    attraction_research,
                    weather_research,
                    hotel_research,
                    constraint_context,
                ): day_index
                for day_index in range(1, request.trip_days + 1)
            }
            for future in as_completed(futures):
                day_index = futures[future]
                try:
                    results[day_index] = future.result()
                except Exception as exc:
                    raise ValueError(f"第 {day_index} 天行程 Agent 并行生成失败：{exc}") from exc

        missing_days = [day_index for day_index in range(1, request.trip_days + 1) if day_index not in results]
        if missing_days:
            raise ValueError(f"Day Agent 并行生成缺少日期：{missing_days}")
        return [results[day_index] for day_index in range(1, request.trip_days + 1)]

    def _build_day_plan_with_repair(
        self,
        request: TravelRequest,
        day_index: int,
        skeleton: dict[str, Any],
        attraction_research: AttractionResearch,
        weather_research: WeatherResearch,
        hotel_research: HotelResearch,
        constraint_context: dict[str, Any],
    ) -> DayPlan:
        day_context = self._day_generation_context(
            request,
            day_index,
            skeleton,
            attraction_research,
            weather_research,
        )
        previous_data: dict[str, Any] | None = None
        errors: list[str] = []
        max_attempts = 3
        for attempt in range(max_attempts):
            if attempt == 0:
                data, _diagnostics = self.itinerary_agent.plan_day(
                    request,
                    day_context=day_context,
                    constraint_context=constraint_context,
                )
            else:
                data, _diagnostics = self.itinerary_agent.repair_day(
                    request,
                    day_context=day_context,
                    constraint_context=constraint_context,
                    errors=errors,
                    previous_data=previous_data,
                )
            if isinstance(data, dict) and day_context.get("weather"):
                data = {**data, "weather": day_context["weather"]}
            previous_data = data
            schema = self._validate_schema(data, DayPlan, f"第 {day_index} 天行程 Agent")
            if not schema.ok:
                errors = schema.error_list
                continue
            day = schema.value
            assert isinstance(day, DayPlan)
            validation = self._validate_day_plan_from_skeleton(
                day,
                request,
                day_index,
                skeleton,
                attraction_research,
                hotel_research,
                constraint_context,
            )
            if validation.ok:
                assert isinstance(validation.value, DayPlan)
                return validation.value
            errors = validation.error_list
        raise ValueError(f"第 {day_index} 天行程多次修复后仍未通过校验：" + "；".join(errors[:10]))

    def _resolve_meals_for_daily_plans(
        self,
        daily_plans: list[DayPlan],
        request: TravelRequest,
        skeleton: dict[str, Any],
        attraction_research: AttractionResearch,
        weather_research: WeatherResearch,
        constraint_context: dict[str, Any],
        restaurant_catalog: dict[str, list[RestaurantOption]],
    ) -> None:
        for day_index, day in enumerate(daily_plans, start=1):
            day_context = self._day_generation_context(
                request,
                day_index,
                skeleton,
                attraction_research,
                weather_research,
            )
            self._materialize_meal_intents(day, request, day_context, constraint_context, restaurant_catalog)

    def _validate_schema(
        self,
        data: dict[str, Any] | None,
        model: type[BaseModel],
        agent_label: str,
    ) -> AgentValidationResult:
        if not isinstance(data, dict):
            return AgentValidationResult(False, errors=[f"{agent_label} 第一层 JSON 格式校验失败：未返回可解析 JSON 对象。"])
        try:
            value = model.model_validate(data)
        except ValidationError as exc:
            errors = []
            for error in exc.errors()[:12]:
                loc = ".".join(str(part) for part in error.get("loc", [])) or "<root>"
                errors.append(f"{agent_label} 第一层 JSON 结构错误：{loc}: {error.get('msg', '')}")
            return AgentValidationResult(False, errors=errors)
        except Exception as exc:
            return AgentValidationResult(False, errors=[f"{agent_label} 第一层 JSON 结构错误：{type(exc).__name__}: {exc}"])
        return AgentValidationResult(True, value=value)

    def _normalize_skeleton_data(
        self,
        data: dict[str, Any] | None,
        request: TravelRequest,
        attractions: AttractionResearch,
        hotels: HotelResearch,
    ) -> AgentValidationResult:
        if not isinstance(data, dict):
            return AgentValidationResult(False, errors=["最终行程骨架第一层 JSON 格式校验失败：未返回可解析 JSON 对象。"])

        errors: list[str] = []
        attraction_catalog = {item.name: item for item in attractions.selected_attractions}
        hotel_catalog = {item.name: item for item in hotels.candidates}
        if not attraction_catalog:
            errors.append("最终行程骨架第二层验真失败：已验真景点池为空。")
        if not hotel_catalog:
            errors.append("最终行程骨架第二层验真失败：已验真酒店池为空。")

        recommended_name = str(data.get("recommended_hotel_name") or "").strip()
        resolved_recommended = self._resolve_place_name(recommended_name, hotel_catalog)
        if not resolved_recommended:
            errors.append("最终行程骨架第二层验真失败：recommended_hotel_name 必须来自酒店白名单。")
            recommended_hotel = None
        else:
            recommended_hotel = hotel_catalog[resolved_recommended]

        stays_raw = data.get("daily_stays")
        if not isinstance(stays_raw, list):
            errors.append("最终行程骨架第一层 JSON 结构错误：daily_stays 必须是数组。")
            stays_raw = []
        if len(stays_raw) != request.trip_days:
            errors.append(f"最终行程骨架第三层数量校验失败：daily_stays 应为 {request.trip_days} 天，实际 {len(stays_raw)} 天。")

        daily_stays: list[DailyStayPlan] = []
        previous_end: HotelOption | None = None
        for index in range(request.trip_days):
            raw = stays_raw[index] if index < len(stays_raw) and isinstance(stays_raw[index], dict) else {}
            day_number = index + 1
            current_date = request.start_date + timedelta(days=index)
            start_name = str(raw.get("start_hotel_name") or raw.get("start_hotel") or "").strip()
            end_name = str(raw.get("end_hotel_name") or raw.get("end_hotel") or "").strip()
            start_resolved = self._resolve_place_name(start_name, hotel_catalog)
            end_resolved = self._resolve_place_name(end_name, hotel_catalog)
            if index > 0 and previous_end is not None:
                start_hotel = previous_end
                if start_resolved and start_resolved != previous_end.name:
                    errors.append(f"最终行程骨架第二层验真失败：第 {day_number} 天 start_hotel_name 应等于前一天 end_hotel_name。")
            else:
                start_hotel = hotel_catalog[start_resolved] if start_resolved else recommended_hotel
            if not start_hotel:
                errors.append(f"最终行程骨架第二层验真失败：第 {day_number} 天 start_hotel_name 必须来自酒店白名单。")

            charged_night = bool(raw.get("charged_night", index < request.stay_nights))
            if index >= request.stay_nights:
                charged_night = False
            if charged_night:
                end_hotel = hotel_catalog[end_resolved] if end_resolved else start_hotel
                if not end_hotel:
                    errors.append(f"最终行程骨架第二层验真失败：第 {day_number} 天 end_hotel_name 必须来自酒店白名单。")
            else:
                end_hotel = start_hotel

            hotel_changed = (
                charged_night
                and start_hotel is not None
                and end_hotel is not None
                and start_hotel.name != end_hotel.name
            )
            daily_stays.append(
                DailyStayPlan(
                    day_index=day_number,
                    date=current_date,
                    start_hotel=start_hotel,
                    end_hotel=end_hotel,
                    night_area=str(raw.get("night_area") or ""),
                    charged_night=charged_night,
                    hotel_changed=hotel_changed,
                    reason=str(raw.get("reason") or ""),
                )
            )
            if charged_night and end_hotel is not None:
                previous_end = end_hotel

        charged_count = sum(1 for stay in daily_stays if stay.charged_night)
        if charged_count != request.stay_nights:
            errors.append(f"最终行程骨架第三层数量校验失败：charged_night=true 应为 {request.stay_nights} 晚，实际 {charged_count} 晚。")
        if daily_stays and daily_stays[-1].charged_night and request.trip_days > 1:
            errors.append("最终行程骨架第三层数量校验失败：最后一天 charged_night 必须为 false。")

        assignments_raw = data.get("daily_attraction_assignments")
        if not isinstance(assignments_raw, list):
            errors.append("最终行程骨架第一层 JSON 结构错误：daily_attraction_assignments 必须是数组。")
            assignments_raw = []
        if len(assignments_raw) != request.trip_days:
            errors.append(
                f"最终行程骨架第三层数量校验失败：daily_attraction_assignments 应为 {request.trip_days} 天，实际 {len(assignments_raw)} 天。"
            )

        expected_daily_counts = self._expected_daily_attraction_counts(request, len(attraction_catalog))
        assignments: dict[int, list[Attraction]] = {}
        used_attractions: set[str] = set()
        for index in range(request.trip_days):
            raw = assignments_raw[index] if index < len(assignments_raw) and isinstance(assignments_raw[index], dict) else {}
            day_number = index + 1
            names = raw.get("attraction_names") if isinstance(raw.get("attraction_names"), list) else []
            expected_count = expected_daily_counts[index] if index < len(expected_daily_counts) else 0
            if len(names) != expected_count:
                errors.append(f"最终行程骨架第三层数量校验失败：第 {day_number} 天应分配 {expected_count} 个景点，实际 {len(names)} 个。")
            day_attractions: list[Attraction] = []
            for raw_name in names:
                resolved = self._resolve_place_name(str(raw_name), attraction_catalog)
                if not resolved:
                    errors.append(f"最终行程骨架第二层验真失败：第 {day_number} 天景点「{raw_name}」不在景点白名单。")
                    continue
                if resolved in used_attractions:
                    errors.append(f"最终行程骨架第二层验真失败：景点「{resolved}」被重复分配。")
                    continue
                used_attractions.add(resolved)
                day_attractions.append(attraction_catalog[resolved])
            assignments[day_number] = day_attractions

        errors.extend(
            self._hotel_rotation_sequence_errors(
                daily_stays,
                request,
                self._hotel_rotation_policy(request),
                available_hotel_count=len(hotel_catalog),
                label="最终行程骨架",
            )
        )

        if errors:
            return AgentValidationResult(False, errors=errors[:16], current_count=len(assignments))

        return AgentValidationResult(
            True,
            value={
                "city": request.city,
                "travel_theme": str(data.get("travel_theme") or "城市旅行"),
                "overview": str(data.get("overview") or ""),
                "recommended_hotel": recommended_hotel,
                "daily_stays": daily_stays,
                "assignments": assignments,
                "packing_tips": list(data.get("packing_tips") or []),
                "risk_alerts": list(data.get("risk_alerts") or []),
                "notes": list(data.get("notes") or []),
            },
            current_count=len(assignments),
        )

    def _day_generation_context(
        self,
        request: TravelRequest,
        day_index: int,
        skeleton: dict[str, Any],
        attractions: AttractionResearch,
        weather: WeatherResearch,
    ) -> dict[str, Any]:
        current_date = request.start_date + timedelta(days=day_index - 1)
        forecast_by_date = {item.date: item for item in weather.forecast}
        weather_info = forecast_by_date.get(current_date)
        assigned = skeleton.get("assignments", {}).get(day_index, [])
        stay = self._stay_for_day(skeleton.get("daily_stays", []), day_index - 1, request)
        return {
            "day_index": day_index,
            "date": current_date.isoformat(),
            "start_hotel": self._compact_hotel_for_day_context(stay.start_hotel),
            "end_hotel": self._compact_hotel_for_day_context(stay.end_hotel),
            "night_area": stay.night_area,
            "charged_night": stay.charged_night,
            "hotel_changed": stay.hotel_changed,
            "stay_reason": stay.reason,
            "weather": self._compact_weather_for_day_context(weather_info),
            "assigned_attractions": [self._compact_attraction_for_day_context(item) for item in assigned],
        }

    def _compact_hotel_for_day_context(self, hotel: HotelOption | None) -> dict[str, Any] | None:
        if hotel is None:
            return None
        return {
            "name": hotel.name,
            "nearby_area": hotel.nearby_area,
            "nightly_price": hotel.nightly_price,
            "location": hotel.location.model_dump(),
        }

    def _compact_weather_for_day_context(self, weather: WeatherInfo | None) -> dict[str, Any]:
        if weather is None:
            return {}
        return {
            "date": weather.date.isoformat(),
            "condition": weather.condition,
            "high_c": weather.high_c,
            "low_c": weather.low_c,
            "suggestion": weather.suggestion,
            "data_source": weather.data_source,
        }

    def _compact_attraction_for_day_context(self, attraction: Attraction) -> dict[str, Any]:
        return {
            "name": attraction.name,
            "category": attraction.category,
            "address": attraction.location.address,
            "ticket_price": attraction.ticket_price,
            "recommended_hours": attraction.recommended_hours,
            "summary": attraction.summary,
            "location": attraction.location.model_dump(),
        }

    def _validate_day_plan_from_skeleton(
        self,
        day: DayPlan,
        request: TravelRequest,
        day_index: int,
        skeleton: dict[str, Any],
        attractions: AttractionResearch,
        hotels: HotelResearch,
        constraint_context: dict[str, Any],
    ) -> AgentValidationResult:
        errors: list[str] = []
        current_date = request.start_date + timedelta(days=day_index - 1)
        if day.date != current_date:
            errors.append(f"第 {day_index} 天行程第三层数量校验失败：date 应为 {current_date.isoformat()}，实际 {day.date.isoformat()}。")

        assigned = skeleton.get("assignments", {}).get(day_index, [])
        assigned_catalog = {item.name: item for item in assigned}
        all_attraction_catalog = {item.name: item for item in attractions.selected_attractions}
        expected_names = set(assigned_catalog)
        used_names: set[str] = set()
        meal_types: set[str] = set()
        avoid_dining = constraint_context.get("keywords_to_avoid", {}).get("dining", []) or []

        if not day.meal_intents:
            errors.append(f"第 {day_index} 天行程第一层 JSON 结构错误：必须输出 meal_intents，不能直接编造餐饮 item。")
        for intent in day.meal_intents:
            meal_type = str(intent.meal_type or "").strip()
            if meal_type not in {"breakfast", "lunch", "dinner"}:
                errors.append(
                    f"第 {day_index} 天行程第一层/业务校验失败：餐饮意图 meal_type 必须是 breakfast、lunch、dinner。"
                )
            else:
                if meal_type in meal_types:
                    errors.append(
                        f"第 {day_index} 天重复安排了 {meal_type}，每餐只能有一个意图。"
                    )
                meal_types.add(meal_type)
            if time_bounds(intent.time_range) is None:
                errors.append(
                    f"第 {day_index} 天餐饮 time_range 必须是有效的 HH:MM-HH:MM。"
                )
            if intent.budget_total < 0:
                errors.append(f"第 {day_index} 天行程第一层/业务校验失败：餐饮意图 budget_total 不能为负数。")
            intent_text = f"{intent.anchor_name} {intent.cuisine_intent} {intent.reason}"
            violated = self._constraint_keyword_violations(intent_text, avoid_dining)
            if violated:
                errors.append(f"第 {day_index} 天行程第二层偏好/忌讳验真失败：餐饮意图命中忌讳关键词 {violated}。")

        for item in day.items:
            if time_bounds(item.time_range) is None:
                errors.append(
                    f"第 {day_index} 天「{item.title}」time_range 必须是有效的 HH:MM-HH:MM。"
                )
            if item.item_type == "attraction":
                resolved = self._resolve_place_name(item.location_name or item.title, assigned_catalog)
                if not resolved:
                    if self._resolve_place_name(item.location_name or item.title, all_attraction_catalog):
                        errors.append(f"第 {day_index} 天行程第二层验真失败：景点「{item.location_name or item.title}」不属于当天分配景点。")
                    else:
                        errors.append(f"第 {day_index} 天行程第二层验真失败：景点「{item.location_name or item.title}」不在已验真景点池。")
                    continue
                if resolved in used_names:
                    errors.append(f"第 {day_index} 天行程第二层验真失败：景点「{resolved}」当天重复安排。")
                    continue
                used_names.add(resolved)
                attraction = assigned_catalog[resolved]
                item.title = resolved
                item.location_name = resolved
                item.location_address = attraction.location.address
                item.location = attraction.location.model_copy(deep=True)
                item.estimated_cost = attraction.ticket_price
            elif item.item_type in {"meal", "food"}:
                errors.append(f"第 {day_index} 天行程第二层验真失败：不要输出 meal/food item，餐饮由 meal_intents 统一落地。")
            elif item.item_type == "transport":
                errors.append(f"第 {day_index} 天行程第二层验真失败：不要输出 transport item，交通由程序注入。")
            elif item.item_type == "hotel":
                errors.append(f"第 {day_index} 天行程第二层验真失败：不要输出 hotel item，换宿节点由程序注入。")

        if used_names != expected_names:
            missing = sorted(expected_names - used_names)
            extra = sorted(used_names - expected_names)
            if missing:
                errors.append(f"第 {day_index} 天行程第三层数量校验失败：缺少当天分配景点 {missing}。")
            if extra:
                errors.append(f"第 {day_index} 天行程第二层验真失败：出现未分配景点 {extra}。")

        if day_index < request.trip_days:
            missing_meals = {"breakfast", "lunch", "dinner"} - meal_types
            if missing_meals:
                errors.append(
                    f"第 {day_index} 天行程第三层数量校验失败：非最后一天必须包含早餐、午餐、晚餐，缺少 {sorted(missing_meals)}。"
                )
        else:
            stay = self._stay_for_day(skeleton.get("daily_stays", []), day_index - 1, request)
            if not meal_types:
                errors.append(f"第 {day_index} 天行程第三层数量校验失败：最后一天至少需要 1 个餐饮 intent。")
            if self._day_has_night_activity(day, stay) and "dinner" not in meal_types:
                errors.append(f"第 {day_index} 天行程第三层数量校验失败：最后一天有夜间活动，因此必须包含晚餐 item。")

        if errors:
            return AgentValidationResult(False, errors=errors[:12], current_count=len(day.items))
        return AgentValidationResult(True, value=day, current_count=len(day.items))

    def _materialize_meal_intents(
        self,
        day: DayPlan,
        request: TravelRequest,
        day_context: dict[str, Any],
        constraint_context: dict[str, Any],
        restaurant_catalog: dict[str, list[RestaurantOption]],
        locked_meals: list[DayPlanItem] | None = None,
    ) -> None:
        if not day.meal_intents and not locked_meals:
            return

        existing_non_meals = [
            item
            for item in day.items
            if item.item_type not in {"meal", "food"}
        ]
        day_context = {
            **day_context,
            "date": day.date.isoformat(),
            "activity_timeline": [
                {
                    "name": item.location_name or item.title,
                    "time_range": item.time_range,
                }
                for item in existing_non_meals
                if item.item_type == "attraction"
            ],
        }
        preserved = {
            item.meal_type: item.model_copy(deep=True) for item in locked_meals or []
        }
        meal_items = []
        for intent in day.meal_intents:
            if intent.meal_type in preserved:
                item = preserved.pop(intent.meal_type)
                item.time_range = intent.time_range
            else:
                item = self._meal_item_from_intent(
                    intent, request, day_context, constraint_context, restaurant_catalog
                )
            meal_items.append(item)
        meal_items.extend(preserved.values())
        day.items = self._merge_meal_items_by_time(existing_non_meals, meal_items)

    def _meal_item_from_intent(
        self,
        intent: MealIntent,
        request: TravelRequest,
        day_context: dict[str, Any],
        constraint_context: dict[str, Any],
        restaurant_catalog: dict[str, list[RestaurantOption]],
    ) -> DayPlanItem:
        restaurant = self._search_restaurant_for_intent(intent, request, day_context, constraint_context)
        if restaurant is not None:
            restaurant_catalog.setdefault(str(intent.meal_type), []).append(restaurant)
            total_cost = round(
                max(restaurant.avg_cost_per_person * request.travelers, 0.0), 2
            )
            return DayPlanItem(
                time_range=intent.time_range,
                title=restaurant.name,
                item_type="meal",
                meal_type=intent.meal_type,
                location_name=restaurant.name,
                location_address=restaurant.location.address,
                location=restaurant.location.model_copy(deep=True),
                summary=self._meal_summary(intent, restaurant),
                estimated_cost=total_cost,
                reason=intent.reason
                or f"按{self._meal_type_label(intent.meal_type)}意图匹配真实餐饮 POI。",
                is_route_stop=True,
            )

        anchor = self._best_meal_anchor(intent, day_context)
        return DayPlanItem(
            time_range=intent.time_range,
            title=f"{self._meal_type_label(intent.meal_type)}区域餐饮建议",
            item_type="meal",
            meal_type=intent.meal_type,
            location_name=f"{anchor}附近" if anchor else "附近餐饮区域",
            location_address="",
            summary=(
                f"建议在{anchor or '当天路线附近'}选择{intent.cuisine_intent or '本地餐饮'}。"
                "暂未确定具体餐厅，请就近选择，费用按本餐预留预算估算。"
            ),
            estimated_cost=round(max(intent.budget_total, 0.0), 2),
            reason=intent.reason or "餐饮意图保留为区域建议，不参与详细交通路线。",
            is_route_stop=False,
        )

    def _search_restaurant_for_intent(
        self,
        intent: MealIntent,
        request: TravelRequest,
        day_context: dict[str, Any],
        constraint_context: dict[str, Any],
    ) -> RestaurantOption | None:
        avoid = list(
            dict.fromkeys(
                list(intent.must_avoid or [])
                + list(
                    (constraint_context.get("keywords_to_avoid") or {}).get(
                        "dining", []
                    )
                    or []
                )
            )
        )
        anchors = self._meal_search_anchors(intent, day_context)
        searches = self._meal_search_terms(intent)
        entities = [
            day_context.get("start_hotel"),
            day_context.get("end_hotel"),
            *(day_context.get("assigned_attractions") or []),
        ]
        locations = {
            entity["name"]: entity.get("location")
            for entity in entities
            if entity and entity.get("name")
        }
        visit_date = date.fromisoformat(
            str(day_context.get("date") or request.start_date)
        )
        checked = set()
        for anchor in anchors:
            anchor_location = locations.get(anchor)
            if not valid_location(anchor_location):
                continue
            for preferences, radius_m in searches:
                radius_m = min(
                    radius_m, 1500 if request.transport_mode == "walk" else 2500
                )
                try:
                    payload = self.backend.search_restaurants(
                        city=request.city,
                        anchor=anchor,
                        preferences=preferences,
                        budget_hint=max(intent.budget_total, 0.0),
                        travelers=request.travelers,
                        radius_m=radius_m,
                        anchor_location=anchor_location,
                    )
                except Exception:
                    continue
                if payload.get("data_mode") != "live":
                    continue
                nearby: list[RestaurantOption] = []
                for raw in payload.get("restaurants") or []:
                    try:
                        option = RestaurantOption.model_validate(raw)
                    except Exception:
                        continue
                    if (
                        self._restaurant_matches_intent(option, avoid)
                        and distance_km(anchor_location, option.location)
                        <= radius_m / 1000
                    ):
                        nearby.append(option)
                for option in sorted(
                    nearby,
                    key=lambda option: distance_km(anchor_location, option.location),
                )[:3]:
                    identity = (
                        option.name,
                        option.location.address,
                        option.location.lat,
                        option.location.lng,
                    )
                    if identity in checked:
                        continue
                    # Bound additional provider lookups, including relaxed queries.
                    # If no suitable candidate is found, keep an honest area suggestion.
                    if len(checked) >= 8:
                        return None
                    checked.add(identity)
                    if self._restaurant_can_serve_meal(
                        option, intent, request.city, visit_date
                    ):
                        return option
        return None

    def _restaurant_can_serve_meal(
        self, option: RestaurantOption, intent: MealIntent, city: str, visit_date: date
    ) -> bool:
        details = self.place_service.resolve(
            PlaceLookupRequest(
                city=city,
                places=[
                    PlaceReference(
                        key="meal-candidate",
                        name=option.name,
                        address=option.location.address,
                        lat=option.location.lat,
                        lng=option.location.lng,
                    )
                ],
            )
        ).places[0]
        # Only the matched POI's regular hours are evidence. Missing/complex rules
        # remain unknown and are rechecked by the final schedule and quality gate.
        hours = parse_opening_hours(
            details.opening_hours if details.verified else "", visit_date
        )
        if hours.status == "closed":
            return False
        if hours.status == "unknown":
            return True
        bounds = time_bounds(intent.time_range)
        duration = max(
            (bounds[1] - bounds[0]) if bounds else 0,
            MIN_MEAL_DURATIONS.get(intent.meal_type, 45),
        )
        lower, upper = MEAL_WINDOWS[intent.meal_type]
        return any(
            max(a, lower) + duration <= min(b, upper) for a, b in hours.intervals
        )

    def _meal_search_anchors(
        self, intent: MealIntent, day_context: dict[str, Any]
    ) -> list[str]:
        anchors: list[str] = []

        def add(value: Any) -> None:
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in anchors:
                anchors.append(cleaned)

        meal_type = str(intent.meal_type)
        start_hotel = day_context.get("start_hotel") or {}
        end_hotel = day_context.get("end_hotel") or {}
        assigned = day_context.get("assigned_attractions") or []
        meal_time = time_bounds(intent.time_range)
        timeline = sorted(
            [
                item
                for item in day_context.get("activity_timeline", [])
                if time_bounds(item.get("time_range", ""))
            ],
            key=lambda item: time_bounds(item["time_range"])[0],
        )
        previous = [
            item
            for item in timeline
            if meal_time and time_bounds(item["time_range"])[0] < meal_time[0]
        ]
        following = [
            item
            for item in timeline
            if meal_time and time_bounds(item["time_range"])[0] >= meal_time[0]
        ]
        if meal_type == "breakfast":
            add(start_hotel.get("name"))
            add((start_hotel.get("location") or {}).get("name"))
        elif meal_type == "lunch":
            if previous or following:
                add((previous[-1] if previous else following[0]).get("name"))
            middle = assigned[min(len(assigned) // 2, len(assigned) - 1)] if assigned else {}
            add(middle.get("name"))
            add((middle.get("location") or {}).get("name"))
        elif meal_type == "dinner":
            if previous:
                add(previous[-1].get("name"))
            if assigned:
                last = assigned[-1]
                add(last.get("name"))
                add((last.get("location") or {}).get("name"))
            add(end_hotel.get("name"))
            add((end_hotel.get("location") or {}).get("name"))

        # Arbitrary model-generated areas cannot override today's verified locations.
        known = {
            entity.get("name")
            for entity in [start_hotel, end_hotel, *assigned]
            if entity
        }
        if intent.anchor_name in known:
            add(intent.anchor_name)
        return anchors

    def _meal_search_terms(self, intent: MealIntent) -> list[tuple[str, int]]:
        cuisine = self._sanitize_place_name(intent.cuisine_intent)
        meal_type = str(intent.meal_type)
        if meal_type == "breakfast":
            broad = "早餐,粥粉面,早茶,肠粉"
        elif meal_type == "dinner":
            broad = "本地菜,特色餐厅,海鲜"
        else:
            broad = "本地菜,餐厅,简餐"
        exact = ",".join(self._split_meal_query_terms(cuisine))
        relaxed = ",".join(self._split_meal_query_terms(self._meal_query_without_avoid_words(cuisine)))
        return [
            (exact or broad, 1200),
            (relaxed or broad, 2500),
            (broad, 5000),
        ]

    def _split_meal_query_terms(self, value: str) -> list[str]:
        text = value.lower()
        for marker in ("，", "、", "/", "；", ";", "。"):
            text = text.replace(marker, ",")
        blocked = (
            "不辣",
            "不要辣",
            "不能吃辣",
            "忌辣",
            "避免",
            "不要",
            "不能",
            "忌",
            "no",
            "not",
            "avoid",
            "without",
        )
        raw_parts: list[str] = []
        for chunk in text.split(","):
            words = [word.strip() for word in chunk.split() if word.strip()]
            if len(words) <= 1:
                raw_parts.append(chunk)
                continue
            skip_next = False
            for word in words:
                if skip_next:
                    skip_next = False
                    continue
                if word in {"no", "not", "avoid", "without"}:
                    skip_next = True
                    continue
                raw_parts.append(word)

        terms: list[str] = []
        for part in raw_parts:
            term = part.strip().lower()
            if not term or any(marker in term for marker in blocked):
                continue
            if term not in terms:
                terms.append(term)
        return terms[:4]

    def _meal_query_without_avoid_words(self, value: str) -> str:
        text = value
        for marker in ("不辣", "不要辣", "清淡", "少油", "少盐", "避免", "忌"):
            text = text.replace(marker, " ")
        return " ".join(part for part in text.split() if part)

    def _restaurant_matches_intent(self, option: RestaurantOption, avoid: list[str]) -> bool:
        if not option.name or option.location.lat == 0.0 and option.location.lng == 0.0:
            return False
        text = " ".join(
            [
                option.name,
                option.cuisine,
                option.summary,
                option.location.name,
                option.location.address,
            ]
        )
        return not self._constraint_keyword_violations(text, avoid)

    def _best_meal_anchor(self, intent: MealIntent, day_context: dict[str, Any]) -> str:
        anchors = self._meal_search_anchors(intent, day_context)
        return anchors[0] if anchors else ""

    def _meal_summary(self, intent: MealIntent, restaurant: RestaurantOption) -> str:
        cuisine = (intent.cuisine_intent or restaurant.cuisine or "当地餐饮").rstrip(
            "。；，; "
        )
        reason = intent.reason.strip().rstrip("。；，; ")
        return f"{self._meal_type_label(intent.meal_type)}建议：{cuisine}。" + (
            f"{reason}。" if reason else ""
        )

    def _merge_meal_items_by_time(
        self,
        non_meal_items: list[DayPlanItem],
        meal_items: list[DayPlanItem],
    ) -> list[DayPlanItem]:
        return sorted(
            non_meal_items + meal_items,
            key=lambda item: self._time_sort_key(item.time_range),
        )

    def _time_sort_key(self, value: str) -> tuple[int, str]:
        text = str(value or "").strip()
        start = text.split("-", 1)[0].strip()
        hour_text = start.split(":", 1)[0].strip()
        if hour_text.isdigit():
            hour = int(hour_text)
            if 0 <= hour <= 23:
                return (hour, text)
        return (99, text)

    def _meal_type_label(self, meal_type: str) -> str:
        return {
            "breakfast": "早餐",
            "lunch": "午餐",
            "dinner": "晚餐",
        }.get(str(meal_type), "餐饮")

    def _candidate_pool_preview(self, candidate_pool: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
        preview: list[dict[str, Any]] = []
        for item in candidate_pool[:limit]:
            location = item.get("location") if isinstance(item.get("location"), dict) else {}
            if item.get("nightly_price") is not None or item.get("nearby_area"):
                preview.append(
                    {
                        "name": item.get("name", ""),
                        "nightly_price": item.get("nightly_price", 0.0),
                        "nearby_area": item.get("nearby_area", ""),
                        "address_hint": self._short_text(item.get("address", "") or location.get("address", ""), 48),
                    }
                )
            else:
                preview.append(
                    {
                        "candidate_id": item.get("candidate_id", ""),
                        "name": item.get("name", ""),
                        "source_query": item.get("source_query", ""),
                        "category": item.get("category", ""),
                        "address_hint": self._short_text(item.get("address", ""), 48),
                    }
                )
        return preview

    @staticmethod
    def _short_text(value: Any, limit: int) -> str:
        text = str(value or "").strip()
        return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"

    def _validate_agent_attraction_research(
        self,
        candidate: AttractionSelectionResearch,
        profile: AttractionResearch,
        candidate_pool: list[dict[str, Any]],
        constraint_context: dict[str, Any],
    ) -> AgentValidationResult:
        errors: list[str] = []
        diagnostics = dict(candidate.agent_diagnostics or {})
        diagnostics["candidate_pool_size"] = len(candidate_pool)
        diagnostics["candidate_selected_count"] = len(candidate.selected_attractions)
        if not candidate.selected_attractions:
            errors.append("景点 Agent 第二层验真失败：selected_attractions 为空。")
        if not candidate_pool:
            errors.append("景点 Agent 第二层验真失败：本轮工具候选池为空，必须调用 travel_search_attraction_pois。")
            candidate.agent_diagnostics = diagnostics
            return AgentValidationResult(False, errors=errors, current_count=0)

        by_id = {
            self._safe_pool_key(item.get("candidate_id")): item
            for item in candidate_pool
            if self._safe_pool_key(item.get("candidate_id"))
        }
        by_name = {
            self._sanitize_place_name(str(item.get("name", ""))): item
            for item in candidate_pool
            if self._sanitize_place_name(str(item.get("name", "")))
        }
        grounded: list[Attraction] = []
        seen_names: set[str] = set()
        avoid_keywords = constraint_context.get("keywords_to_avoid", {}).get("attractions", []) or []

        for item in candidate.selected_attractions:
            source = (
                by_id.get(self._safe_pool_key(item.candidate_id))
                or by_name.get(self._sanitize_place_name(item.name))
            )
            if source is None:
                errors.append(f"景点 Agent 第二层验真失败：「{item.name}」不在本轮工具候选池中。")
                continue

            name = str(source.get("name") or item.name).strip()
            if not name:
                errors.append("景点 Agent 第二层验真失败：工具候选存在空名称。")
                continue
            if name in seen_names:
                errors.append(f"景点 Agent 第二层验真失败：重复景点「{name}」。")
                continue
            source_text = " ".join(
                [
                    name,
                    str(source.get("category") or ""),
                    str(source.get("type") or ""),
                    str(source.get("address") or ""),
                    item.reason,
                    " ".join(item.matched_preferences),
                ]
            )
            text = source_text
            violated = self._constraint_keyword_violations(text, avoid_keywords)
            if violated:
                errors.append(f"景点 Agent 第二层偏好/忌讳验真失败：「{name}」命中忌讳关键词 {violated}。")
                continue

            seen_names.add(name)
            summary = item.reason or str(source.get("type") or "真实 POI 景点候选")
            category = str(source.get("category") or source.get("source_query") or "景点")
            source_query = str(source.get("source_query") or "")
            try:
                grounded.append(
                    Attraction.model_validate(
                        {
                            "candidate_id": str(source.get("candidate_id") or item.candidate_id or ""),
                            "source_query": source_query,
                            "score": item.score,
                            "matched_preferences": item.matched_preferences,
                            "taboo_check": item.taboo_check,
                            "name": name,
                            "category": category,
                            "tags": [value for value in [source_query, category] if value],
                            "summary": summary,
                            "recommended_hours": self._estimate_attraction_hours(category, source_query),
                            "ticket_price": self._estimate_attraction_ticket_price(source),
                            "best_time": self._estimate_attraction_best_time(category, source_query),
                            "location": {
                                "name": name,
                                "address": str(source.get("address") or ""),
                                "lat": float(source.get("lat", 0.0) or 0.0),
                                "lng": float(source.get("lng", 0.0) or 0.0),
                            },
                        }
                    )
                )
            except Exception as exc:
                errors.append(f"景点 Agent 第二层验真失败：「{name}」无法规范化为 Attraction：{exc}")

        diagnostics["grounded_selected_count"] = len(grounded)
        if errors:
            candidate.agent_diagnostics = diagnostics
            return AgentValidationResult(False, errors=errors[:12], current_count=len(grounded))

        reasoning = list(candidate.selection_reasoning or [])
        reasoning.append("已通过第二层验真：景点均来自本轮工具候选池，并检查了用户偏好/忌讳。")
        grounded_research = AttractionResearch(
            city_overview=candidate.city_overview or profile.city_overview,
            selected_attractions=grounded,
            selection_reasoning=reasoning,
            recommended_night_area=candidate.recommended_night_area or profile.recommended_night_area,
            search_plan=candidate.search_plan or profile.search_plan,
            preference_interpretation=candidate.preference_interpretation or profile.preference_interpretation,
            agent_diagnostics={**diagnostics, "grounding_ok": True},
        )
        return AgentValidationResult(True, value=grounded_research, current_count=len(grounded))

    def _estimate_attraction_hours(self, category: str, source_query: str) -> float:
        text = f"{category} {source_query}".lower()
        if any(token in text for token in ("主题乐园", "游乐园", "动物园", "海洋馆", "景区")):
            return 3.0
        if any(token in text for token in ("博物馆", "美术馆", "艺术馆", "展览", "展馆")):
            return 2.0
        if any(token in text for token in ("老街", "古城", "街区", "公园", "广场", "夜市")):
            return 1.5
        return 2.0

    def _estimate_attraction_ticket_price(self, source: dict[str, Any]) -> float:
        for key in ("ticket_price", "price", "cost"):
            value = source.get(key)
            if value in (None, ""):
                continue
            try:
                return max(float(value), 0.0)
            except Exception:
                continue
        text = " ".join(
            [
                str(source.get("category") or ""),
                str(source.get("source_query") or ""),
                str(source.get("type") or ""),
                str(source.get("name") or ""),
            ]
        )
        if any(token in text for token in ("主题乐园", "游乐园", "温泉", "海洋馆", "动物园")):
            return 80.0
        if any(token in text for token in ("景区", "风景区")):
            return 40.0
        return 0.0

    def _estimate_attraction_best_time(self, category: str, source_query: str) -> str:
        text = f"{category} {source_query}".lower()
        if any(token in text for token in ("夜", "夜市", "酒吧", "夜生活", "灯光")):
            return "night"
        return "daytime"

    def _validate_attraction_quantity(self, research: AttractionResearch, target_count: int) -> AgentValidationResult:
        current = len(research.selected_attractions)
        if current < target_count:
            return AgentValidationResult(
                False,
                errors=[
                    f"景点 Agent 第三层数量校验失败：目标 {target_count} 个不重复景点，当前通过验真 {current} 个，还缺 {target_count - current} 个。请继续调用工具换 query 扩展候选池。"
                ],
                current_count=current,
            )
        if current > target_count:
            return AgentValidationResult(
                False,
                errors=[f"景点 Agent 第三层数量校验失败：目标 {target_count} 个景点，但输出 {current} 个，不能超过目标数量。"],
                current_count=current,
            )
        return AgentValidationResult(True, value=research, current_count=current)

    def _validate_weather_research(self, weather: WeatherResearch, request: TravelRequest) -> AgentValidationResult:
        errors: list[str] = []
        expected_dates = [request.start_date + timedelta(days=index) for index in range(request.trip_days)]
        forecast_by_date = {item.date: item for item in weather.forecast}
        for date_value in expected_dates:
            if date_value not in forecast_by_date:
                errors.append(f"天气 Agent 第三层数量校验失败：缺少 {date_value.isoformat()} 的 forecast。")
        for item in weather.forecast:
            if item.condition == "unknown":
                if item.high_c is not None or item.low_c is not None:
                    errors.append(f"天气 Agent 第二层验真失败：{item.date.isoformat()} 未知天气必须使用 null 温度。")
            elif item.high_c is None or item.low_c is None:
                errors.append(f"天气 Agent 第二层验真失败：{item.date.isoformat()} 已知天气缺少温度。")
            elif item.high_c < item.low_c:
                errors.append(f"天气 Agent 第二层验真失败：{item.date.isoformat()} high_c 小于 low_c。")
        if errors:
            return AgentValidationResult(False, errors=errors[:12], current_count=len(weather.forecast))
        return AgentValidationResult(True, value=weather, current_count=len(weather.forecast))

    def _validate_agent_hotel_research(
        self,
        candidate: HotelSelectionResearch,
        candidate_pool: list[dict[str, Any]],
        constraint_context: dict[str, Any],
    ) -> AgentValidationResult:
        errors: list[str] = []
        catalog: dict[str, HotelOption] = {}
        for item in candidate_pool:
            try:
                hotel = HotelOption.model_validate(item)
            except Exception:
                continue
            if hotel.name and hotel.name not in catalog:
                catalog[hotel.name] = hotel
        if not catalog:
            return AgentValidationResult(False, errors=["酒店 Agent 第二层验真失败：本轮 travel_search_hotels 工具候选池为空。"], current_count=0)
        if not candidate.candidate_names:
            errors.append("酒店 Agent 第二层验真失败：candidate_names 为空。")

        grounded_candidates: list[HotelOption] = []
        seen_names: set[str] = set()
        for raw_name in candidate.candidate_names:
            resolved_name = self._resolve_place_name(raw_name, catalog)
            if not resolved_name:
                errors.append(f"酒店 Agent 第二层验真失败：「{raw_name}」不在 travel_search_hotels 工具候选池中。")
                continue
            canonical = catalog[resolved_name]
            if canonical.name in seen_names:
                errors.append(f"酒店 Agent 第二层验真失败：重复酒店「{canonical.name}」。")
                continue
            seen_names.add(canonical.name)
            grounded_candidates.append(canonical)

        recommended: HotelOption | None = None
        if candidate.recommended_hotel_name:
            resolved = self._resolve_place_name(candidate.recommended_hotel_name, {hotel.name: hotel for hotel in grounded_candidates})
            if not resolved:
                errors.append("酒店 Agent 第二层验真失败：recommended_hotel_name 必须来自 candidate_names。")
            else:
                recommended = next(hotel for hotel in grounded_candidates if hotel.name == resolved)
        elif grounded_candidates:
            errors.append("酒店 Agent 第二层验真失败：recommended_hotel_name 不能为空，且必须来自 candidate_names。")

        if errors:
            return AgentValidationResult(False, errors=errors[:12], current_count=len(grounded_candidates))

        reasoning = list(candidate.selection_reasoning or [])
        reasoning.append("已通过第二层验真：酒店均来自 travel_search_hotels 工具候选池，并保留 Agent 自主选择的候选。")
        return AgentValidationResult(
            True,
            value=HotelResearch(
                candidates=grounded_candidates,
                recommended_hotel=recommended or grounded_candidates[0],
                selection_reasoning=reasoning,
            ),
            current_count=len(grounded_candidates),
        )

    def _validate_hotel_quantity(self, research: HotelResearch, target_count: int) -> AgentValidationResult:
        current = len(research.candidates)
        if current < target_count:
            return AgentValidationResult(
                False,
                errors=[
                    f"酒店 Agent 第三层数量校验失败：目标 {target_count} 个酒店，当前通过验真 {current} 个，还缺 {target_count - current} 个。请继续调用 travel_search_hotels，换 area_hint/search_focus 扩展候选池。"
                ],
                current_count=current,
            )
        if current > target_count:
            return AgentValidationResult(
                False,
                errors=[f"酒店 Agent 第三层数量校验失败：目标 {target_count} 个酒店，但输出 {current} 个，不能超过目标数量。"],
                current_count=current,
            )
        return AgentValidationResult(True, value=research, current_count=current)

    def _validate_trip_plan_grounding(
        self,
        plan: TripPlan,
        request: TravelRequest,
        attractions: AttractionResearch,
        hotels: HotelResearch,
        restaurant_catalog: dict[str, list[RestaurantOption]],
        constraint_context: dict[str, Any],
        hotel_rotation_policy: dict[str, Any],
    ) -> AgentValidationResult:
        errors: list[str] = []
        attraction_catalog = {item.name: item for item in attractions.selected_attractions}
        hotel_catalog = {item.name: item for item in hotels.candidates}
        if plan.trip_days != request.trip_days:
            errors.append(f"最终 Agent 第三层数量校验失败：trip_days 应为 {request.trip_days}，实际 {plan.trip_days}。")
        if len(plan.daily_plans) != request.trip_days:
            errors.append(f"最终 Agent 第三层数量校验失败：daily_plans 应为 {request.trip_days} 天，实际 {len(plan.daily_plans)} 天。")
        if len(plan.daily_stays) != request.trip_days:
            errors.append(f"最终 Agent 第三层数量校验失败：daily_stays 应为 {request.trip_days} 天，实际 {len(plan.daily_stays)} 天。")

        charged_count = sum(1 for stay in plan.daily_stays if stay.charged_night)
        if charged_count != request.stay_nights:
            errors.append(f"最终 Agent 第三层数量校验失败：charged_night=true 应为 {request.stay_nights} 晚，实际 {charged_count} 晚。")
        if plan.daily_stays and plan.daily_stays[-1].charged_night and request.trip_days > 1:
            errors.append("最终 Agent 第三层数量校验失败：最后一天通常不应新增住宿晚数，charged_night 应为 false。")

        used_attractions: set[str] = set()
        avoid_dining = constraint_context.get("keywords_to_avoid", {}).get("dining", []) or []
        for day_index, day in enumerate(plan.daily_plans, start=1):
            meal_count = 0
            has_dinner_item = False
            stay = plan.daily_stays[day_index - 1] if day_index - 1 < len(plan.daily_stays) else None
            for item in day.items:
                if item.item_type == "attraction":
                    resolved = self._resolve_place_name(item.location_name or item.title, attraction_catalog)
                    if not resolved:
                        errors.append(f"最终 Agent 第二层验真失败：第 {day_index} 天景点「{item.location_name or item.title}」不在已验真的景点池中。")
                    elif resolved in used_attractions:
                        errors.append(f"最终 Agent 第二层验真失败：景点「{resolved}」被跨天重复安排。")
                    else:
                        used_attractions.add(resolved)
                        attraction = attraction_catalog[resolved]
                        item.location_name = resolved
                        item.title = resolved
                        item.location_address = attraction.location.address
                        item.location = attraction.location.model_copy(deep=True)
                elif item.item_type in {"meal", "food"}:
                    meal_count += 1
                    meal_text = f"{item.title} {item.location_name} {item.summary} {item.reason}"
                    if any(marker in meal_text for marker in ("晚餐", "晚饭", "晚市", "夜宵", "dinner")):
                        has_dinner_item = True
                    violated = self._constraint_keyword_violations(meal_text, avoid_dining)
                    if violated:
                        errors.append(f"最终 Agent 第二层偏好/忌讳验真失败：第 {day_index} 天餐饮「{item.title}」命中忌讳关键词 {violated}。")
                    if item.estimated_cost < 0:
                        errors.append(f"最终 Agent 第一层/业务校验失败：第 {day_index} 天餐饮「{item.title}」estimated_cost 不能为负数。")
                    resolved_anchor = self._resolve_route_anchor(
                        item.location_name or item.title,
                        attraction_catalog=attraction_catalog,
                        hotel_catalog=hotel_catalog,
                        restaurant_catalog={},
                        region_candidates=self._collect_region_candidates(attractions, hotels, plan.daily_stays),
                    )
                    if not resolved_anchor and not item.is_route_stop:
                        item.location_address = ""
                elif item.item_type == "hotel":
                    resolved = self._resolve_place_name(item.location_name or item.title, hotel_catalog)
                    if not resolved:
                        errors.append(f"最终 Agent 第二层验真失败：第 {day_index} 天换宿节点「{item.location_name or item.title}」不在已验真的酒店池中。")
                elif item.item_type == "transport":
                    errors.append("最终 Agent 第二层验真失败：最终 Agent 不应输出 transport item，交通由程序在最后注入。")
            if day_index < request.trip_days:
                if meal_count < 3:
                    errors.append(f"最终 Agent 第三层数量校验失败：第 {day_index} 天至少需要早餐、午餐、晚餐 3 个餐饮 item，实际 {meal_count} 个。")
            else:
                if meal_count < 1:
                    errors.append(f"最终 Agent 第三层数量校验失败：最后一天至少需要 1 个餐饮 item，实际 {meal_count} 个。")
                if self._day_has_night_activity(day, stay) and not has_dinner_item:
                    errors.append("最终 Agent 第三层数量校验失败：最后一天仍安排夜间活动或晚间交通，因此必须包含晚餐 item。")
        for index, stay in enumerate(plan.daily_stays):
            day_number = index + 1
            if stay.day_index != day_number:
                errors.append(f"最终 Agent 第三层数量校验失败：daily_stays[{index}].day_index 应为 {day_number}。")
            for label, hotel in (("start_hotel", stay.start_hotel), ("end_hotel", stay.end_hotel)):
                if hotel is None:
                    errors.append(f"最终 Agent 第二层验真失败：第 {day_number} 天 {label} 不能为空，必须来自酒店候选池。")
                    continue
                resolved = self._resolve_place_name(hotel.name, hotel_catalog)
                if not resolved:
                    errors.append(f"最终 Agent 第二层验真失败：第 {day_number} 天 {label}「{hotel.name}」不在已验真的酒店池中。")
                else:
                    canonical = hotel_catalog[resolved]
                    if label == "start_hotel":
                        stay.start_hotel = canonical
                    else:
                        stay.end_hotel = canonical
            if index > 0 and stay.start_hotel and plan.daily_stays[index - 1].end_hotel:
                previous = plan.daily_stays[index - 1].end_hotel
                if previous and stay.start_hotel.name != previous.name:
                    errors.append(f"最终 Agent 第二层验真失败：第 {day_number} 天 start_hotel 应等于前一天 end_hotel。")
            expected_changed = (
                stay.charged_night
                and stay.start_hotel is not None
                and stay.end_hotel is not None
                and stay.start_hotel.name != stay.end_hotel.name
            )
            if stay.hotel_changed != expected_changed:
                errors.append(f"最终 Agent 第二层验真失败：第 {day_number} 天 hotel_changed 与 start_hotel/end_hotel 是否变化不一致。")

        errors.extend(
            self._hotel_rotation_sequence_errors(
                plan.daily_stays,
                request,
                hotel_rotation_policy,
                available_hotel_count=len(hotel_catalog),
                label="最终 Agent",
            )
        )

        if plan.recommended_hotel is not None:
            resolved_hotel = self._resolve_place_name(plan.recommended_hotel.name, hotel_catalog)
            if not resolved_hotel:
                errors.append("最终 Agent 第二层验真失败：recommended_hotel 必须来自已验真的酒店池。")
            else:
                plan.recommended_hotel = hotel_catalog[resolved_hotel]
        else:
            errors.append("最终 Agent 第二层验真失败：recommended_hotel 不能为空，必须来自已验真的酒店池。")

        budget_errors = self._validate_agent_budget(plan)
        errors.extend(budget_errors)

        if attractions.selected_attractions:
            plan.selected_attractions = attractions.selected_attractions[:]
        plan.hotel_candidates = hotels.candidates[:]

        if errors:
            return AgentValidationResult(False, errors=errors[:16], current_count=len(plan.daily_plans))
        return AgentValidationResult(True, value=plan, current_count=len(plan.daily_plans))

    def _validate_agent_budget(self, plan: TripPlan) -> list[str]:
        errors: list[str] = []
        budget = plan.budget
        values = {
            "hotel": budget.hotel,
            "attractions": budget.attractions,
            "food": budget.food,
            "transport": budget.transport,
            "contingency": budget.contingency,
            "total": budget.total,
        }
        for key, value in values.items():
            if value < 0:
                errors.append(f"最终 Agent 第一层/业务校验失败：budget.{key} 不能为负数。")
        subtotal = budget.hotel + budget.attractions + budget.food + budget.transport + budget.contingency
        tolerance = max(20.0, subtotal * 0.08)
        if abs(budget.total - subtotal) > tolerance:
            errors.append(
                f"最终 Agent 第二层预算验真失败：budget.total={budget.total} 与各项合计 {round(subtotal, 2)} 差距过大。"
            )
        return errors

    def _hotel_rotation_sequence_errors(
        self,
        daily_stays: list[DailyStayPlan],
        request: TravelRequest,
        hotel_rotation_policy: dict[str, Any],
        *,
        available_hotel_count: int,
        label: str,
    ) -> list[str]:
        errors: list[str] = []
        hotel_names_by_charged_night = [
            stay.end_hotel.name
            for stay in daily_stays
            if stay.charged_night and stay.end_hotel is not None
        ]
        target_hotel_count = int(hotel_rotation_policy.get("target_hotel_count", 1) or 1)
        interval = int(hotel_rotation_policy.get("interval_nights", 2) or 2)
        used_hotel_count = len(dict.fromkeys(hotel_names_by_charged_night))

        if available_hotel_count >= target_hotel_count and used_hotel_count != target_hotel_count:
            errors.append(f"{label} 第三层数量校验失败：按酒店轮换策略应使用 {target_hotel_count} 个不同酒店，实际使用 {used_hotel_count} 个。")

        if used_hotel_count <= 1:
            return errors

        expected_group_count = math.ceil(request.stay_nights / interval) if interval else 1
        group_hotels: list[str] = []
        for group_index in range(expected_group_count):
            start = group_index * interval
            end = min(start + interval, len(hotel_names_by_charged_night))
            names = hotel_names_by_charged_night[start:end]
            if not names:
                continue
            unique_names = list(dict.fromkeys(names))
            if len(unique_names) != 1:
                errors.append(
                    f"{label} 第三层数量校验失败：第 {group_index + 1} 个住宿组应连续住同一酒店，实际为 {unique_names}。"
                )
            group_hotels.append(unique_names[0])

        for index in range(1, len(group_hotels)):
            if group_hotels[index] == group_hotels[index - 1]:
                errors.append(
                    f"{label} 第三层数量校验失败：第 {index + 1} 个住宿组应按每 {interval} 晚换宿策略更换酒店。"
                )
        return errors

    def _day_has_night_activity(self, day: DayPlan, stay: DailyStayPlan | None = None) -> bool:
        markers = (
            "夜间",
            "夜游",
            "夜市",
            "夜生活",
            "晚间",
            "晚上",
            "傍晚",
            "夜景",
            "夜宵",
            "晚班",
            "晚高峰",
        )
        text_parts = [day.route_summary]
        if stay is not None:
            text_parts.append(stay.reason)
        for item in day.items:
            text_parts.extend(
                [
                    item.time_range,
                    item.title,
                    item.location_name,
                    item.summary,
                    item.reason,
                    item.from_location,
                    item.to_location,
                ]
            )
        text = " ".join(str(part or "") for part in text_parts)
        if any(marker in text for marker in markers):
            return True
        for item in day.items:
            if item.time_range and self._time_range_has_evening(item.time_range):
                return True
        return False

    def _time_range_has_evening(self, value: str) -> bool:
        for hour in range(18, 24):
            if f"{hour:02d}:" in value or f"{hour}:" in value:
                return True
        return False

    def _constraint_keyword_violations(self, text: str, keywords: list[str]) -> list[str]:
        if not text:
            return []
        safe_markers = ("避开", "避免", "不含", "不加", "不要", "无辣", "少辣", "清淡", "非辣", "不辣")
        violations: list[str] = []
        for keyword in keywords:
            if not keyword or keyword not in text:
                continue
            positions = [index for index in range(len(text)) if text.startswith(keyword, index)]
            unsafe = False
            for position in positions:
                window = text[max(0, position - 8) : position + len(keyword) + 8]
                if not any(marker in window for marker in safe_markers):
                    unsafe = True
                    break
            if unsafe:
                violations.append(keyword)
        return violations

    def _normalize_preference_inputs(self, request: TravelRequest) -> TravelRequest:
        positive_parts = self._split_user_intent_text(request.extra_preferences)
        taboo_positive_parts: list[str] = []
        true_taboo_parts: list[str] = []
        ambiguous_parts: list[str] = []

        for part in self._split_user_intent_text(request.taboos):
            polarity = self._intent_polarity(part)
            if polarity == "positive":
                taboo_positive_parts.append(part)
            elif polarity == "negative":
                true_taboo_parts.append(part)
            else:
                ambiguous_parts.append(part)

        merged_positive = self._join_unique_text_parts(positive_parts + taboo_positive_parts)
        merged_taboos = self._join_unique_text_parts(true_taboo_parts + ambiguous_parts)
        if merged_positive != request.extra_preferences or merged_taboos != request.taboos:
            return request.model_copy(
                update={
                    "extra_preferences": merged_positive,
                    "taboos": merged_taboos,
                }
            )
        return request

    def _split_user_intent_text(self, text: str) -> list[str]:
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        for sep in ["，", "、", ";", "；", "\n", "|"]:
            cleaned = cleaned.replace(sep, ",")
        return [part.strip() for part in cleaned.split(",") if part.strip()]

    def _intent_polarity(self, text: str) -> str:
        lowered = text.strip().lower()
        negative_markers = [
            "不想",
            "不要",
            "不去",
            "不吃",
            "不喜欢",
            "讨厌",
            "忌讳",
            "避开",
            "避免",
            "不能",
            "拒绝",
            "少点",
            "少去",
            "no ",
            "avoid",
            "hate",
            "dislike",
        ]
        positive_markers = [
            "喜欢",
            "想去",
            "想玩",
            "希望",
            "偏好",
            "爱",
            "要去",
            "想看",
            "想体验",
            "prefer",
            "like",
            "want",
        ]
        if any(marker in lowered for marker in negative_markers):
            return "negative"
        if any(marker in lowered for marker in positive_markers):
            return "positive"
        return "ambiguous"

    def _join_unique_text_parts(self, parts: list[str]) -> str:
        seen: set[str] = set()
        result: list[str] = []
        for part in parts:
            cleaned = part.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                result.append(cleaned)
        return "，".join(result)

    def _align_hotels_with_itinerary(
        self,
        request: TravelRequest,
        skeleton: dict[str, Any],
        hotels: HotelResearch,
        constraint_context: dict[str, Any],
    ) -> None:
        """Validate each lodging block against the days that actually use it."""
        stays = skeleton.get("daily_stays", [])
        assignments = skeleton.get("assignments", {})
        groups: dict[str, list[list[Attraction]]] = {}
        originals: dict[str, HotelOption] = {}
        for stay in stays:
            points = [
                point
                for point in assignments.get(stay.day_index, [])
                if valid_location(point.location)
            ]
            for hotel in (stay.start_hotel, stay.end_hotel):
                if hotel and points:
                    originals[hotel.name] = hotel
                    groups.setdefault(hotel.name, []).append(points)
        max_km = (
            4
            if request.transport_mode == "walk"
            else (8 if request.transport_mode == "public_transit" else 12)
        )
        chosen: dict[str, HotelOption] = {}
        used: set[str] = set()
        nearby_candidates: dict[str, HotelOption] = {}
        for name, day_groups in groups.items():

            def distances(hotel: HotelOption) -> list[float]:
                return [
                    min(distance_km(hotel.location, point.location) for point in points)
                    for points in day_groups
                ]

            def acceptable(hotel: HotelOption) -> bool:
                return hotel.name not in used and max(distances(hotel)) <= max_km

            options = [hotel for hotel in hotels.candidates if acceptable(hotel)]
            if not acceptable(originals[name]):
                # At most three real anchors per lodging block; never use a city-wide first hit.
                points = list(
                    {
                        point.name: point for group in day_groups for point in group
                    }.values()
                )
                anchor_points = points[:]
                points.sort(
                    key=lambda point: sum(
                        distance_km(point.location, other.location)
                        for other in anchor_points
                    )
                )
                for point in points[:3]:
                    if options:
                        break
                    try:
                        payload = self.backend.search_hotels(
                            city=request.city,
                            budget_min=request.budget_min,
                            budget_max=request.budget_max,
                            travelers=request.travelers,
                            stay_nights=request.stay_nights,
                            hotel_style=request.hotel_style,
                            limit=12,
                            area_hint=point.name,
                            anchor_location=point.location.model_dump(),
                            radius_m=max_km * 1000,
                        )
                        for raw in payload.get("candidates", []):
                            hotel = HotelOption.model_validate(raw)
                            if acceptable(hotel):
                                options.append(hotel)
                    except (ValueError, TypeError):
                        continue
            if not options:
                raise ValueError(
                    f"住宿「{name}」距离当天活动区域过远，且未找到可靠的附近酒店。请缩小游玩区域或调整住宿要求。"
                )
            replacement = (
                originals[name]
                if acceptable(originals[name])
                else min(
                    options,
                    key=lambda hotel: (
                        hotel.style != request.hotel_style,
                        sum(distances(hotel)),
                        hotel.nightly_price,
                    ),
                )
            )
            chosen[name] = replacement
            used.add(replacement.name)
            nearby_candidates.update({hotel.name: hotel for hotel in options})
            if replacement.name != name:
                skeleton.setdefault("notes", []).append(
                    f"原住宿「{name}」距离游玩区域较远，已调整为「{replacement.name}」，优先减少往返。"
                )
        for stay in stays:
            if stay.start_hotel:
                stay.start_hotel = chosen.get(stay.start_hotel.name, stay.start_hotel)
            if stay.end_hotel:
                stay.end_hotel = chosen.get(stay.end_hotel.name, stay.end_hotel)
                stay.night_area = stay.end_hotel.nearby_area
            stay.hotel_changed = bool(
                stay.start_hotel
                and stay.end_hotel
                and stay.start_hotel.name != stay.end_hotel.name
            )
        if stays:
            skeleton["recommended_hotel"] = stays[0].start_hotel or stays[0].end_hotel
            hotels.recommended_hotel = skeleton["recommended_hotel"]
            hotels.candidates = list(
                {
                    **nearby_candidates,
                    **{hotel.name: hotel for hotel in chosen.values()},
                }.values()
            )

    def _schedule_place_facts(
        self,
        plan: TripPlan,
        catalog: dict[str, Attraction],
        day_indices: set[int] | None,
    ) -> dict[str, PlaceDetails]:
        refs = []
        for day_index, day in enumerate(plan.daily_plans, 1):
            if day_indices is not None and day_index not in day_indices:
                continue
            for item_index, item in enumerate(day.items):
                if item.item_type != "attraction" and not (
                    item.item_type in {"meal", "food"} and item.is_route_stop
                ):
                    continue
                name = item.location_name or item.title
                point = catalog.get(name) if item.item_type == "attraction" else None
                location = point.location if point else item.location
                refs.append(
                    PlaceReference(
                        key=f"{day_index}:{item_index}",
                        name=name,
                        address=item.location_address
                        or (location.address if location else ""),
                        poi_id=point.candidate_id if point else "",
                        lat=location.lat if valid_location(location) else None,
                        lng=location.lng if valid_location(location) else None,
                    )
                )
        facts = {}
        for offset in range(0, len(refs), 40):
            response = self.place_service.resolve(
                PlaceLookupRequest(city=plan.city, places=refs[offset : offset + 40])
            )
            facts.update({detail.key: detail for detail in response.places})
        return facts

    def _check_plan_schedule(
        self,
        plan: TripPlan,
        request: TravelRequest,
        attractions: AttractionResearch,
        day_indices: set[int] | None = None,
    ) -> None:
        catalog = {item.name: item for item in attractions.selected_attractions}
        facts = self._schedule_place_facts(plan, catalog, day_indices)
        adjusted = []
        for index, day in enumerate(plan.daily_plans, 1):
            if day_indices is not None and index not in day_indices:
                continue
            hours = {
                item_index: parse_opening_hours(detail.opening_hours, day.date)
                for item_index in range(len(day.items))
                if (detail := facts.get(f"{index}:{item_index}")) is not None
            }
            if schedule_day(day, catalog, hours):
                adjusted.append(str(index))
            for item_index, item in enumerate(day.items):
                detail = facts.get(f"{index}:{item_index}")
                if detail:
                    item.opening_check = check_opening_visit(
                        detail.opening_hours,
                        day.date,
                        item.time_range,
                        source=detail.opening_hours_source,
                        checked_at=detail.opening_hours_checked_at,
                    )
        plan.notes = [
            note
            for note in plan.notes
            if not note.startswith("时间校验：")
            and not note.startswith("开放核对：")
            and not note.endswith(
                "天已按交通耗时重排时间，保留完整游玩和用餐时长，并预留到达缓冲。"
            )
        ]
        if adjusted:
            plan.notes.append(
                f"时间校验：第 {'、'.join(adjusted)} 天已结合交通耗时和可核实的开放时段重排时间，保留完整游玩和用餐时长，并预留到达缓冲。"
            )
        plan.notes.append(
            "开放核对：按高德常规开放规则核对出行日期与停留时段；缺失、跨夜、预约或复杂例外标为待确认，不代表实时营业或可预约。"
        )
        used_names = {
            item.location_name or item.title
            for day in plan.daily_plans
            for item in day.items
            if item.item_type == "attraction"
        }
        plan.selected_attractions = [
            item for name, item in catalog.items() if name in used_names
        ]
        checked_scope = "本次调整日期" if day_indices is not None else "每日安排"
        plan.overview = f"{request.city} {request.trip_days} 日行程，共安排 {len(plan.selected_attractions)} 处景点、{request.stay_nights} 晚住宿。已校验{checked_scope}的游玩、用餐与交通时间冲突，并核对可获取的常规开放时段；预约与临时调整请出发前确认。"
        rooms = math.ceil(request.travelers / 2)
        budget_note = f"住宿按 {request.stay_nights} 晚、每晚 {rooms} 间房估算（每间最多 2 人）；单日游默认含 1 晚。房价区间是按酒店类别预留的参考预算，不是实时报价。"
        plan.notes = [note for note in plan.notes if not note.startswith("住宿按 ")] + [
            budget_note
        ]
        meal_warning = "部分用餐尚未匹配到可靠餐厅，仅提供附近区域建议；确定具体店铺后需复核距离和时间。"
        plan.risk_alerts = [
            alert for alert in plan.risk_alerts if alert != meal_warning
        ]
        if any(
            item.item_type in {"meal", "food"} and not item.is_route_stop
            for day in plan.daily_plans
            for item in day.items
        ):
            plan.risk_alerts.append(meal_warning)

    def _inject_transport_details(
        self,
        plan: TripPlan,
        request: TravelRequest,
        attractions: AttractionResearch,
        hotels: HotelResearch,
        restaurant_catalog: dict[str, list[RestaurantOption]],
        daily_stays: list[DailyStayPlan],
        day_indices: set[int] | None = None,
    ) -> None:
        route_target_catalog = self._build_route_target_catalog(
            attractions, hotels, restaurant_catalog
        )
        for day in plan.daily_plans:
            for item in day.items:
                if valid_location(item.location):
                    route_target_catalog[item.location_name] = RouteTarget(
                        item.location_name,
                        item.location_address,
                        item.location.lat,
                        item.location.lng,
                    )
        for index, day in enumerate(plan.daily_plans):
            if day_indices is not None and index + 1 not in day_indices:
                continue
            day_stay = self._stay_for_day(daily_stays, index, request)
            non_transport_items = [
                item for item in day.items if item.item_type != "transport"
            ]
            non_transport_items = self._sort_day_items_by_time(non_transport_items)
            route_targets = self._collect_day_route_targets(
                non_transport_items, day_stay, route_target_catalog
            )
            route_segments = self._build_day_route_segments(request, route_targets)

            day.items = self._interleave_transport_items(
                non_transport_items, route_segments
            )
            day.total_transport_cost = round(
                sum(segment.estimated_cost for segment in route_segments), 2
            )
            day.total_transport_time_min = sum(
                segment.duration_min for segment in route_segments
            )
            day.route_summary = self._refresh_day_route_summary(day, request)

    def _sort_day_items_by_time(self, items: list[DayPlanItem]) -> list[DayPlanItem]:
        return sorted(items, key=lambda item: self._time_sort_key(item.time_range))

    def _interleave_transport_items(
        self,
        items: list[DayPlanItem],
        route_segments: list[RoutePlan],
    ) -> list[DayPlanItem]:
        transport_items = self._route_items_for_day(route_segments)
        if not transport_items:
            return items

        visible_route_origins = {
            item.location_name
            for item in items
            if self._is_route_stop_item(item)
        }
        output: list[DayPlanItem] = []
        route_index = 0
        for item in items:
            if self._is_route_stop_item(item):
                while (
                    route_index < len(transport_items)
                    and transport_items[route_index].to_location == item.location_name
                    and transport_items[route_index].from_location not in visible_route_origins
                ):
                    output.append(transport_items[route_index])
                    route_index += 1

            output.append(item)
            if self._is_route_stop_item(item):
                while (
                    route_index < len(transport_items)
                    and transport_items[route_index].from_location == item.location_name
                ):
                    output.append(transport_items[route_index])
                    route_index += 1

        output.extend(transport_items[route_index:])
        return output

    def _is_route_stop_item(self, item: DayPlanItem) -> bool:
        if item.item_type in {"attraction", "hotel"}:
            return True
        return item.item_type in {"food", "meal"} and item.is_route_stop

    def _collect_day_route_targets(
        self,
        items: list[DayPlanItem],
        stay: DailyStayPlan,
        route_target_catalog: dict[str, RouteTarget],
    ) -> list[RouteTarget]:
        ordered_targets: list[RouteTarget] = []

        def push_target(name: str, address: str = "") -> None:
            normalized = name.strip()
            if not normalized:
                return
            if ordered_targets and ordered_targets[-1].name == normalized:
                return
            ordered_targets.append(route_target_catalog.get(normalized, RouteTarget(normalized, address=address)))

        if stay.start_hotel is not None:
            push_target(stay.start_hotel.name, stay.start_hotel.location.address)

        for item in items:
            if item.item_type in {"attraction", "hotel"}:
                push_target(item.location_name, item.location_address)
            elif item.item_type in {"food", "meal"} and item.is_route_stop:
                push_target(item.location_name, item.location_address)

        if stay.charged_night and stay.end_hotel is not None:
            push_target(stay.end_hotel.name, stay.end_hotel.location.address)

        return ordered_targets

    def _build_day_route_segments(
        self,
        request: TravelRequest,
        route_targets: list[RouteTarget],
    ) -> list[RoutePlan]:
        route_segments: list[RoutePlan] = []
        for idx in range(len(route_targets) - 1):
            origin = route_targets[idx]
            destination = route_targets[idx + 1]
            if origin.name == destination.name:
                continue
            route_segments.append(
                self._build_route_plan(
                    request.city,
                    origin.name,
                    destination.name,
                    request.transport_mode,
                    origin=origin,
                    destination=destination,
                )
            )
        return route_segments

    def _stay_for_day(
        self,
        daily_stays: list[DailyStayPlan],
        index: int,
        request: TravelRequest | None = None,
    ) -> DailyStayPlan:
        if daily_stays:
            return daily_stays[min(index, len(daily_stays) - 1)]
        base_date = request.start_date if request is not None else self._date_for_index(0)
        return DailyStayPlan(day_index=index + 1, date=base_date + timedelta(days=index))

    def _date_for_index(self, index: int):
        from datetime import date

        return date.today() + timedelta(days=index)

    def _calculate_hotel_cost(self, daily_stays: list[DailyStayPlan], request: TravelRequest) -> float:
        charged = [
            stay
            for stay in daily_stays
            if stay.charged_night and stay.end_hotel is not None
        ]
        if len(charged) != request.stay_nights:
            raise ValueError("住宿晚数与旅行日期不一致，请重新规划住宿后再计算费用。")
        return round(
            sum(
                stay.end_hotel.nightly_price
                for stay in charged
                if stay.end_hotel is not None
            )
            * math.ceil(request.travelers / 2),
            2,
        )

    def _refresh_plan_budget(
        self,
        plan: TripPlan,
        request: TravelRequest,
        daily_stays: list[DailyStayPlan],
    ) -> None:
        hotel_cost = self._calculate_hotel_cost(daily_stays, request)
        attraction_cost = sum(
            item.estimated_cost
            for day in plan.daily_plans
            for item in day.items
            if item.item_type == "attraction"
        )
        food_cost = sum(
            item.estimated_cost
            for day in plan.daily_plans
            for item in day.items
            if item.item_type in {"food", "meal"}
        )
        transport_cost = sum(day.total_transport_cost for day in plan.daily_plans)
        contingency = round((hotel_cost + attraction_cost + food_cost + transport_cost) * 0.08, 2)
        plan.budget = BudgetBreakdown(
            hotel=round(hotel_cost, 2),
            attractions=round(attraction_cost, 2),
            food=round(food_cost, 2),
            transport=round(transport_cost, 2),
            contingency=contingency,
            total=round(hotel_cost + attraction_cost + food_cost + transport_cost + contingency, 2),
        )

    def _build_route_target_catalog(
        self,
        attractions: AttractionResearch,
        hotels: HotelResearch,
        restaurant_catalog: dict[str, list[RestaurantOption]],
    ) -> dict[str, RouteTarget]:
        catalog: dict[str, RouteTarget] = {}

        def add(name: str, location: Any) -> None:
            cleaned = self._sanitize_place_name(name)
            if not cleaned:
                return
            try:
                lat = float(getattr(location, "lat", 0.0) or 0.0)
                lng = float(getattr(location, "lng", 0.0) or 0.0)
            except Exception:
                return
            if lat == 0.0 and lng == 0.0:
                return
            address = str(getattr(location, "address", "") or "").strip()
            target = RouteTarget(name.strip(), address, lat, lng)
            catalog[name.strip()] = target
            catalog[cleaned] = target

        for attraction in attractions.selected_attractions:
            add(attraction.name, attraction.location)
            add(attraction.location.name, attraction.location)
        for hotel_option in hotels.candidates:
            add(hotel_option.name, hotel_option.location)
            add(hotel_option.location.name, hotel_option.location)
        if hotels.recommended_hotel is not None:
            add(hotels.recommended_hotel.name, hotels.recommended_hotel.location)
            add(hotels.recommended_hotel.location.name, hotels.recommended_hotel.location)
        for options in restaurant_catalog.values():
            for restaurant in options:
                add(restaurant.name, restaurant.location)
                add(restaurant.location.name, restaurant.location)
        return catalog

    def _route_target_for(self, name: str, catalog: dict[str, RouteTarget]) -> RouteTarget:
        cleaned = self._sanitize_place_name(name)
        return catalog.get(cleaned) or catalog.get(name) or RouteTarget(name)

    def _refresh_day_route_summary(self, day: DayPlan, request: TravelRequest) -> str:
        attraction_titles = [item.title for item in day.items if item.item_type == "attraction"]
        route_summary = "、".join(attraction_titles) or "当天安排灵活探索"
        if day.total_transport_time_min > 0:
            return (
                f"{route_summary} | "
                f"{self._transport_mode_label(request.transport_mode)}合计约{day.total_transport_time_min}分钟"
            )
        return route_summary

    def _ground_attraction_research(
        self,
        candidate: AttractionResearch,
        fallback: AttractionResearch,
    ) -> AttractionResearch | None:
        if not candidate.selected_attractions:
            return None

        catalog = {item.name: item for item in fallback.selected_attractions}
        grounded: list[Attraction] = []
        for item in candidate.selected_attractions:
            resolved_name = self._resolve_place_name(item.name, catalog)
            if not resolved_name:
                continue
            canonical = catalog[resolved_name]
            if canonical.name not in {existing.name for existing in grounded}:
                grounded.append(canonical)

        if not grounded:
            return None

        recommended_night_area = (
            self._resolve_place_name(candidate.recommended_night_area, [fallback.recommended_night_area])
            or fallback.recommended_night_area
        )
        return AttractionResearch(
            city_overview=candidate.city_overview or fallback.city_overview,
            selected_attractions=grounded,
            selection_reasoning=candidate.selection_reasoning or fallback.selection_reasoning,
            recommended_night_area=recommended_night_area,
            search_plan=candidate.search_plan or fallback.search_plan,
            preference_interpretation=candidate.preference_interpretation or fallback.preference_interpretation,
        )

    def _safe_pool_key(self, value: Any) -> str:
        return str(value or "").strip()

    def _collect_region_candidates(
        self,
        attractions: AttractionResearch,
        hotels: HotelResearch,
        daily_stays: list[DailyStayPlan] | None = None,
    ) -> list[str]:
        candidates: list[str] = []

        def add(value: str) -> None:
            cleaned = self._sanitize_place_name(value)
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)

        add(attractions.recommended_night_area)
        if hotels.recommended_hotel is not None:
            add(hotels.recommended_hotel.nearby_area)
        for hotel in hotels.candidates:
            add(hotel.nearby_area)
        for stay in daily_stays or []:
            add(stay.night_area)
            if stay.start_hotel is not None:
                add(stay.start_hotel.nearby_area)
            if stay.end_hotel is not None:
                add(stay.end_hotel.nearby_area)
        return candidates

    def _attractions_per_day(self, request: TravelRequest) -> int:
        return {
            "relaxed": 1,
            "balanced": 2,
            "intense": 3,
        }.get(request.pace, 2)

    def _repair_attraction_tool_budget(self, target_count: int, current_count: int) -> int:
        shortage = max(target_count - current_count, 0)
        if shortage <= 0:
            return 0
        return min(max(math.ceil(shortage / 2), 2), 8)

    def _target_attraction_count(self, request: TravelRequest) -> int:
        return max(request.trip_days * self._attractions_per_day(request), 3)

    def _expected_daily_attraction_counts(self, request: TravelRequest, available_count: int) -> list[int]:
        if request.trip_days <= 0:
            return []
        per_day = self._attractions_per_day(request)
        total = min(max(available_count, 0), request.trip_days * per_day)
        counts = [0 for _ in range(request.trip_days)]
        for index in range(total):
            counts[index % request.trip_days] += 1
        return counts

    def _resolve_route_anchor(
        self,
        raw_name: str,
        *,
        attraction_catalog: dict[str, Attraction],
        hotel_catalog: dict[str, HotelOption],
        restaurant_catalog: dict[str, RestaurantOption],
        region_candidates: list[str],
    ) -> str | None:
        direct = self._resolve_place_name(raw_name, attraction_catalog)
        if direct:
            return direct

        direct = self._resolve_place_name(raw_name, hotel_catalog)
        if direct:
            return direct

        direct = self._resolve_place_name(raw_name, restaurant_catalog)
        if direct:
            return direct

        return self._resolve_place_name(raw_name, region_candidates)

    def _resolve_location_address(
        self,
        raw_name: str,
        *,
        attraction_catalog: dict[str, Attraction],
        hotel_catalog: dict[str, HotelOption],
        restaurant_catalog: dict[str, RestaurantOption],
    ) -> str:
        resolved_attraction = self._resolve_place_name(raw_name, attraction_catalog)
        if resolved_attraction:
            return attraction_catalog[resolved_attraction].location.address

        resolved_hotel = self._resolve_place_name(raw_name, hotel_catalog)
        if resolved_hotel:
            return hotel_catalog[resolved_hotel].location.address

        resolved_restaurant = self._resolve_place_name(raw_name, restaurant_catalog)
        if resolved_restaurant:
            return restaurant_catalog[resolved_restaurant].location.address

        return ""

    def _resolve_place_name(
        self,
        raw_name: str,
        candidates: Iterable[str] | dict[str, object],
    ) -> str | None:
        text = self._sanitize_place_name(raw_name)
        if not text:
            return None

        options = list(candidates.keys()) if isinstance(candidates, dict) else list(candidates)
        if not options:
            return None
        if text in options:
            return text

        scored: list[tuple[int, str]] = []
        for option in options:
            normalized_option = self._sanitize_place_name(option)
            if not normalized_option:
                continue
            if normalized_option == text:
                return option
            if normalized_option.endswith(text) or text.endswith(normalized_option):
                scored.append((3, option))
            elif text in normalized_option or normalized_option in text:
                scored.append((2, option))
            elif self._core_place_token(normalized_option) == self._core_place_token(text):
                scored.append((1, option))

        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], len(item[1])))
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1]

    def _sanitize_place_name(self, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            return ""
        for marker in ("??", "锛?", "?锛?", "锟?"):
            cleaned = cleaned.replace(marker, "")
        cleaned = cleaned.replace(" / ", " ").replace("/", " ")
        cleaned = cleaned.strip(" -,.，。;；:：|[]【】()（）")
        return " ".join(cleaned.split()).strip()

    def _core_place_token(self, value: str) -> str:
        text = self._sanitize_place_name(value)
        if not text:
            return ""
        for sep in (" ", "路", "-", "·", "("):
            if sep in text:
                text = text.split(sep, 1)[0]
        return text[-6:]

    def _normalize_city_input(self, city: str) -> str | None:
        raw = (city or "").strip()
        if not raw:
            return None
        if "锟?" in raw:
            return None
        if "?" in raw and not any(ch.isalpha() or ("\u4e00" <= ch <= "\u9fff") for ch in raw):
            return None

        cleaned = self._sanitize_place_name(raw)
        if not cleaned:
            return None
        return cleaned

    def _build_route_plan(
        self,
        city: str,
        origin_name: str,
        destination_name: str,
        mode: str,
        *,
        origin: RouteTarget | None = None,
        destination: RouteTarget | None = None,
    ) -> RoutePlan:
        effective_mode = "public_transit" if mode == "mixed" else mode
        transport = self.backend.estimate_local_transport(
            city,
            origin_name,
            destination_name,
            effective_mode,
            self._effective_transit_preference(effective_mode),
            origin_location=origin.__dict__ if origin else None,
            destination_location=destination.__dict__ if destination else None,
        )
        if self._is_route_outlier(transport):
            existing_reason = str(transport.get("fallback_reason", "") or "")
            outlier_reason = self._route_outlier_reason(transport)
            transport["fallback_reason"] = ", ".join(
                part for part in [existing_reason, outlier_reason] if part
            )
            if str(transport.get("data_mode")) == "live":
                transport["data_mode"] = "live_outlier"
            else:
                transport["data_mode"] = str(transport.get("data_mode") or "fallback")

        detail_segments = [
            RouteDetailSegment.model_validate(item)
            for item in (transport.get("route_segments") or [])
        ]
        return RoutePlan(
            city=city,
            mode=effective_mode,  # type: ignore[arg-type]
            origin=origin_name,
            destination=destination_name,
            origin_address=origin.address if origin else "",
            destination_address=destination.address if destination else "",
            distance_km=float(transport.get("distance_km", 0.0) or 0.0),
            duration_min=int(transport.get("duration_min", 0) or 0),
            estimated_cost=float(transport.get("estimated_cost", transport.get("estimated_taxi_cost", 0.0)) or 0.0),
            transfers=int(transport.get("transfers", 0) or 0),
            walk_distance_m=int(transport.get("walk_distance_m", 0) or 0),
            tolls=float(transport.get("tolls", 0.0) or 0.0),
            data_mode=str(transport.get("data_mode", "fallback")),
            fallback_reason=str(transport.get("fallback_reason", "")),
            summary=str(transport.get("summary", "")),
            strategy_label=str(transport.get("strategy_label", "")),
            traffic_status=str(transport.get("traffic_status", "")),
            route_alternatives=list(transport.get("route_alternatives") or []),
            route_segments=detail_segments,
        )

    def _is_route_outlier(self, transport: dict[str, Any]) -> bool:
        distance_km = float(transport.get("distance_km", 0.0) or 0.0)
        duration_min = int(transport.get("duration_min", 0) or 0)
        return distance_km >= 80 or duration_min >= 240

    def _route_outlier_reason(self, transport: dict[str, Any]) -> str:
        distance_km = float(transport.get("distance_km", 0.0) or 0.0)
        duration_min = int(transport.get("duration_min", 0) or 0)
        reasons: list[str] = []
        if distance_km >= 80:
            reasons.append(f"route_distance_outlier:{distance_km:.1f}km")
        if duration_min >= 240:
            reasons.append(f"route_duration_outlier:{duration_min}min")
        return ",".join(reasons)

    def _route_items_for_day(self, segments: list[RoutePlan]) -> list[DayPlanItem]:
        items: list[DayPlanItem] = []
        for index, segment in enumerate(segments, start=1):
            detail = (
                f"{self._transport_mode_label(segment.mode)}约{segment.duration_min}分钟，"
                f"{segment.distance_km:.1f}公里，约 {segment.estimated_cost:.0f} 元"
            )
            if segment.mode == "public_transit":
                detail += f"，换乘 {segment.transfers} 次，步行 {segment.walk_distance_m} 米"
            if segment.mode == "self_drive" and segment.tolls > 0:
                detail += f"，过路费约 {segment.tolls:.0f} 元"
            if segment.traffic_status:
                detail += f"，{segment.traffic_status}"

            items.append(
                DayPlanItem(
                    time_range=f"交通 {index}",
                    title=f"{segment.origin} -> {segment.destination}",
                    item_type="transport",
                    location_name=segment.destination,
                    location_address=segment.destination_address,
                    summary=detail,
                    estimated_cost=segment.estimated_cost,
                    reason=segment.summary or "路线由交通工具链估算生成。",
                    transport_mode=self._transport_mode_label(segment.mode),
                    from_location=segment.origin,
                    to_location=segment.destination,
                    duration_min=segment.duration_min,
                    distance_km=segment.distance_km,
                    transfers=segment.transfers,
                    route_segments=segment.route_segments,
                    route_strategy=segment.strategy_label,
                    route_data_mode=segment.data_mode,
                    route_fallback_reason=segment.fallback_reason,
                    route_alternatives=segment.route_alternatives,
                    expandable=bool(segment.route_segments),
                )
            )
        return items

    def _transport_mode_label(self, mode: str) -> str:
        return {
            "public_transit": "公共交通",
            "self_drive": "自驾",
            "taxi": "打车",
            "mixed": "混合出行",
            "walk": "步行",
        }.get(mode, mode)

    def _effective_transit_preference(self, mode: str) -> str:
        if mode != "public_transit":
            return "recommended"
        return self._request_transit_preference

    def _transport_note(self, request: TravelRequest) -> str:
        if request.transport_mode != "public_transit":
            return "当前版本仅对公共交通提供细分偏好映射。"
        label = {
            "recommended": "推荐",
            "less_walking": "步行少",
            "subway_priority": "地铁优先",
            "bus_priority": "公交优先",
        }.get(request.transit_preference, request.transit_preference)
        return f"公共交通偏好：{label}。"

    def _format_attraction_agent_diagnostic(self, diagnostics: dict[str, Any]) -> str:
        reason = str(diagnostics.get("failure_reason") or "unknown").strip()
        readable = {
            "llm_output_json_parse_failed": "景点 Agent 没有返回可解析 JSON",
            "attraction_research_schema_validation_failed": "景点 Agent 返回的 JSON 不符合景点研究结构",
            "selected_attractions_not_list": "景点 Agent 返回的 selected_attractions 不是列表",
            "agent_selected_no_attractions": "景点 Agent 没有选出景点",
            "mcp_candidate_pool_empty": "景点 Agent 本轮没有留下 MCP 候选池",
            "no_agent_attractions_matched_candidate_pool": "景点 Agent 选择的景点没有匹配到本轮 MCP 候选池",
            "agent_grounding_failed": "景点 Agent 输出未通过候选池验真",
            "attraction_agent_postprocess_exception": "景点 Agent 后处理发生异常",
            "attraction_agent_unavailable_or_unverified": "景点 Agent 不可用或结果未通过验真",
        }.get(reason, reason or "未知原因")
        parts = [f"景点 Agent 诊断：{readable}。"]
        if diagnostics.get("raw_output_length") is not None:
            parts.append(f"原始输出长度 {diagnostics.get('raw_output_length')}。")
        if diagnostics.get("raw_selected_count") is not None:
            parts.append(f"模型原始选择 {diagnostics.get('raw_selected_count')} 个景点。")
        if diagnostics.get("verified_selected_count") is not None:
            parts.append(f"字段验真后剩余 {diagnostics.get('verified_selected_count')} 个。")
        if diagnostics.get("candidate_pool_size") is not None:
            parts.append(f"MCP 候选池 {diagnostics.get('candidate_pool_size')} 个。")
        if diagnostics.get("grounded_selected_count") is not None:
            parts.append(f"候选池匹配成功 {diagnostics.get('grounded_selected_count')} 个。")
        return "".join(parts)

    def _pace_label(self, pace: str) -> str:
        return {"relaxed": "慢游", "balanced": "平衡", "intense": "紧凑"}.get(pace, pace)

    def _hotel_style_label(self, hotel_style: str) -> str:
        return {"budget": "经济", "comfort": "舒适", "premium": "高档"}.get(hotel_style, hotel_style)

    def _human_labels(self, labels: list[str]) -> str:
        return "、".join(label for label in labels if label)

    def _packing_tips(self, weather: WeatherResearch) -> list[str]:
        tips = ["舒适步行鞋", "充电宝", "身份证件与订单截图"]
        conditions = {item.condition for item in weather.forecast}
        if any("rain" in item or "雨" in item or "雪" in item for item in conditions):
            tips.append("折叠伞")
        if "hot" in conditions:
            tips.append("防晒用品和水杯")
        return tips

    def _source_from_attraction_research(self, research: AttractionResearch) -> str:
        joined = " ".join(research.selection_reasoning).lower()
        if "live amap" in joined or "高德实时" in joined:
            return "live_amap"
        if research.selected_attractions:
            return "agent_generated"
        return "fallback"

    def _source_from_weather_research(self, research: WeatherResearch) -> str:
        if research.data_source:
            return research.data_source
        summary = research.overall_summary.lower()
        if "qweather" in summary or "和风实时" in summary:
            return "live_qweather"
        if research.forecast:
            return "agent_generated"
        return "fallback"

    def _source_from_hotel_research(self, research: HotelResearch) -> str:
        if (
            research.recommended_hotel
            and research.recommended_hotel.price_source == "estimated_from_poi"
        ):
            return "live_poi_estimated_price"
        if research.recommended_hotel and research.recommended_hotel.price_source != "estimated":
            return research.recommended_hotel.price_source
        if research.candidates:
            return "agent_generated"
        return "fallback"

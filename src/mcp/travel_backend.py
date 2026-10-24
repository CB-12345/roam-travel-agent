from __future__ import annotations

import math
import time
import threading
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

import requests

from src.models import RouteDetailSegment
from src.mcp.qweather import QWeatherClient
from src.services.observability import observed_call


CITY_DATA: dict[str, dict[str, Any]] = {
    "hangzhou": {
        "aliases": ["hangzhou", "杭州"],
        "profile": "杭州适合喜欢湖景、茶文化、慢节奏步行和城市自然结合的旅行者。",
        "night_area": "湖滨商圈",
        "attractions": [
            {"name": "西湖", "category": "nature", "tags": ["nature", "humanity"], "hours": 3.0, "ticket": 0, "best_time": "morning", "summary": "经典湖滨路线，适合慢走和拍照。", "lat": 30.2431, "lng": 120.1500},
            {"name": "灵隐寺", "category": "humanity", "tags": ["humanity"], "hours": 2.5, "ticket": 75, "best_time": "morning", "summary": "文化浓度高，适合人文向行程。", "lat": 30.2407, "lng": 120.1016},
            {"name": "中国茶叶博物馆", "category": "art", "tags": ["art", "humanity"], "hours": 2.0, "ticket": 0, "best_time": "afternoon", "summary": "安静的室内文化体验。", "lat": 30.2052, "lng": 120.1163},
            {"name": "南宋御街", "category": "food", "tags": ["food", "shopping", "nightlife"], "hours": 2.0, "ticket": 0, "best_time": "evening", "summary": "适合晚上边吃边逛。", "lat": 30.2480, "lng": 120.1710},
            {"name": "西溪湿地", "category": "nature", "tags": ["nature"], "hours": 3.0, "ticket": 80, "best_time": "afternoon", "summary": "适合轻松的户外半日游。", "lat": 30.2720, "lng": 120.0630},
        ],
        "hotels": [
            {"name": "西湖轻居酒店", "style": "budget", "stars": 3, "nightly": 320, "summary": "靠近湖滨，性价比较高。", "area": "湖滨商圈", "lat": 30.2520, "lng": 120.1602},
            {"name": "茶港精品酒店", "style": "comfort", "stars": 4, "nightly": 540, "summary": "在西湖与茶文化路线之间，舒适度较好。", "area": "西湖景区周边", "lat": 30.2330, "lng": 120.1320},
            {"name": "杭州湖景宫酒店", "style": "premium", "stars": 5, "nightly": 920, "summary": "高档住宿，适合作为城市观光据点。", "area": "湖滨商圈", "lat": 30.2502, "lng": 120.1591},
        ],
    },
    "chengdu": {
        "aliases": ["chengdu", "成都"],
        "profile": "成都适合美食爱好者、熊猫爱好者和偏慢节奏的城市探索。",
        "night_area": "太古里",
        "attractions": [
            {"name": "成都大熊猫繁育研究基地", "category": "family", "tags": ["family", "nature"], "hours": 3.0, "ticket": 55, "best_time": "morning", "summary": "适合亲子和动物主题路线。", "lat": 30.7335, "lng": 104.1490},
            {"name": "武侯祠", "category": "humanity", "tags": ["humanity"], "hours": 2.0, "ticket": 50, "best_time": "afternoon", "summary": "适合历史文化主题。", "lat": 30.6460, "lng": 104.0430},
            {"name": "锦里古街", "category": "food", "tags": ["food", "shopping", "nightlife", "humanity"], "hours": 2.0, "ticket": 0, "best_time": "evening", "summary": "适合晚上吃喝和感受老街氛围。", "lat": 30.6468, "lng": 104.0427},
            {"name": "成都博物馆", "category": "art", "tags": ["art", "humanity"], "hours": 2.0, "ticket": 0, "best_time": "afternoon", "summary": "适合室内博物馆路线。", "lat": 30.6571, "lng": 104.0656},
            {"name": "人民公园", "category": "nature", "tags": ["nature", "food"], "hours": 2.0, "ticket": 0, "best_time": "afternoon", "summary": "适合茶馆和公园慢游。", "lat": 30.6570, "lng": 104.0496},
        ],
        "hotels": [
            {"name": "春熙轻住酒店", "style": "budget", "stars": 3, "nightly": 280, "summary": "地铁方便，适合预算型出行。", "area": "春熙路", "lat": 30.6578, "lng": 104.0800},
            {"name": "太古里舒适酒店", "style": "comfort", "stars": 4, "nightly": 460, "summary": "靠近美食、购物和夜生活区。", "area": "太古里", "lat": 30.6548, "lng": 104.0825},
            {"name": "合江亭江景酒店", "style": "premium", "stars": 5, "nightly": 860, "summary": "适合对舒适度要求较高的行程。", "area": "合江亭", "lat": 30.6482, "lng": 104.0886},
        ],
    },
    "beijing": {
        "aliases": ["beijing", "北京"],
        "profile": "北京适合喜欢皇城古迹、博物馆和高密度人文路线的旅行者。",
        "night_area": "王府井",
        "attractions": [
            {"name": "故宫博物院", "category": "humanity", "tags": ["humanity", "art"], "hours": 4.0, "ticket": 60, "best_time": "morning", "summary": "经典核心景点，适合安排半天。", "lat": 39.9163, "lng": 116.3972},
            {"name": "天坛公园", "category": "humanity", "tags": ["humanity", "nature"], "hours": 2.5, "ticket": 34, "best_time": "afternoon", "summary": "适合人文和园林结合路线。", "lat": 39.8822, "lng": 116.4065},
            {"name": "中国国家博物馆", "category": "art", "tags": ["art", "humanity"], "hours": 3.0, "ticket": 0, "best_time": "afternoon", "summary": "适合历史展陈和博物馆深度游。", "lat": 39.9051, "lng": 116.4010},
            {"name": "什刹海", "category": "nightlife", "tags": ["food", "nightlife", "humanity"], "hours": 2.5, "ticket": 0, "best_time": "evening", "summary": "适合夜景、美食和胡同漫游。", "lat": 39.9415, "lng": 116.3870},
            {"name": "颐和园", "category": "nature", "tags": ["nature", "humanity"], "hours": 3.5, "ticket": 30, "best_time": "morning", "summary": "适合舒缓型大景区路线。", "lat": 39.9996, "lng": 116.2755},
        ],
        "hotels": [
            {"name": "王府井智选酒店", "style": "budget", "stars": 3, "nightly": 360, "summary": "适合核心城区观光。", "area": "王府井", "lat": 39.9138, "lng": 116.4125},
            {"name": "中轴舒适酒店", "style": "comfort", "stars": 4, "nightly": 620, "summary": "去核心人文景点更方便。", "area": "东城区", "lat": 39.9188, "lng": 116.4102},
            {"name": "京城礼宾宫酒店", "style": "premium", "stars": 5, "nightly": 1080, "summary": "适合作为高档城市旅行据点。", "area": "王府井", "lat": 39.9145, "lng": 116.4140},
        ],
    },
    "guangzhou": {
        "aliases": ["guangzhou", "广州"],
        "profile": "广州适合美食、人文、博物馆和城市漫步结合的路线。",
        "night_area": "北京路",
        "attractions": [
            {"name": "南越王博物院", "category": "humanity", "tags": ["humanity", "art"], "hours": 2.0, "ticket": 0, "best_time": "morning", "summary": "适合历史文化开场。", "lat": 23.1312, "lng": 113.2644},
            {"name": "广东省博物馆", "category": "art", "tags": ["art", "humanity"], "hours": 2.5, "ticket": 0, "best_time": "afternoon", "summary": "适合室内博物馆路线。", "lat": 23.1196, "lng": 113.3304},
            {"name": "沙面", "category": "humanity", "tags": ["humanity", "nature"], "hours": 2.0, "ticket": 0, "best_time": "afternoon", "summary": "适合 city walk 和建筑拍照。", "lat": 23.1086, "lng": 113.2381},
            {"name": "永庆坊", "category": "food", "tags": ["food", "shopping", "humanity"], "hours": 2.0, "ticket": 0, "best_time": "evening", "summary": "适合晚间美食和街区漫游。", "lat": 23.1189, "lng": 113.2445},
            {"name": "广州塔", "category": "nightlife", "tags": ["nightlife", "shopping"], "hours": 2.5, "ticket": 150, "best_time": "evening", "summary": "适合夜景和城市观景。", "lat": 23.1085, "lng": 113.3192},
        ],
        "hotels": [
            {"name": "北京路轻住酒店", "style": "budget", "stars": 3, "nightly": 320, "summary": "适合人文与美食路线。", "area": "北京路", "lat": 23.1258, "lng": 113.2706},
            {"name": "珠江新城舒适酒店", "style": "comfort", "stars": 4, "nightly": 520, "summary": "适合博物馆和城市夜景路线。", "area": "珠江新城", "lat": 23.1230, "lng": 113.3242},
            {"name": "广州塔景观酒店", "style": "premium", "stars": 5, "nightly": 980, "summary": "适合作为高档夜景型行程据点。", "area": "广州塔周边", "lat": 23.1102, "lng": 113.3214},
        ],
    },
}


PREFERENCE_LABELS = {
    "humanity": "人文与历史",
    "art": "艺术与展览",
    "nature": "自然与户外",
    "food": "美食探索",
    "shopping": "购物与城市漫步",
    "nightlife": "夜生活与夜景",
    "family": "亲子活动",
}


class TravelDataBackend:
    def __init__(self, amap_key: str = "", qweather_key: str = "", qweather_host: str = ""):
        self.amap_key = amap_key.strip()
        self.qweather_key = qweather_key.strip()
        self.qweather_host = qweather_host.strip()
        self._weather_cache: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}
        self._amap_cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self._amap_pacing_lock = threading.Lock()
        self._last_amap_request_at = 0.0
        self._amap_min_interval_s = 0.45
        self._amap_retry_delay_s = 1.5
        self._last_amap_error = ""
        self._last_poi_geocode_error = ""
        self._transport_cache: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def _safe_text(self, value: Any, default: str = "") -> str:
        if value is None:
            return default
        if isinstance(value, str):
            text = value.strip()
            return text or default
        if isinstance(value, (list, tuple, set)):
            parts = [str(item).strip() for item in value if str(item).strip()]
            return " ".join(parts) or default
        text = str(value).strip()
        return text or default

    def _amap_cache_key(self, url: str, params: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
        normalized = tuple(
            sorted(
                (key, self._safe_text(value))
                for key, value in params.items()
                if key != "key"
            )
        )
        return (url, normalized)

    def _wait_for_amap_slot(self) -> None:
        # Serialize only pacing; independent network requests may still overlap.
        with self._amap_pacing_lock:
            wait_s = self._amap_min_interval_s - (time.monotonic() - self._last_amap_request_at)
            if wait_s > 0:
                time.sleep(wait_s)
            self._last_amap_request_at = time.monotonic()

    def _amap_get(
        self,
        url: str,
        params: dict[str, Any],
        *,
        timeout: int = 12,
        retries: int = 1,
        use_cache: bool = True,  # 使用缓存
    ) -> dict[str, Any] | None:
        if not self.amap_key:
            return None

        request_params = dict(params)
        # 拼接key
        request_params["key"] = self.amap_key
        # 缓存
        cache_key = self._amap_cache_key(url, request_params)
        if use_cache and cache_key in self._amap_cache:
            # 直接返回缓存，不用再申请了
            return self._amap_cache[cache_key]

        for attempt in range(retries + 1):
            # 等待一下
            self._wait_for_amap_slot()
            try:
                # 向高德地图发送网络请求，拿服务器的数据
                response = observed_call("http", "amap:" + urlsplit(url).path, lambda: requests.get(url, params=request_params, timeout=timeout))
                response.raise_for_status()  # 输出200就成功
                payload = response.json()
            except Exception as exc:
                self._last_amap_error = f"request_exception:{type(exc).__name__}"
                # 如果请求小于要求的次数，就循环
                if attempt < retries:
                    time.sleep(self._amap_retry_delay_s * (attempt + 1))
                    continue
                return None

            infocode = self._safe_text(payload.get("infocode"))  # 状态码
            info = self._safe_text(payload.get("info"))  # 状态文字
            # 失败了，且请求次数小于要求次数，就再次循环
            if infocode == "10021" and attempt < retries:
                self._last_amap_error = f"amap_error:{infocode}:{info}"
                time.sleep(self._amap_retry_delay_s * (attempt + 1))
                continue

            if str(payload.get("status")) != "1":
                self._last_amap_error = f"amap_error:{infocode}:{info}"
                return payload

            self._last_amap_error = ""
            if use_cache:
                # 返回结果存进缓存
                self._amap_cache[cache_key] = payload
            return payload

        return None

    def resolve_city(self, city: str) -> dict[str, Any] | None:
        city_key = city.strip().lower()
        for data in CITY_DATA.values():
            if city_key in [alias.lower() for alias in data["aliases"]]:
                return data
        return None

    def is_supported_city(self, city: str) -> bool:
        if self.resolve_city(city) is not None:
            return True
        return self.geocode_city(city) is not None

    def get_supported_city_context(self, city: str) -> dict[str, Any]:
        resolved = self.resolve_city(city)
        if resolved is not None:
            return resolved
        raise ValueError(f"暂不支持城市“{city}”的自动规划：它既不在内置城市列表中，也无法通过地图服务识别。")

    def get_city_profile(self, city: str) -> dict[str, Any]:
        '''返回城市的画像'''
        data = self.resolve_city(city)
        if data is None:
            geo = self.geocode_city(city)
            if geo is None:
                raise ValueError(f"暂不支持城市“{city}”的自动规划：它既不在内置城市列表中，也无法通过地图服务识别。")
            return {
                "city": city,
                "profile": f"{geo.get('formatted_address', city)} 目前已通过地图服务识别，可基于实时景点、酒店和路线数据生成行程。",
                "night_area": geo.get("formatted_address", city),
                "available_preferences": list(PREFERENCE_LABELS.keys()),
                "data_mode": "live",
            }
        return {
            "city": city,
            "profile": data["profile"],
            "night_area": data["night_area"],
            "available_preferences": list(PREFERENCE_LABELS.keys()),
            "data_mode": "live" if self.amap_key else "fallback",
        }

    def search_attractions(
        self,
        city: str,
        preferences: str = "",
        travelers: int = 1,
        daily_budget: float = 500.0,
        target_count: int = 6,
        **kwargs: Any,
    ) -> dict[str, Any]:
        '''搜索景点'''
        target_count = max(1, min(int(target_count or 6), 24))
        if "budget" in kwargs and not daily_budget:
            try:
                daily_budget = float(kwargs["budget"])
            except Exception:
                pass
        if self.amap_key:
            live = self._search_attractions_live(city, preferences, travelers, daily_budget, target_count)
            if live:
                return live

        data = self.resolve_city(city)
        if data is None:
            raise ValueError(f"城市“{city}”暂无可用景点数据：未命中内置城市，且实时地图景点检索失败。")
        preference_list = [item.strip() for item in preferences.split(",") if item.strip()]
        scored = []
        for attraction in data["attractions"]:
            score = 1
            for pref in preference_list:
                if pref in attraction["tags"]:
                    score += 3
            if attraction["ticket"] > daily_budget * 0.45:
                score -= 2
            scored.append((score, attraction))

        scored.sort(key=lambda item: (-item[0], item[1]["ticket"]))
        selected = [self._serialize_attraction(item[1]) for item in scored[:target_count]]
        return {
            "city": city,
            "preferences": preference_list,
            "travelers": travelers,
            "attractions": selected,
            "data_mode": "fallback",
        }

    def search_attraction_pois(
        self,
        city: str,
        query: str,
        limit: int = 6,
    ) -> dict[str, Any]:
        """Search raw attraction POI candidates by an agent-chosen query."""
        query = self._safe_text(query)
        limit = max(1, min(int(limit or 6), 12))
        if not query:
            return {
                "city": city,
                "query": query,
                "candidates": [],
                "data_mode": "empty_query",
                "error": "query is empty",
            }

        geo = self.geocode_city(city)
        if self.amap_key and geo:
            try:
                payload = self._amap_get(
                    "https://restapi.amap.com/v3/place/text",
                    {
                        "keywords": query,
                        "city": geo["adcode"] or city,
                        "citylimit": "true",
                        "offset": limit,
                        "page": 1,
                    },
                )
            except Exception:
                payload = None

            if payload is not None and str(payload.get("status")) == "1":
                candidates = []
                for index, poi in enumerate(payload.get("pois") or [], start=1):
                    name = self._safe_text(poi.get("name"))
                    location = self._safe_text(poi.get("location"))
                    if not name or "," not in location:
                        continue
                    try:
                        lng, lat = [float(item) for item in location.split(",")]
                    except Exception:
                        continue
                    poi_id = self._safe_text(poi.get("id"), f"{query}-{index}-{name}")
                    candidates.append(
                        {
                            "candidate_id": poi_id,
                            "source_query": query,
                            "name": name,
                            "category": query,
                            "type": self._safe_text(poi.get("type"), "POI 景点候选"),
                            "address": self._safe_text(poi.get("address")),
                            "lat": lat,
                            "lng": lng,
                            "data_mode": "live",
                        }
                    )
                    if len(candidates) >= limit:
                        break
                return {
                    "city": city,
                    "query": query,
                    "candidates": candidates,
                    "data_mode": "live",
                    "error": "",
                }

        data = self.resolve_city(city)
        if data is None:
            return {
                "city": city,
                "query": query,
                "candidates": [],
                "data_mode": "fallback_unavailable",
                "error": self._last_amap_error or "no live result and no local city data",
            }

        query_lc = query.lower()
        candidates = []
        for index, attraction in enumerate(data["attractions"], start=1):
            haystack = " ".join(
                [
                    self._safe_text(attraction.get("name")),
                    self._safe_text(attraction.get("category")),
                    self._safe_text(attraction.get("summary")),
                    " ".join(str(tag) for tag in attraction.get("tags", [])),
                ]
            ).lower()
            if query_lc and query_lc not in haystack:
                if len(candidates) >= limit:
                    break
                # Local data is tiny, so keep a few general candidates for the agent.
            candidates.append(
                {
                    "candidate_id": f"local-{city}-{index}",
                    "source_query": query,
                    "name": self._safe_text(attraction.get("name")),
                    "category": self._safe_text(attraction.get("category"), query),
                    "type": self._safe_text(attraction.get("summary"), "本地景点候选"),
                    "address": self._safe_text(attraction.get("area")),
                    "lat": float(attraction.get("lat", 0.0) or 0.0),
                    "lng": float(attraction.get("lng", 0.0) or 0.0),
                    "data_mode": "fallback",
                }
            )
            if len(candidates) >= limit:
                break
        return {
            "city": city,
            "query": query,
            "candidates": candidates,
            "data_mode": "fallback",
            "error": "",
        }

    def search_hotels(
        self,
        city: str,
        budget_min: float | None = None,
        budget_max: float | None = None,
        travelers: int | None = None,
        stay_nights: int | None = None,
        hotel_style: str = "comfort",
        **kwargs: Any,
    ) -> dict[str, Any]:
        budget_min = float(budget_min if budget_min is not None else kwargs.get("total_budget", 0) or 0)
        budget_max = float(budget_max if budget_max is not None else kwargs.get("total_budget", 0) or 0)
        travelers = int(travelers if travelers is not None else kwargs.get("travelers", 2) or 2)
        stay_nights = int(stay_nights if stay_nights is not None else kwargs.get("stay_length", 1) or 1)
        hotel_style = kwargs.get("style", hotel_style)
        limit = max(1, min(int(kwargs.get("limit", 3) or 3), 12))
        area_hint = self._safe_text(kwargs.get("area_hint", ""))
        search_focus = self._safe_text(kwargs.get("search_focus", "main"), "main")
        anchor_location = self._coerce_location(kwargs.get("anchor_location"))
        radius_m = max(500, min(int(kwargs.get("radius_m", 8000)), 20000))

        if self.amap_key:
            live = self._search_hotels_live(
                city,
                budget_min,
                budget_max,
                travelers,
                stay_nights,
                hotel_style,
                limit=limit,
                area_hint=area_hint,
                search_focus=search_focus,
                anchor_location=anchor_location,
                radius_m=radius_m,
            )
            if live:
                return live
            if anchor_location:
                return {"city": city, "candidates": [], "data_mode": "unavailable"}

        data = self.resolve_city(city)
        if data is None:
            raise ValueError(f"城市“{city}”暂无可用酒店数据：未命中内置城市，且实时地图酒店检索失败。")
        per_night_cap = max(budget_max / max(stay_nights, 1) * 0.45, 180)
        candidates = []
        for hotel in data["hotels"]:
            score = 1
            if hotel["style"] == hotel_style:
                score += 3
            if hotel["nightly"] <= per_night_cap:
                score += 2
            if travelers >= 3 and hotel["nightly"] < per_night_cap * 0.85:
                score += 1
            candidates.append((score, hotel))

        candidates.sort(key=lambda item: (-item[0], item[1]["nightly"]))
        serialized = [self._serialize_hotel(item[1]) for item in candidates[:limit]]
        return {
            "city": city,
            "per_night_cap": round(per_night_cap, 2),
            "candidates": serialized,
            "data_mode": "fallback",
            "area_hint": area_hint,
            "search_focus": search_focus,
        }

    def search_restaurants(
        self,
        city: str,
        anchor: str = "",
        preferences: str = "",
        budget_hint: float = 0.0,
        travelers: int = 2,
        radius_m: int = 2000,
        anchor_location: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.amap_key:
            live = self._search_restaurants_live(
                city=city,
                anchor=anchor,
                preferences=preferences,
                budget_hint=budget_hint,
                travelers=travelers,
                radius_m=radius_m,
                anchor_location=anchor_location,
            )
            if live:
                return live
            return {"city": city, "restaurants": [], "data_mode": "unavailable"}

        data = self.resolve_city(city)
        if data is None:
            raise ValueError(f"城市“{city}”暂无可用餐饮数据：未命中内置城市，且实时地图餐饮检索失败。")

        preference_list = [item.strip() for item in preferences.split(",") if item.strip()]
        labels = [PREFERENCE_LABELS.get(pref, pref) for pref in preference_list]
        anchors = [anchor] if anchor else [data.get("night_area", city)]
        restaurants: list[dict[str, Any]] = []
        base_lat = data["attractions"][0]["lat"] if data.get("attractions") else 0.0
        base_lng = data["attractions"][0]["lng"] if data.get("attractions") else 0.0
        for index, area in enumerate(anchors):
            area_name = area or data.get("night_area", city)
            restaurants.append(
                {
                    "name": f"{area_name}风味餐厅",
                    "cuisine": "本地风味",
                    "avg_cost_per_person": self._estimate_restaurant_price(budget_hint, travelers, index),
                    "price_source": "local_sample",
                    "summary": f"适合在{area_name}附近安排用餐，偏向{self._human_preference_summary(labels) or '城市特色'}。",
                    "nearby_anchor": area_name,
                    "location": {
                        "name": f"{area_name}风味餐厅",
                        "address": area_name,
                        "lat": base_lat + 0.003 * (index + 1),
                        "lng": base_lng + 0.003 * (index + 1),
                    },
                }
            )

        return {
            "city": city,
            "anchor": anchor,
            "preferences": preference_list,
            "restaurants": restaurants[:4],
            "data_mode": "fallback",
        }

    def get_weather_forecast(self, city: str, start_date: str, end_date: str) -> dict[str, Any]:
        cache_key = (city, start_date, end_date, self.qweather_host, self.qweather_key)
        cached = self._weather_cache.get(cache_key)
        if cached and time.monotonic() < cached[0]:
            return deepcopy(cached[1])
        result = QWeatherClient(self.qweather_key, self.qweather_host).forecast(city, start_date, end_date)
        ttl = 600 if result["data_mode"] in {"live", "partial"} else 60
        # Bound the cache in a long-running local process.
        if len(self._weather_cache) >= 128:
            self._weather_cache.clear()
        self._weather_cache[cache_key] = (time.monotonic() + ttl, deepcopy(result))
        return result

    def estimate_local_transport(
        self,
        city: str,
        origin_name: str,
        destination_name: str,
        mode: str = "public_transit",
        transit_preference: str = "recommended",
        origin_location: dict[str, Any] | None = None,
        destination_location: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transport_cache_key = (
            self._safe_text(city).lower(),
            self._safe_text(origin_name).lower(),
            self._safe_text(destination_name).lower(),
            self._safe_text(mode).lower(),
            self._safe_text(transit_preference).lower(),
            str(round(float((origin_location or {}).get("lat", 0.0) or 0.0), 6)),
            str(round(float((origin_location or {}).get("lng", 0.0) or 0.0), 6)),
            str(round(float((destination_location or {}).get("lat", 0.0) or 0.0), 6)),
            str(round(float((destination_location or {}).get("lng", 0.0) or 0.0), 6)),
        )
        if transport_cache_key in self._transport_cache:
            return dict(self._transport_cache[transport_cache_key])

        fallback_reason = ""
        if self.amap_key:
            live = self._estimate_local_transport_live(
                city,
                origin_name,
                destination_name,
                mode,
                transit_preference,
                origin_location=origin_location,
                destination_location=destination_location,
            )
            if live:
                self._transport_cache[transport_cache_key] = dict(live)
                return live
            fallback_reason = getattr(self, "_last_transport_live_error", "") or "live_transport_unavailable"

        data = self.resolve_city(city)
        origin = self._coerce_location(origin_location) or self._resolve_runtime_location(data, origin_name, city)
        destination = self._coerce_location(destination_location) or self._resolve_runtime_location(data, destination_name, city)
        distance_km = max(self._distance(origin["lat"], origin["lng"], destination["lat"], destination["lng"]), 1.2)
        if (
            self.amap_key
            and mode == "public_transit"
            and distance_km < 2
            and "amap_transit_empty" in fallback_reason
        ):
            walk_live = self._estimate_walking_route_live(city, origin_name, destination_name, origin, destination, fallback_reason)
            if walk_live:
                self._transport_cache[transport_cache_key] = dict(walk_live)
                return walk_live
        if mode == "self_drive":
            duration_min = int(distance_km * 4.8 + 10)
            estimated_cost = round(distance_km * 0.9 + 8, 1)
            transfers = 0
            walk_distance_m = 120
            summary = "本地兜底自驾估算。"
            route_segments = [
                self._segment(
                    segment_type="drive",
                    instruction=f"从 {origin_name} 自驾前往 {destination_name}",
                    duration_min=duration_min,
                    distance_m=int(distance_km * 1000),
                    details={"strategy": "默认自驾策略", "traffic_status": "本地兜底无实时路况"},
                )
            ]
            traffic_status = "无实时路况"
        elif mode == "taxi":
            duration_min = int(distance_km * 5.4 + 8)
            estimated_cost = round(distance_km * 3.2 + 12, 1)
            transfers = 0
            walk_distance_m = 80
            summary = "本地兜底打车估算。"
            route_segments = [
                self._segment(
                    segment_type="taxi",
                    instruction=f"从 {origin_name} 打车前往 {destination_name}",
                    duration_min=duration_min,
                    distance_m=int(distance_km * 1000),
                    details={"route_brief": "本地兜底不含详细道路名"},
                )
            ]
            traffic_status = ""
        else:
            duration_min = int(distance_km * 6.5 + 15)
            estimated_cost = round(2 + math.ceil(distance_km / 6), 1)
            transfers = 1 if distance_km > 4 else 0
            walk_distance_m = int(distance_km * 180)
            summary = f"本地兜底公共交通估算，策略：{self._transit_preference_label(transit_preference)}。"
            route_segments = self._fallback_transit_segments(origin_name, destination_name, duration_min, walk_distance_m, transfers)
            traffic_status = ""
        result = {
            "city": city,
            "mode": mode,
            "transit_preference": transit_preference,
            "origin": origin_name,
            "destination": destination_name,
            "distance_km": round(distance_km, 1),
            "duration_min": duration_min,
            "estimated_cost": estimated_cost,
            "estimated_taxi_cost": round(distance_km * 3.2 + 12, 1),
            "transfers": transfers,
            "walk_distance_m": walk_distance_m,
            "tolls": round(distance_km * 0.25, 1) if mode == "self_drive" else 0.0,
            "data_mode": "fallback",
            "fallback_reason": fallback_reason,
            "summary": summary,
            "strategy_label": self._transit_preference_label(transit_preference) if mode == "public_transit" else "",
            "traffic_status": traffic_status,
            "route_segments": [segment.model_dump() for segment in route_segments],
        }
        self._transport_cache[transport_cache_key] = dict(result)
        return result

    def _resolve_runtime_location(
        self,
        data: dict[str, Any] | None,
        name: str,
        city: str,
    ) -> dict[str, Any]:
        if data is not None:
            located = self._find_location(data, name)
            if located is not None:
                return located

        poi = self._geocode_poi(name, city) if self.amap_key else None
        if poi is not None:
            return {
                "name": name,
                "address": city,
                "lat": poi["lat"],
                "lng": poi["lng"],
            }

        city_geo = self.geocode_city(city)
        if city_geo is not None:
            raise ValueError(f"地点“{name}”无法在城市“{city}”内定位，不能用城市中心冒充该地点。")

        raise ValueError(f"城市“{city}”暂无可用交通参考数据：未命中内置城市，且地图服务无法识别。")

    def _coerce_location(self, location: dict[str, Any] | None) -> dict[str, float] | None:
        if not location:
            return None
        try:
            lat = float(location.get("lat", 0.0) or 0.0)
            lng = float(location.get("lng", 0.0) or 0.0)
        except Exception:
            return None
        if lat == 0.0 and lng == 0.0:
            return None
        return {"lat": lat, "lng": lng}

    def geocode_city(self, city: str) -> dict[str, Any] | None:
        if not self.amap_key:
            return None
        try:
            payload = self._amap_get(
                "https://restapi.amap.com/v3/geocode/geo",
                {"address": city},
            )
            '''
            成功 返回大概是：
            {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "geocodes": [
        {
            "formatted_address": "广东省广州市...",
            "location": "113.xxx,23.xxx",
            "country": "中国",
            "province": "广东省",
            "city": "广州市",
            "adcode": "440106",
            ...
        }
    ]
}
            '''
            if payload is None or str(payload.get("status")) != "1":
                return None
            geocodes = payload.get("geocodes") or []
            if not geocodes:
                return None
            first = geocodes[0]
            lng, lat = [float(item) for item in first["location"].split(",")]
            return {
                "city": city,
                "adcode": first.get("adcode", ""),
                "formatted_address": self._safe_text(first.get("formatted_address"), city),
                "location": {"lat": lat, "lng": lng},
            }
        except Exception:
            return None

    def _search_attractions_live(
        self,
        city: str,
        preferences: str,
        travelers: int,
        daily_budget: float,
        target_count: int = 6,
    ) -> dict[str, Any] | None:
        geo = self.geocode_city(city)
        if not geo:
            return None

        keyword_map = {
            "humanity": ["博物馆", "古镇", "历史街区", "寺庙"],
            "art": ["美术馆", "展览馆", "艺术中心"],
            "nature": ["公园", "湿地", "湖", "山"],
            "food": ["美食街", "夜市", "步行街"],
            "shopping": ["商圈", "步行街"],
            "nightlife": ["酒吧街", "夜市", "商圈"],
            "family": ["动物园", "主题公园", "乐园"],
        }
        preference_list = [item.strip() for item in preferences.split(",") if item.strip()]
        keyword_groups = [keyword_map.get(pref, []) for pref in preference_list if keyword_map.get(pref)]
        keywords: list[str] = []
        if keyword_groups:
            max_group_len = max(len(group) for group in keyword_groups)
            for index in range(max_group_len):
                for group in keyword_groups:
                    if index < len(group) and group[index] not in keywords:
                        keywords.append(group[index])
        if not keywords:
            keywords = ["景点", "博物馆", "公园", "步行街"]

        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        max_keyword_searches = min(max(4, len(keyword_groups) or 0, math.ceil(target_count / 3)), 12)
        for keyword in keywords[:max_keyword_searches]:
            try:
                payload = self._amap_get(
                    "https://restapi.amap.com/v3/place/text",
                    {
                        "keywords": keyword,
                        "city": geo["adcode"] or city,
                        "citylimit": "true",
                        "offset": min(max(target_count, 6), 20),
                        "page": 1,
                    },
                )
                if payload is None or str(payload.get("status")) != "1":
                    continue
            except Exception:
                continue

            for poi in payload.get("pois") or []:
                if not poi.get("name") or poi["name"] in seen:
                    continue
                location = poi.get("location", "")
                if "," not in location:
                    continue
                lng, lat = [float(item) for item in location.split(",")]
                seen.add(poi["name"])
                items.append(
                    {
                        "candidate_id": self._safe_text(poi.get("id"), f"{keyword}-{len(items) + 1}-{poi.get('name')}"),
                        "source_query": keyword,
                        "name": self._safe_text(poi.get("name"), "未命名景点"),
                        "category": keyword,
                        "tags": preference_list or ["humanity"],
                        "summary": self._safe_text(poi.get("type"), "POI 景点候选"),
                        "recommended_hours": 2.0,
                        "ticket_price": self._estimate_attraction_ticket(keyword, daily_budget),
                        "best_time": self._best_time_by_keyword(keyword),
                        "location": {
                            "name": self._safe_text(poi.get("name"), "未命名景点"),
                            "address": self._safe_text(poi.get("address")),
                            "lat": lat,
                            "lng": lng,
                        },
                    }
                )
                if len(items) >= target_count:
                    break
            if len(items) >= target_count:
                break

        if not items:
            return None

        night_area = next((item["name"] for item in items if "街" in item["name"] or "商" in item["name"]), geo["formatted_address"])
        return {
            "city": city,
            "preferences": preference_list,
            "travelers": travelers,
            "attractions": items[:target_count],
            "data_mode": "live",
            "night_area": night_area,
        }

    def _search_hotels_live(
        self,
        city: str,
        budget_min: float,
        budget_max: float,
        travelers: int,
        stay_nights: int,
        hotel_style: str,
        *,
        limit: int = 3,
        area_hint: str = "",
        search_focus: str = "main",
        anchor_location: dict[str, Any] | None = None,
        radius_m: int = 8000,
    ) -> dict[str, Any] | None:
        geo = self.geocode_city(city)
        if not geo:
            return None

        base_keyword = {"budget": "快捷酒店", "comfort": "酒店", "premium": "高档酒店"}.get(hotel_style, "酒店")
        keyword = " ".join(part for part in [area_hint, base_keyword, "" if search_focus == "main" else search_focus] if part)
        try:
            params = {
                "keywords": keyword,
                "types": "100000",
                "city": geo["adcode"] or city,
                "citylimit": "true",
                "offset": 25 if anchor_location else max(limit, 8),
                "page": 1,
            }
            if anchor_location:
                params.update(
                    location=f"{anchor_location['lng']},{anchor_location['lat']}",
                    radius=radius_m,
                    sortrule="distance",
                )
                params["keywords"] = base_keyword
            payload = self._amap_get(
                (
                    "https://restapi.amap.com/v3/place/around"
                    if anchor_location
                    else "https://restapi.amap.com/v3/place/text"
                ),
                params,
            )
            if payload is None or str(payload.get("status")) != "1":
                return None
        except Exception:
            return None

        per_night_cap = max(budget_max / max(stay_nights, 1) * 0.45, 180)
        candidates: list[dict[str, Any]] = []
        for poi in payload.get("pois") or []:
            location = poi.get("location", "")
            if not poi.get("name") or "," not in location:
                continue
            lng, lat = [float(item) for item in location.split(",")]
            if (
                anchor_location
                and self._distance(
                    lat, lng, anchor_location["lat"], anchor_location["lng"]
                )
                > radius_m / 1000
            ):
                continue
            low, high, actual_style, stars = self._hotel_price_reference(poi)
            nightly = round((low + high) / 2, 2)
            poi_name = self._safe_text(poi.get("name"), "未命名酒店")
            candidates.append(
                {
                    "name": poi_name,
                    "style": actual_style,
                    "star_level": stars,
                    "nightly_price": nightly,
                    "nightly_price_min": low,
                    "nightly_price_max": high,
                    "price_source": "estimated_from_poi",
                    "booking_url": "",
                    "summary": self._safe_text(poi.get("type"), "酒店地点候选"),
                    "nearby_area": self._safe_text(poi.get("address"), city),
                    "location": {
                        "name": poi_name,
                        "address": "".join(
                            self._safe_text(poi.get(key))
                            for key in ("pname", "cityname", "adname", "address")
                        ),
                        "lat": lat,
                        "lng": lng,
                    },
                }
            )
            if len(candidates) >= limit:
                break

        if not candidates:
            return None

        return {
            "city": city,
            "per_night_cap": round(per_night_cap, 2),
            "candidates": candidates,
            "data_mode": "live",
            "area_hint": area_hint,
            "search_focus": search_focus,
        }

    def _search_restaurants_live(
        self,
        *,
        city: str,
        anchor: str,
        preferences: str,
        budget_hint: float,
        travelers: int,
        radius_m: int,
        anchor_location: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        geo = self.geocode_city(city)
        if not geo:
            return None

        preference_list = [item.strip() for item in preferences.split(",") if item.strip()]
        search_anchor = anchor.strip() or geo.get("formatted_address", city)
        center = self._coerce_location(anchor_location) or self._geocode_poi(
            search_anchor, city
        )
        if center is None:
            return None

        keywords = self._restaurant_keywords(preference_list)
        seen: set[str] = set()
        restaurants: list[dict[str, Any]] = []

        for keyword in keywords:
            try:
                payload = self._amap_get(
                    "https://restapi.amap.com/v3/place/around",
                    {
                        "location": f"{center['lng']},{center['lat']}",
                        "keywords": keyword,
                        "types": "050000",
                        "radius": min(max(radius_m, 500), 5000),
                        "offset": 10,
                        "page": 1,
                        "sortrule": "distance",
                    },
                )
                if payload is None or str(payload.get("status")) != "1":
                    continue
            except Exception:
                continue

            for poi in payload.get("pois") or []:
                name = self._safe_text(poi.get("name"))
                location = poi.get("location", "")
                if not name or name in seen or "," not in location:
                    continue
                lng, lat = [float(item) for item in location.split(",")]
                if (
                    self._distance(lat, lng, center["lat"], center["lng"])
                    > radius_m / 1000
                ):
                    continue
                address = self._safe_text(poi.get("address"))
                address_source = "poi"
                if not address:
                    address = self._reverse_geocode_address(lat, lng)
                    address_source = "reverse_geocode" if address else "missing"
                seen.add(name)
                restaurants.append(
                    {
                        "name": name,
                        "cuisine": self._safe_text(keyword, "餐饮"),
                        "avg_cost_per_person": self._estimate_restaurant_price(
                            budget_hint,
                            travelers,
                            len(restaurants),
                        ),
                        "price_source": "estimated_from_poi",
                        "address_source": address_source,
                        "summary": self._safe_text(poi.get("type"), "高德 POI 餐饮候选"),
                        "nearby_anchor": self._safe_text(search_anchor, city),
                        "location": {
                            "name": name,
                            "address": address,
                            "lat": lat,
                            "lng": lng,
                        },
                    }
                )
                if len(restaurants) >= 6:
                    break
            if len(restaurants) >= 6:
                break

        if not restaurants:
            return None

        return {
            "city": city,
            "anchor": search_anchor,
            "preferences": preference_list,
            "restaurants": restaurants,
            "data_mode": "live",
        }

    def _estimate_local_transport_live(
        self,
        city: str,
        origin_name: str,
        destination_name: str,
        mode: str,
        transit_preference: str,
        origin_location: dict[str, Any] | None = None,
        destination_location: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        self._last_transport_live_error = ""
        origin = self._coerce_location(origin_location) or self._geocode_poi(origin_name, city)
        origin_error = self._last_poi_geocode_error
        destination = self._coerce_location(destination_location) or self._geocode_poi(destination_name, city)
        destination_error = self._last_poi_geocode_error
        if not origin or not destination:
            missing = []
            if not origin:
                missing.append(f"origin_poi_geocode_failed:{origin_error or 'unknown'}")
            if not destination:
                missing.append(f"destination_poi_geocode_failed:{destination_error or 'unknown'}")
            self._last_transport_live_error = ",".join(missing)
            return None

        if mode == "public_transit":
            return self._estimate_transit_route_live(city, origin_name, destination_name, origin, destination, transit_preference)

        try:
            payload = self._amap_get(
                "https://restapi.amap.com/v3/direction/driving",
                {
                    "origin": f"{origin['lng']},{origin['lat']}",
                    "destination": f"{destination['lng']},{destination['lat']}",
                },
            )
            if payload is None or str(payload.get("status")) != "1":
                self._last_transport_live_error = self._last_amap_error or "amap_driving_error"
                return None
            paths = ((payload.get("route") or {}).get("paths")) or []
            if not paths:
                self._last_transport_live_error = "amap_driving_empty"
                return None
            first = paths[0]
            distance_km = round(float(first.get("distance", 0)) / 1000, 1)
            duration_min = max(int(float(first.get("duration", 0)) / 60), 1)
            taxi_cost = round(distance_km * 3.1 + 12, 1)
            tolls = round(float(first.get("tolls", 0) or 0), 1)
            if mode == "self_drive":
                estimated_cost = round(distance_km * 0.9 + tolls + 8, 1)
                summary = "高德实时自驾路线。"
                traffic_status = self._traffic_status_label(first.get("strategy", ""))
                route_segments = self._parse_drive_steps(first.get("steps") or [])
            else:
                estimated_cost = taxi_cost
                summary = "高德实时打车路线。"
                traffic_status = ""
                route_segments = self._parse_taxi_steps(first.get("steps") or [])
            return {
                "city": city,
                "mode": mode,
                "transit_preference": transit_preference,
                "origin": origin_name,
                "destination": destination_name,
                "distance_km": distance_km,
                "duration_min": duration_min,
                "estimated_cost": estimated_cost,
                "estimated_taxi_cost": taxi_cost,
                "transfers": 0,
                "walk_distance_m": 0,
                "tolls": tolls,
                "data_mode": "live",
                "summary": summary,
                "strategy_label": "",
                "traffic_status": traffic_status,
                "route_segments": [segment.model_dump() for segment in route_segments],
            }
        except Exception:
            self._last_transport_live_error = "driving_request_exception"
            return None

    def _estimate_transit_route_live(
        self,
        city: str,
        origin_name: str,
        destination_name: str,
        origin: dict[str, float],
        destination: dict[str, float],
        transit_preference: str,
    ) -> dict[str, Any] | None:
        geo = self.geocode_city(city)
        city_code = geo["adcode"] if geo else city
        strategy = self._transit_strategy(transit_preference)
        try:
            payload = self._amap_get(
                "https://restapi.amap.com/v3/direction/transit/integrated",
                {
                    "origin": f"{origin['lng']},{origin['lat']}",
                    "destination": f"{destination['lng']},{destination['lat']}",
                    "city": city_code,
                    "strategy": strategy,
                    "nightflag": 0,
                },
            )
            if payload is None:
                self._last_transport_live_error = self._last_amap_error or "transit_request_exception"
                return None
            if str(payload.get("status")) != "1":
                info = self._safe_text(payload.get("info"))
                infocode = self._safe_text(payload.get("infocode"))
                self._last_transport_live_error = f"amap_transit_error:{infocode}:{info}"
                return None
            transits = ((payload.get("route") or {}).get("transits")) or []
            if not transits:
                self._last_transport_live_error = "amap_transit_empty"
                return None
            first = self._select_best_transit(transits, transit_preference)
            distance_km = round(float(first.get("distance", 0)) / 1000, 1)
            duration_min = max(int(float(first.get("duration", 0)) / 60), 1)
            cost = round(float(first.get("cost", 0) or 0), 1)
            walking_distance = 0
            transfers = 0
            for segment in first.get("segments") or []:
                walking_distance += int(float((segment.get("walking") or {}).get("distance", 0) or 0))
                bus_info = segment.get("bus") or {}
                buslines = bus_info.get("buslines") or []
                railway = bus_info.get("railway") or {}
                if buslines:
                    transfers += 1
                if railway:
                    transfers += 1
            transfers = max(transfers - 1, 0) if transfers else 0
            route_segments = self._parse_transit_segments(first.get("segments") or [])
            alternatives = self._summarize_transit_alternatives(transits, first)
            return {
                "city": city,
                "mode": "public_transit",
                "transit_preference": transit_preference,
                "origin": origin_name,
                "destination": destination_name,
                "distance_km": distance_km,
                "duration_min": duration_min,
                "estimated_cost": cost,
                "estimated_taxi_cost": round(distance_km * 3.1 + 12, 1),
                "transfers": transfers,
                "walk_distance_m": walking_distance,
                "tolls": 0.0,
                "data_mode": "live",
                "summary": f"高德实时公共交通路线，策略：{self._transit_preference_label(transit_preference)}。",
                "strategy_label": self._transit_preference_label(transit_preference),
                "traffic_status": "",
                "route_alternatives": alternatives,
                "route_segments": [segment.model_dump() for segment in route_segments],
            }
        except Exception:
            self._last_transport_live_error = "transit_request_exception"
            return None

    def _estimate_walking_route_live(
        self,
        city: str,
        origin_name: str,
        destination_name: str,
        origin: dict[str, float],
        destination: dict[str, float],
        fallback_reason: str,
    ) -> dict[str, Any] | None:
        try:
            payload = self._amap_get(
                "https://restapi.amap.com/v3/direction/walking",
                {
                    "origin": f"{origin['lng']},{origin['lat']}",
                    "destination": f"{destination['lng']},{destination['lat']}",
                },
            )
            if payload is None or str(payload.get("status")) != "1":
                return None
            paths = ((payload.get("route") or {}).get("paths")) or []
            if not paths:
                return None
            first = paths[0]
            distance_km = round(float(first.get("distance", 0) or 0) / 1000, 1)
            duration_min = max(int(float(first.get("duration", 0) or 0) / 60), 1)
            route_segments = self._parse_walk_steps(first.get("steps") or [])
            return {
                "city": city,
                "mode": "walk",
                "transit_preference": "short_walk_after_transit_empty",
                "origin": origin_name,
                "destination": destination_name,
                "distance_km": distance_km,
                "duration_min": duration_min,
                "estimated_cost": 0.0,
                "estimated_taxi_cost": round(distance_km * 3.1 + 12, 1),
                "transfers": 0,
                "walk_distance_m": int(float(first.get("distance", 0) or 0)),
                "tolls": 0.0,
                "data_mode": "live_walk",
                "fallback_reason": f"{fallback_reason},short_distance_walk_api",
                "summary": "公共交通无可用方案且距离较短，已改用高德实时步行路线。",
                "strategy_label": "短距离步行",
                "traffic_status": "",
                "route_segments": [segment.model_dump() for segment in route_segments],
            }
        except Exception:
            return None

    def _transit_strategy(self, transit_preference: str) -> int:
        return {
            "recommended": 0,
            "less_walking": 3,
            "bus_priority": 5,
            "subway_priority": 7,
        }.get(transit_preference, 0)

    def _transit_preference_label(self, transit_preference: str) -> str:
        return {
            "recommended": "推荐",
            "less_walking": "步行少",
            "bus_priority": "公交优先",
            "subway_priority": "地铁优先",
        }.get(transit_preference, transit_preference)

    def _select_best_transit(self, transits: list[dict[str, Any]], transit_preference: str) -> dict[str, Any]:
        scored = []
        for index, transit in enumerate(transits):
            duration = float(transit.get("duration", 0) or 0)
            distance = float(transit.get("distance", 0) or 0)
            cost = float(transit.get("cost", 0) or 0)
            walk_distance = self._transit_walk_distance(transit)
            ride_count = self._transit_ride_count(transit)
            subway_count = self._transit_subway_count(transit)
            bus_count = self._transit_bus_count(transit)
            score = duration / 60
            score += walk_distance / 120
            score += max(ride_count - 1, 0) * 8
            score += cost * 0.3
            score += distance / 5000
            if transit_preference == "less_walking":
                score += walk_distance / 80
            elif transit_preference == "subway_priority":
                score -= subway_count * 4
                score += bus_count * 1.5
            elif transit_preference == "bus_priority":
                score -= bus_count * 2
            if ride_count == 1:
                score -= 4
            scored.append((score, index, transit))
        scored.sort(key=lambda item: (item[0], item[1]))
        return scored[0][2]

    def _transit_walk_distance(self, transit: dict[str, Any]) -> int:
        total = 0
        for segment in transit.get("segments") or []:
            total += int(float((segment.get("walking") or {}).get("distance", 0) or 0))
        return total

    def _transit_ride_count(self, transit: dict[str, Any]) -> int:
        count = 0
        for segment in transit.get("segments") or []:
            bus_info = segment.get("bus") or {}
            if bus_info.get("buslines"):
                count += 1
            if bus_info.get("railway"):
                count += 1
        return count

    def _transit_bus_count(self, transit: dict[str, Any]) -> int:
        count = 0
        for segment in transit.get("segments") or []:
            for line in ((segment.get("bus") or {}).get("buslines") or [])[:1]:
                if not self._is_subway_line(line.get("name", ""), line.get("type", "")):
                    count += 1
        return count

    def _transit_subway_count(self, transit: dict[str, Any]) -> int:
        count = 0
        for segment in transit.get("segments") or []:
            bus_info = segment.get("bus") or {}
            if bus_info.get("railway"):
                count += 1
            for line in (bus_info.get("buslines") or [])[:1]:
                if self._is_subway_line(line.get("name", ""), line.get("type", "")):
                    count += 1
        return count

    def _summarize_transit_alternatives(
        self,
        transits: list[dict[str, Any]],
        selected: dict[str, Any],
    ) -> list[dict[str, Any]]:
        alternatives: list[dict[str, Any]] = []
        for transit in transits:
            if transit is selected:
                continue
            lines = self._transit_primary_lines(transit)
            route_segments = self._parse_transit_segments(transit.get("segments") or [])
            if not lines and not route_segments:
                continue
            alternatives.append(
                {
                    "lines": lines,
                    "duration_min": max(int(float(transit.get("duration", 0) or 0) / 60), 1),
                    "distance_km": round(float(transit.get("distance", 0) or 0) / 1000, 1),
                    "cost": round(float(transit.get("cost", 0) or 0), 1),
                    "walk_distance_m": self._transit_walk_distance(transit),
                    "transfers": max(self._transit_ride_count(transit) - 1, 0),
                    "route_segments": [segment.model_dump() for segment in route_segments],
                }
            )
        return alternatives

    def _transit_primary_lines(self, transit: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        for segment in transit.get("segments") or []:
            bus_info = segment.get("bus") or {}
            railway = bus_info.get("railway") or {}
            if railway.get("name"):
                lines.append(str(railway["name"]))
            buslines = bus_info.get("buslines") or []
            if buslines and buslines[0].get("name"):
                lines.append(str(buslines[0]["name"]))
        return lines

    def _segment(
        self,
        *,
        segment_type: str,
        instruction: str,
        duration_min: int = 0,
        distance_m: int = 0,
        line_name: str = "",
        direction: str = "",
        on_station: str = "",
        off_station: str = "",
        via_count: int = 0,
        via_stops: list[str] | None = None,
        entrance: str = "",
        exit: str = "",
        details: dict[str, Any] | None = None,
    ) -> RouteDetailSegment:
        return RouteDetailSegment(
            segment_type=segment_type,
            instruction=instruction,
            duration_min=duration_min,
            distance_m=distance_m,
            line_name=line_name,
            direction=direction,
            on_station=on_station,
            off_station=off_station,
            via_count=via_count,
            via_stops=via_stops or [],
            entrance=entrance,
            exit=exit,
            details=details or {},
        )

    def _fallback_transit_segments(
        self,
        origin_name: str,
        destination_name: str,
        duration_min: int,
        walk_distance_m: int,
        transfers: int,
    ) -> list[RouteDetailSegment]:
        return [
            self._segment(
                segment_type="transit",
                instruction=(
                    f"高德未返回 {origin_name} 到 {destination_name} 的实时公共交通方案，"
                    "当前仅展示时间、距离、费用估算，不生成具体线路和站点。"
                ),
                duration_min=duration_min,
                distance_m=0,
                details={
                    "estimated": True,
                    "walk_distance_m": walk_distance_m,
                    "transfers": transfers,
                },
            ),
        ]

    def _parse_transit_segments(self, segments: list[dict[str, Any]]) -> list[RouteDetailSegment]:
        parsed: list[RouteDetailSegment] = []
        for segment in segments:
            walking = segment.get("walking") or {}
            if walking:
                instruction = "；".join(step.get("instruction", "") for step in (walking.get("steps") or []) if step.get("instruction"))
                parsed.append(
                    self._segment(
                        segment_type="walk",
                        instruction=instruction or "步行接驳",
                        duration_min=max(int(float(walking.get("duration", 0) or 0) / 60), 0),
                        distance_m=int(float(walking.get("distance", 0) or 0)),
                    )
                )

            bus_info = segment.get("bus") or {}
            buslines = bus_info.get("buslines") or []
            railway = bus_info.get("railway") or {}

            if railway:
                parsed.append(
                    self._segment(
                        segment_type="railway",
                        instruction=railway.get("trip", "") or "乘坐铁路/城际铁路",
                        duration_min=max(int(float(railway.get("time", 0) or 0) / 60), 0),
                        distance_m=int(float(railway.get("distance", 0) or 0)),
                        line_name=railway.get("name", ""),
                        direction=railway.get("trip", ""),
                        on_station=(railway.get("departure_stop") or {}).get("name", ""),
                        off_station=(railway.get("arrival_stop") or {}).get("name", ""),
                        via_count=int(railway.get("via_num", 0) or 0),
                        entrance=(segment.get("entrance") or {}).get("name", ""),
                        exit=(segment.get("exit") or {}).get("name", ""),
                    )
                )

            if buslines:
                line = buslines[0]
                line_name = line.get("name", "") or ""
                is_subway_line = self._is_subway_line(line_name, line.get("type", ""))
                arrival_stop_name = (line.get("arrival_stop") or {}).get("name", "")
                departure_stop_name = (line.get("departure_stop") or {}).get("name", "")
                direction = self._build_transit_direction(line_name, departure_stop_name, arrival_stop_name)
                instruction = self._build_transit_instruction(
                    line_name=line_name,
                    departure_stop=departure_stop_name,
                    arrival_stop=arrival_stop_name,
                    is_subway_line=is_subway_line,
                )
                alternatives = [
                    {
                        "line_name": alternative.get("name", "") or "",
                        "departure_stop": (alternative.get("departure_stop") or {}).get("name", ""),
                        "arrival_stop": (alternative.get("arrival_stop") or {}).get("name", ""),
                    }
                    for alternative in buslines[1:]
                    if alternative.get("name")
                ]
                parsed.append(
                    self._segment(
                        segment_type="subway" if is_subway_line else "bus",
                        instruction=instruction,
                        duration_min=max(int(float(line.get("duration", 0) or 0) / 60), 0),
                        distance_m=int(float(line.get("distance", 0) or 0)),
                        line_name=line_name,
                        direction=direction,
                        on_station=departure_stop_name,
                        off_station=arrival_stop_name,
                        via_count=int(line.get("via_num", 0) or 0),
                        via_stops=[stop.get("name", "") for stop in (line.get("via_stops") or [])],
                        entrance=(segment.get("entrance") or {}).get("name", ""),
                        exit=(segment.get("exit") or {}).get("name", ""),
                        details={"alternatives": alternatives} if alternatives else {},
                    )
                )
        return parsed

    def _is_subway_line(self, line_name: str, line_type: str) -> bool:
        normalized_type = str(line_type or "").lower()
        normalized_name = str(line_name or "").lower()
        subway_markers = ("地铁", "轨道", "轨道交通", "轻轨", "metro", "subway")
        bus_markers = ("公交", "公共汽车", "旅游", "专线", "bus")

        if any(marker in normalized_type for marker in subway_markers):
            return True
        if any(marker in normalized_type for marker in bus_markers):
            return False
        if any(marker in normalized_name for marker in subway_markers):
            return True
        return False

    def _build_transit_direction(self, line_name: str, departure_stop: str, arrival_stop: str) -> str:
        if "(" in line_name and ")" in line_name:
            inner = line_name.split("(", 1)[1].rsplit(")", 1)[0].strip()
            if inner:
                return inner
        if "（" in line_name and "）" in line_name:
            inner = line_name.split("（", 1)[1].rsplit("）", 1)[0].strip()
            if inner:
                return inner
        if arrival_stop:
            return f"前往{arrival_stop}"
        if departure_stop:
            return f"从{departure_stop}出发"
        return ""

    def _build_transit_instruction(
        self,
        *,
        line_name: str,
        departure_stop: str,
        arrival_stop: str,
        is_subway_line: bool,
    ) -> str:
        if line_name and departure_stop and arrival_stop:
            prefix = "乘坐地铁" if is_subway_line else "乘坐公交"
            return f"{prefix}{line_name}，从{departure_stop}上车，到{arrival_stop}下车"
        if line_name:
            return line_name
        return "乘坐地铁" if is_subway_line else "乘坐公交"

    def _parse_drive_steps(self, steps: list[dict[str, Any]]) -> list[RouteDetailSegment]:
        parsed: list[RouteDetailSegment] = []
        for step in steps[:8]:
            parsed.append(
                self._segment(
                    segment_type="drive",
                    instruction=step.get("instruction", "") or "按导航前往",
                    duration_min=max(int(float(step.get("duration", 0) or 0) / 60), 0),
                    distance_m=int(float(step.get("distance", 0) or 0)),
                    details={"road": step.get("road", ""), "orientation": step.get("orientation", "")},
                )
            )
        return parsed

    def _parse_taxi_steps(self, steps: list[dict[str, Any]]) -> list[RouteDetailSegment]:
        parsed: list[RouteDetailSegment] = []
        for step in steps[:5]:
            parsed.append(
                self._segment(
                    segment_type="taxi",
                    instruction=step.get("instruction", "") or "按推荐道路行驶",
                    duration_min=max(int(float(step.get("duration", 0) or 0) / 60), 0),
                    distance_m=int(float(step.get("distance", 0) or 0)),
                    details={"road": step.get("road", "")},
                )
            )
        return parsed

    def _parse_walk_steps(self, steps: list[dict[str, Any]]) -> list[RouteDetailSegment]:
        parsed: list[RouteDetailSegment] = []
        for step in steps[:10]:
            parsed.append(
                self._segment(
                    segment_type="walk",
                    instruction=step.get("instruction", "") or "按步行导航前往",
                    duration_min=max(int(float(step.get("duration", 0) or 0) / 60), 0),
                    distance_m=int(float(step.get("distance", 0) or 0)),
                    details={"road": step.get("road", ""), "orientation": step.get("orientation", "")},
                )
            )
        return parsed

    def _traffic_status_label(self, strategy: str) -> str:
        if not strategy:
            return "按默认策略规划"
        return f"策略代码：{strategy}"

    def _serialize_attraction(self, attraction: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": attraction.get("candidate_id", f"local-{attraction['name']}"),
            "source_query": attraction.get("source_query", attraction.get("category", "")),
            "name": attraction["name"],
            "category": attraction["category"],
            "tags": attraction["tags"],
            "summary": attraction["summary"],
            "recommended_hours": attraction["hours"],
            "ticket_price": attraction["ticket"],
            "best_time": attraction["best_time"],
            "location": {
                "name": attraction["name"],
                "address": "",
                "lat": attraction["lat"],
                "lng": attraction["lng"],
            },
        }

    def _serialize_hotel(self, hotel: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": hotel["name"],
            "style": hotel["style"],
            "star_level": hotel["stars"],
            "nightly_price": hotel["nightly"],
            "price_source": "local_sample",
            "booking_url": "",
            "summary": hotel["summary"],
            "nearby_area": hotel["area"],
            "location": {
                "name": hotel["name"],
                "address": hotel["area"],
                "lat": hotel["lat"],
                "lng": hotel["lng"],
            },
        }

    def _find_location(self, data: dict[str, Any], name: str) -> dict[str, Any] | None:
        for attraction in data["attractions"]:
            if attraction["name"].lower() == name.lower():
                return attraction
        for hotel in data["hotels"]:
            if hotel["name"].lower() == name.lower():
                return hotel
        return None

    def _distance(self, lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        dx = (lng2 - lng1) * 85
        dy = (lat2 - lat1) * 111
        return math.sqrt(dx * dx + dy * dy)

    def _geocode_poi(self, keyword: str, city: str) -> dict[str, float] | None:
        self._last_poi_geocode_error = ""
        try:
            geo = self.geocode_city(city)
            city_scope = (geo or {}).get("adcode") or city
            payload = self._amap_get(
                "https://restapi.amap.com/v3/place/text",
                {
                    "keywords": keyword,
                    "city": city_scope,
                    "citylimit": "true",
                    "offset": 1,
                    "page": 1,
                },
            )
            if payload is None:
                self._last_poi_geocode_error = self._last_amap_error or "poi_request_exception"
                return None
            if str(payload.get("status")) != "1":
                info = self._safe_text(payload.get("info"))
                infocode = self._safe_text(payload.get("infocode"))
                self._last_poi_geocode_error = f"amap_poi_error:{infocode}:{info}"
                return None
            pois = payload.get("pois") or []
            if not pois:
                self._last_poi_geocode_error = "poi_empty"
                return None
            location = pois[0].get("location", "")
            if "," not in location:
                self._last_poi_geocode_error = "poi_location_missing"
                return None
            lng, lat = [float(item) for item in location.split(",")]
            return {"lat": lat, "lng": lng}
        except Exception:
            self._last_poi_geocode_error = "poi_request_exception"
            return None

    def _reverse_geocode_address(self, lat: float, lng: float) -> str:
        if not self.amap_key or lat == 0.0 and lng == 0.0:
            return ""
        try:
            payload = self._amap_get(
                "https://restapi.amap.com/v3/geocode/regeo",
                {
                    "location": f"{lng},{lat}",
                    "extensions": "base",
                },
            )
            if payload is None or str(payload.get("status")) != "1":
                return ""
            regeocode = payload.get("regeocode") or {}
            return self._safe_text(regeocode.get("formatted_address"))
        except Exception:
            return ""

    def _estimate_attraction_ticket(self, keyword: str, daily_budget: float) -> float:
        if keyword in {"公园", "步行街", "夜市"}:
            return 0.0
        if keyword in {"博物馆", "美术馆"}:
            return min(max(daily_budget * 0.08, 0), 60)
        return min(max(daily_budget * 0.15, 20), 120)

    def _best_time_by_keyword(self, keyword: str) -> str:
        if keyword in {"夜市", "酒吧街", "商圈", "步行街"}:
            return "evening"
        if keyword in {"公园", "湿地", "湖", "山"}:
            return "morning"
        return "afternoon"

    def _hotel_price_reference(
        self, poi: dict[str, Any]
    ) -> tuple[float, float, str, int]:
        """Coarse planning allowance per room; POI data is not a room quote."""
        name = self._safe_text(poi.get("name"))
        text = name + self._safe_text(poi.get("type"))
        stars = next(
            (
                level
                for label, level in (("五星", 5), ("四星", 4), ("三星", 3))
                if label in text
            ),
            0,
        )
        if stars >= 4 or any(
            word in text
            for word in (
                "希尔顿",
                "万豪",
                "洲际",
                "喜来登",
                "四季酒店",
                "丽思卡尔顿",
                "豪华型",
            )
        ):
            return 600, 1200, "premium", stars
        if any(word in name for word in ("旅馆", "宾馆", "招待所", "青年旅舍", "客栈")):
            return 100, 220, "budget", stars
        if any(
            word in text for word in ("快捷", "如家", "汉庭", "7天", "七天", "经济型")
        ):
            return 160, 300, "budget", stars
        return 260, 500, "comfort", stars

    def _estimate_restaurant_price(self, budget_hint: float, travelers: int, index: int) -> float:
        if budget_hint > 0 and travelers > 0:
            per_person = max((budget_hint / max(travelers, 1)) * 0.28, 35)
        else:
            per_person = 55
        return round(per_person + index * 8, 2)

    def _restaurant_keywords(self, preferences: list[str]) -> list[str]:
        keywords: list[str] = []
        known_tags = {"food", "nightlife", "humanity", "art"}
        for preference in preferences:
            cleaned = self._safe_text(preference)
            if cleaned and cleaned not in known_tags:
                keywords.append(cleaned)
        if "food" in preferences:
            keywords.extend(["美食", "本地菜", "特色餐厅"])
        if "nightlife" in preferences:
            keywords.extend(["夜宵", "小吃", "酒馆"])
        if "humanity" in preferences or "art" in preferences:
            keywords.extend(["老字号", "茶餐厅", "咖啡馆"])
        if not keywords:
            keywords = ["美食", "餐厅", "本地菜"]
        unique: list[str] = []
        for keyword in keywords:
            if keyword not in unique:
                unique.append(keyword)
        return unique[:4]

    def _human_preference_summary(self, labels: list[str]) -> str:
        joined = "、".join(label for label in labels if label)
        return joined

    def _weather_suggestion(self, condition: str) -> str:
        lowered = condition.lower()
        if "rain" in lowered:
            return "建议备伞，并准备室内替代景点。"
        if "storm" in lowered:
            return "建议减少户外路线密度，优先考虑安全。"
        if "sun" in lowered or "clear" in lowered:
            return "适合户外景点和城市漫步。"
        if "cloud" in lowered:
            return "天气均衡，适合室内外混合行程。"
        if "hot" in lowered:
            return "下午尽量避免高密度户外安排。"
        return "出发前建议再核对当地天气。"

    def _build_runtime_city_stub(self, geo: dict[str, Any]) -> dict[str, Any]:
        city = str(geo.get("city") or geo.get("formatted_address") or "目的地")
        return {
            "aliases": [city],
            "profile": f"{city} 适合安排城市精华观光、美食探索和本地地标体验。",
            "night_area": str(geo.get("formatted_address") or city),
            "attractions": [
                {"name": f"{city}博物馆", "category": "humanity", "tags": ["humanity", "art"], "hours": 2.0, "ticket": 20, "best_time": "afternoon", "summary": "运行时交通估算专用占位点。", "lat": geo["location"]["lat"], "lng": geo["location"]["lng"]},
                {"name": f"{city}滨河公园", "category": "nature", "tags": ["nature"], "hours": 2.0, "ticket": 0, "best_time": "morning", "summary": "运行时交通估算专用占位点。", "lat": geo["location"]["lat"] + 0.01, "lng": geo["location"]["lng"] + 0.01},
                {"name": f"{city}老街", "category": "food", "tags": ["food", "shopping", "nightlife", "humanity"], "hours": 2.0, "ticket": 0, "best_time": "evening", "summary": "运行时交通估算专用占位点。", "lat": geo["location"]["lat"] + 0.015, "lng": geo["location"]["lng"] + 0.015},
            ],
            "hotels": [
                {"name": f"{city}经济酒店", "style": "budget", "stars": 3, "nightly": 260, "summary": "运行时交通估算专用占位酒店。", "area": str(geo.get('formatted_address') or city), "lat": geo["location"]["lat"] + 0.005, "lng": geo["location"]["lng"] + 0.005},
                {"name": f"{city}舒适酒店", "style": "comfort", "stars": 4, "nightly": 420, "summary": "运行时交通估算专用占位酒店。", "area": str(geo.get('formatted_address') or city), "lat": geo["location"]["lat"] + 0.006, "lng": geo["location"]["lng"] + 0.006},
                {"name": f"{city}高档酒店", "style": "premium", "stars": 5, "nightly": 820, "summary": "运行时交通估算专用占位酒店。", "area": str(geo.get('formatted_address') or city), "lat": geo["location"]["lat"] + 0.007, "lng": geo["location"]["lng"] + 0.007},
            ],
        }

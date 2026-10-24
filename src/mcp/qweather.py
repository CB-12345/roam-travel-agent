from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

import requests

from src.services.observability import observed_call


class WeatherAPIError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class QWeatherClient:
    """Return dated forecasts, keeping unavailable dates explicit and credentials private."""

    def __init__(self, api_key: str, api_host: str):
        self.api_key = api_key.strip()
        value = api_host.strip().rstrip("/")
        if value and "://" not in value:
            value = "https://" + value
        try:
            parsed = urlsplit(value)
        except ValueError:
            parsed = urlsplit("")
        # Only send the credential to a QWeather HTTPS host, without redirects.
        valid = (
            parsed.scheme == "https"
            and re.fullmatch(
                r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.qweatherapi\.com", parsed.netloc
            )
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
        self.base_url = value if valid else ""
        self.host_configured = bool(value)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = observed_call("http", "qweather:" + path, lambda: requests.get(
                self.base_url + path,
                params=params,
                headers={"X-QW-Api-Key": self.api_key},
                timeout=12,
                allow_redirects=False,
            ))
        except requests.RequestException:
            raise WeatherAPIError(
                "network_error", "暂时无法连接和风天气，请稍后重试。"
            ) from None
        status = response.status_code
        messages = {
            "401": "和风天气认证失败，请核对 API Key 和专属 API Host。",
            "402": "和风天气账户额度或余额不足，请查看控制台。",
            "403": "和风天气拒绝访问，请检查凭据、接口权限和账户状态。",
            "404": "和风天气未找到该地区或接口数据。",
            "429": "和风天气请求过于频繁或额度已用完，请稍后重试并查看控制台。",
        }
        if status != 200:
            raise WeatherAPIError(
                f"http_{status}",
                messages.get(str(status), "和风天气暂时未能返回预报。"),
            )
        try:
            payload = response.json()
        except ValueError:
            raise WeatherAPIError(
                "invalid_response", "和风天气返回的数据无法解析。"
            ) from None
        if not isinstance(payload, dict):
            raise WeatherAPIError("invalid_response", "和风天气返回的数据格式不正确。")
        code = str(payload.get("code", ""))
        if code != "200":
            # Do not include upstream free-form errors or request URLs in reports.
            safe_code = code if re.fullmatch(r"\d{3}", code) else "unknown"
            raise WeatherAPIError(
                f"api_{safe_code}", messages.get(code, "和风天气暂无可用预报数据。")
            )
        return payload

    def forecast(self, city: str, start_date: str, end_date: str) -> dict[str, Any]:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if end < start:
            raise ValueError("天气查询的结束日期不能早于开始日期。")
        dates = [
            start + timedelta(days=offset) for offset in range((end - start).days + 1)
        ]
        error_code = ""
        available: dict[date, dict[str, Any]] = {}
        updated_at = ""
        try:
            if not self.api_key:
                raise WeatherAPIError("missing_key", "尚未配置和风天气 API Key。")
            if not self.base_url:
                code = "invalid_host" if self.host_configured else "missing_host"
                raise WeatherAPIError(
                    code, "请在本地配置中填写正确的和风天气专属 API Host。"
                )
            lookup = self._get(
                "/geo/v2/city/lookup", {"location": city, "lang": "zh", "number": 1}
            )
            locations = lookup.get("location")
            if (
                not isinstance(locations, list)
                or not locations
                or not isinstance(locations[0], dict)
                or not locations[0].get("id")
            ):
                raise WeatherAPIError("city_not_found", "和风天气未能识别这个城市。")
            payload = self._get(
                "/v7/weather/7d",
                {"location": locations[0]["id"], "lang": "zh", "unit": "m"},
            )
            updated_at = str(payload.get("updateTime") or "")
            raw_days = payload.get("daily")
            if not isinstance(raw_days, list):
                raise WeatherAPIError(
                    "invalid_forecast", "和风天气返回的逐日预报格式不正确。"
                )
            for item in raw_days:
                if not isinstance(item, dict):
                    continue
                try:
                    current = date.fromisoformat(item["fxDate"])
                    high, low = int(item["tempMax"]), int(item["tempMin"])
                    condition = str(item.get("textDay") or "").strip()
                    if high < low or not condition:
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
                available[current] = {
                    "date": current.isoformat(),
                    "condition": condition,
                    "high_c": high,
                    "low_c": low,
                    "suggestion": self._suggestion(condition, high),
                    "data_source": "live_qweather",
                }
            if not available:
                raise WeatherAPIError("empty_forecast", "和风天气暂无有效的逐日预报。")
            message = (
                f"和风天气预报范围：{min(available).isoformat()} 至 {max(available).isoformat()}。"
                "超出范围或缺少数据的日期暂无预报，出发前请再次查询。"
            )
        except WeatherAPIError as exc:
            error_code, message = exc.code, str(exc)

        forecast = [
            available.get(current)
            or {
                "date": current.isoformat(),
                "condition": "unknown",
                "high_c": None,
                "low_c": None,
                "suggestion": "暂无天气预报。" + message,
                "data_source": "unavailable",
            }
            for current in dates
        ]
        live_count = sum(item["data_source"] == "live_qweather" for item in forecast)
        mode = (
            "live"
            if live_count == len(dates)
            else "partial" if live_count else "unavailable"
        )
        source = {
            "live": "live_qweather",
            "partial": "partial_qweather",
            "unavailable": "unavailable",
        }[mode]
        source_note = "天气服务：和风天气 https://www.qweather.com/。" + message
        if updated_at:
            source_note += f" 预报更新时间：{updated_at}。"
        return {
            "city": city,
            "forecast": forecast,
            "data_mode": mode,
            "data_source": source,
            "error_code": error_code,
            "message": message,
            "source_note": source_note,
            "updated_at": updated_at,
        }

    @staticmethod
    def _suggestion(condition: str, high: int) -> str:
        if "雷" in condition:
            return "留意雷雨预警，避免暴露的高处和水上活动，准备室内替代方案。"
        if "雨" in condition or "雪" in condition:
            return "携带雨具，留意路面湿滑，并预留室内替代方案。"
        if high >= 30:
            return "注意补水与防晒，减少午后连续户外活动。"
        return "结合当天预报安排室内外活动，出发前再次核对天气。"

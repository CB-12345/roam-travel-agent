from __future__ import annotations

from typing import Any

from src.agents.base import BaseWorkflowAgent
from src.models import TravelRequest


class HotelSearchAgent(BaseWorkflowAgent):
    name = "HotelSearchAgent"
    system_prompt = (
        "你是酒店研究 Agent。"
        "你的职责是自主调用酒店工具生成酒店候选池，而不是让程序提前兜底生成。"
        "你必须先调用 travel_search_hotels 工具；没有工具结果时不要编造酒店。"
        "调用酒店工具时，只能使用这些参数名："
        "city, budget_min, budget_max, travelers, stay_nights, hotel_style, limit, area_hint, search_focus。"
        "不要使用 total_budget、stay_length、style 之类的别名。"
        "工具只会返回精简候选；完整地址、坐标和详情由后端保存并补全。"
        "你只需要输出 candidate_names 和 recommended_hotel_name，不要复制完整酒店对象。"
        "你必须只输出一个 JSON 对象，不要输出 Markdown、代码块、解释文字或多余前后缀。"
        "字段名、字段层级和字段类型必须完全符合用户给出的模板，不能省略字段。"
    )

    def research(
        self,
        request: TravelRequest,
        *,
        constraint_context: dict[str, Any] | None = None,
        rotation_policy: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        prompt = self._build_prompt(
            request,
            constraint_context=constraint_context or {},
            rotation_policy=rotation_policy or {},
        )
        data, diagnostics = self.run_json_with_trace(prompt, max_tool_iterations=5)
        diagnostics.update(
            {
                "agent": self.name,
                "city": request.city,
                "stay_nights": request.stay_nights,
                "target_hotel_count": (rotation_policy or {}).get("target_hotel_count", 1),
                "stage": "hotel_agent",
            }
        )
        if data:
            diagnostics["json_keys"] = list(data.keys())
            diagnostics["raw_candidate_count"] = len(data.get("candidate_names") or [])
        return data, diagnostics

    def repair(
        self,
        request: TravelRequest,
        *,
        errors: list[str],
        candidate_pool_preview: list[dict[str, Any]],
        previous_data: dict[str, Any] | None,
        constraint_context: dict[str, Any],
        rotation_policy: dict[str, Any],
        current_count: int,
        shortage_count: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        base_prompt = self._build_prompt(
            request,
            constraint_context=constraint_context,
            rotation_policy=rotation_policy,
        )
        prompt = f"""
你上一次输出的酒店研究 JSON 没有通过校验，需要重新生成。

校验错误：
{self._json(errors)}

当前已通过验真的酒店数量：{current_count}
目标酒店数量：{rotation_policy.get("target_hotel_count", 1)}
还缺酒店数量：{max(shortage_count, 0)}

当前 travel_search_hotels 工具候选池摘要：
{self._json(candidate_pool_preview)}

上一次输出的 JSON：
{self._json(previous_data or {})}

返工要求：
- 如果是 JSON 结构错误，请重新输出完整 HotelSelectionResearch JSON。
- 如果是验真错误，candidate_names 只能来自工具 candidates，不要编造酒店、地址、坐标或价格。
- 如果是数量不足，你必须继续调用 travel_search_hotels，增大 limit，并使用不同 area_hint/search_focus 扩展候选池。
- 新增酒店不能和已通过验真的酒店重复。
- recommended_hotel_name 必须来自 candidate_names。
- 如果工具调用次数已达上限但仍不足，只能输出所有已验真的不重复酒店，并在 selection_reasoning 说明无法达到目标数量。
- 仍然必须遵守用户偏好和忌讳，例如安静、少走路、夜生活、美食、预算等住宿相关约束。
- 只输出一个 JSON 对象，不要 Markdown，不要解释。

原始任务和 JSON 模板如下：
{base_prompt}
"""
        data, diagnostics = self.run_json_with_trace(prompt, max_tool_iterations=5)
        diagnostics.update(
            {
                "agent": self.name,
                "city": request.city,
                "stay_nights": request.stay_nights,
                "target_hotel_count": rotation_policy.get("target_hotel_count", 1),
                "stage": "hotel_agent_repair",
                "repair_errors": errors[:12],
            }
        )
        if data:
            diagnostics["json_keys"] = list(data.keys())
            diagnostics["raw_candidate_count"] = len(data.get("candidate_names") or [])
        return data, diagnostics

    def _build_prompt(
        self,
        request: TravelRequest,
        *,
        constraint_context: dict[str, Any],
        rotation_policy: dict[str, Any],
    ) -> str:
        target_hotel_count = int(rotation_policy.get("target_hotel_count", 1) or 1)
        tool_limit = max(target_hotel_count + 2, 3)
        interval_nights = int(rotation_policy.get("interval_nights", 2) or 2)
        return f"""
请研究这次旅行的酒店候选。

城市：{request.city}
预算范围：{request.budget_min}-{request.budget_max}
同行人数：{request.travelers}
住宿晚数：{request.stay_nights}
住宿偏好：{request.hotel_style}
节奏：{request.pace}
酒店轮换策略：每 {interval_nights} 晚换一次酒店
目标酒店数量：{target_hotel_count}

用户统一约束上下文：
{self._json(constraint_context)}

请调用：
travel_search_hotels(
  city="{request.city}",
  budget_min={request.budget_min},
  budget_max={request.budget_max},
  travelers={request.travelers},
  stay_nights={request.stay_nights},
  hotel_style="{request.hotel_style}",
  limit={tool_limit},
  area_hint="",
  search_focus="main"
)

注意：
- 必须先调用 travel_search_hotels 工具获取真实候选
- 参数名必须完全匹配工具 schema
- 不要传 total_budget
- 不要传 stay_length
- 不要传 style
- candidate_names 必须来自工具返回结果，不要编造酒店名称、地址或价格
- 如果工具候选数量足够，candidate_names 必须输出 {target_hotel_count} 个不同酒店
- 如果候选不足，必须继续换 area_hint/search_focus 调用工具扩展候选池
- 如果工具最终不足，只能输出所有可验真的不重复酒店，并在 selection_reasoning 说明不足原因
- recommended_hotel_name 必须来自 candidate_names
- 不要输出完整酒店对象；后端会按酒店名称从完整候选池补全价格、地址、坐标和 summary
- 必须遵守用户统一约束上下文；例如不想吵就避开过度夜生活区域，不想走路就优先交通方便/靠近景点集群

严格 JSON 输出规则：
- 只能输出一个 JSON 对象。
- 不要输出 Markdown、```json 代码块、解释文字或前后缀。
- 字段名必须完全一致，不能省略字段。
- 未知字符串填 ""，未知数字填 0.0，未知数组填 []。

返回 JSON：
{{
  "candidate_names": ["copy exact hotel candidate name"],
  "recommended_hotel_name": "copy exact hotel candidate name",
  "selection_reasoning": ["string"]
}}
"""

    def _json(self, value: Any) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

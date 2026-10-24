from __future__ import annotations

from typing import Any

from src.agents.base import BaseWorkflowAgent
from src.models import TravelRequest


class AttractionSearchAgent(BaseWorkflowAgent):
    name = "AttractionSearchAgent"
    system_prompt = (
        "你是旅行景点研究 Agent。你负责理解用户偏好和忌讳，自主决定要搜索哪些真实景点 POI，"
        "然后从 MCP 工具返回的候选中筛选景点。"
        "你必须使用工具获得候选，不要凭记忆编造景点、地址或坐标。"
        "你可以调用 travel_get_city_profile(city) 和 travel_search_attraction_pois(city, query, limit)。"
        "调用 travel_search_attraction_pois 时，query 必须是一个具体中文搜索词，例如：滨水公园、博物馆、美食街、亲子乐园。"
        "不要把所有偏好一次性塞进 query。"
        "工具调用有程序侧配额限制；优先覆盖不同偏好主题，而不是反复搜索同一类词。"
        "工具只会返回精简候选；完整地址和坐标由后端保存并补全。"
        "最终 selected_attractions 只能来自工具返回的 candidates，只需要复制 candidate_id 和 name，并给出分数与理由。"
        "selected_attractions 里不要重复同一个景点；长天数旅行要尽量选择足够多的不重复景点。"
        "如果候选和用户忌讳冲突，必须拒绝并在 selection_reasoning 说明。"
        "你必须只输出一个 JSON 对象，不要输出 Markdown、代码块、解释文字或多余前后缀。"
        "字段名、字段层级和字段类型必须完全符合用户给出的模板，不能省略字段。"
    )

    def research(
        self,
        request: TravelRequest,
        *,
        constraint_context: dict[str, Any] | None = None,
        target_count: int | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        attractions_per_day = {"relaxed": 1, "balanced": 2, "intense": 3}.get(request.pace, 2)
        target_count = target_count or max(request.trip_days * attractions_per_day, 3)
        prompt = self._build_prompt(
            request,
            attractions_per_day=attractions_per_day,
            target_count=target_count,
            constraint_context=constraint_context or {},
        )
        data, diagnostics = self.run_json_with_trace(prompt, max_tool_iterations=8)
        diagnostics.update(
            {
                "agent": self.name,
                "city": request.city,
                "trip_days": request.trip_days,
                "target_attraction_count": target_count,
                "stage": "attraction_agent",
            }
        )
        if data:
            diagnostics["json_keys"] = list(data.keys())
            diagnostics["raw_selected_count"] = len(data.get("selected_attractions") or [])
        else:
            diagnostics.setdefault("failure_reason", "llm_output_json_parse_failed")
        return data, diagnostics

    def repair(
        self,
        request: TravelRequest,
        *,
        errors: list[str],
        candidate_pool_preview: list[dict[str, Any]],
        previous_data: dict[str, Any] | None,
        constraint_context: dict[str, Any],
        target_count: int,
        current_count: int,
        shortage_count: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        attractions_per_day = {"relaxed": 1, "balanced": 2, "intense": 3}.get(request.pace, 2)
        base_prompt = self._build_prompt(
            request,
            attractions_per_day=attractions_per_day,
            target_count=target_count,
            constraint_context=constraint_context,
        )
        prompt = f"""
你上一次输出的景点研究 JSON 没有通过校验，需要重新生成。

校验错误：
{self._json(errors)}

当前已通过验真的景点数量：{current_count}
目标景点数量：{target_count}
还缺景点数量：{max(shortage_count, 0)}

当前工具候选池摘要：
{self._json(candidate_pool_preview)}

上一次输出的 JSON：
{self._json(previous_data or {})}

返工要求：
- 如果是 JSON 结构错误，请重新输出完整 AttractionSelectionResearch JSON。
- 如果是验真错误，只能从工具 candidates 中选择景点，不要编造景点、地址、坐标或 candidate_id。
- 如果是数量不足，你必须继续调用 travel_search_attraction_pois，使用和已有候选不同的具体 query 扩展候选池。
- 新增景点不能和已通过验真的景点重复。
- 如果工具调用次数已达上限但仍不足，只能输出所有已验真的不重复景点，并在 selection_reasoning 说明无法达到目标数量。
- 仍然必须遵守用户偏好和忌讳。
- 只输出一个 JSON 对象，不要 Markdown，不要解释。

原始任务和 JSON 模板如下：
{base_prompt}
"""
        data, diagnostics = self.run_json_with_trace(prompt, max_tool_iterations=8)
        diagnostics.update(
            {
                "agent": self.name,
                "city": request.city,
                "trip_days": request.trip_days,
                "target_attraction_count": target_count,
                "stage": "attraction_agent_repair",
                "repair_errors": errors[:12],
            }
        )
        if data:
            diagnostics["json_keys"] = list(data.keys())
            diagnostics["raw_selected_count"] = len(data.get("selected_attractions") or [])
        return data, diagnostics

    def _build_prompt(
        self,
        request: TravelRequest,
        *,
        attractions_per_day: int,
        target_count: int,
        constraint_context: dict[str, Any],
    ) -> str:
        return f"""
请为这次旅行做景点研究。

城市：{request.city}
旅行天数：{request.trip_days}
预算范围：{request.budget_min}-{request.budget_max}
同行人数：{request.travelers}
偏好：{", ".join(request.preferences) or "none"}
补充正向偏好：{request.extra_preferences or "none"}
真正忌讳/负向约束：{request.taboos or "none"}
节奏：{request.pace}
每日景点密度参考：{attractions_per_day} 个/天
目标不重复景点数：尽量接近 {target_count} 个

用户统一约束上下文：
{self._json(constraint_context)}

你可以调用：
1. travel_get_city_profile(city="{request.city}")
2. travel_search_attraction_pois(city="{request.city}", query="一个具体搜索词", limit=8)

工作要求：
- 先根据偏好、忌讳、人数、节奏设计搜索词。
- 偏好全选时，也要尽量覆盖多主题，不要只搜前一种偏好。
- 如果补充正向偏好里出现“爬山、徒步、山景、玩水、古镇”等具体兴趣，必须转成对应搜索词。
- 忌讳要分作用范围：例如“不吃辣”主要影响餐饮；“不想爬山”会影响景点。
- 对每个候选进行 Agent 打分，分数和理由由你判断。
- 只能选择工具返回过的候选；不要创造新景点。
- 工具返回给你的候选是精简版；你不要输出地址、坐标、门票等事实字段，后端会用 candidate_id 回查完整候选。
- selected_attractions 不要重复同名景点。
- 如果工具候选数量足够，selected_attractions 必须达到 {target_count} 个。
- 如果候选不足，必须继续换 query 调用工具扩展候选池，直到达到目标或工具返回调用上限。
- 如果旅行天数很多，优先扩大搜索主题覆盖，尽量给足不重复景点；候选不足时可以少选，并在 selection_reasoning 说明不足原因。
- 如果真实候选不足，可以少选，但不要幻想补齐。
- 必须遵守用户统一约束上下文，尤其是负向约束；例如不想爬山就避开明显登山/徒步/山岳强度景点。

严格 JSON 输出规则：
- 只能输出一个 JSON 对象。
- 不要输出 Markdown、```json 代码块、解释文字或前后缀。
- 字段名必须完全一致，不能省略字段。
- 未知字符串填 ""，未知数字填 0.0，未知数组填 []。

返回 JSON：
{{
  "city_overview": "short summary",
  "search_plan": [
    {{
      "query": "string",
      "reason": "why this query matches the user"
    }}
  ],
  "preference_interpretation": {{
    "positive": ["string"],
    "negative": ["string"],
    "ambiguous": ["string"]
  }},
  "selected_attractions": [
    {{
      "candidate_id": "must copy from tool candidate",
      "name": "must copy exact candidate name",
      "score": 90,
      "matched_preferences": ["string"],
      "taboo_check": "string",
      "reason": "why this candidate fits the user"
    }}
  ],
  "selection_reasoning": ["string"],
  "rejected_candidates": [
    {{
      "candidate_id": "string",
      "name": "string",
      "reason": "why rejected"
    }}
  ],
  "recommended_night_area": "string"
}}
"""

    def _json(self, value: Any) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

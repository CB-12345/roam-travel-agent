from __future__ import annotations

from typing import Any

from src.llm import LangChainRuntime


class BaseWorkflowAgent:
    name = "BaseAgent"
    system_prompt = ""

    def __init__(self, runtime: LangChainRuntime, tool_registry: Any | None = None):
        self.runtime = runtime
        self.tool_registry = tool_registry

    def run_json(self, user_prompt: str, max_tool_iterations: int = 2) -> dict[str, Any] | None:
        return self.runtime.run_json(
            name=self.name,
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            tool_registry=self.tool_registry,
            max_tool_iterations=max_tool_iterations,
        )

    def run_json_with_trace(
        self,
        user_prompt: str,
        max_tool_iterations: int = 2,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        return self.runtime.run_json_with_trace(
            name=self.name,
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            tool_registry=self.tool_registry,
            max_tool_iterations=max_tool_iterations,
        )

    def run_text(self, user_prompt: str, max_tool_iterations: int = 2) -> str:
        return self.runtime.run_simple(
            name=self.name,
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            tool_registry=self.tool_registry,
            max_tool_iterations=max_tool_iterations,
        )


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []

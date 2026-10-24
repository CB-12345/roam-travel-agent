from __future__ import annotations

from dataclasses import dataclass, field
from inspect import Parameter, signature
from typing import Any, Callable


@dataclass
class ToolParameter:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    default: Any | None = None


@dataclass
class ToolResponse:
    text: str = ""
    ok: bool = True
    code: str = ""
    message: str = ""
    stats: dict[str, Any] | None = field(default_factory=dict)
    context: dict[str, Any] | None = field(default_factory=dict)

    @classmethod
    def success(cls, text: str = "") -> "ToolResponse":
        return cls(text=text, ok=True)

    @classmethod
    def error(cls, code: str, message: str) -> "ToolResponse":
        return cls(text=message, ok=False, code=code, message=message)


class FunctionTool:
    def __init__(self, *, name: str, description: str, func: Callable[..., str]) -> None:
        self.name = name
        self.description = description
        self._func = func
        self._parameters = self._build_parameters(func)

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        result = self._func(**self._filter_parameters(parameters))
        return ToolResponse.success(text=str(result))

    def run_with_timing(self, parameters: dict[str, Any]) -> ToolResponse:
        return self.run(parameters)

    def get_parameters(self) -> list[ToolParameter]:
        return list(self._parameters)

    def validate_parameters(self, parameters: dict[str, Any]) -> bool:
        return isinstance(parameters, dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": [parameter.__dict__ for parameter in self._parameters],
        }

    def _filter_parameters(self, parameters: dict[str, Any]) -> dict[str, Any]:
        names = {parameter.name for parameter in self._parameters}
        return {key: value for key, value in parameters.items() if key in names}

    def _build_parameters(self, func: Callable[..., str]) -> list[ToolParameter]:
        parameters: list[ToolParameter] = []
        for name, parameter in signature(func).parameters.items():
            if name == "self":
                continue
            default = None if parameter.default is Parameter.empty else parameter.default
            parameters.append(
                ToolParameter(
                    name=name,
                    type=self._annotation_to_type(parameter.annotation),
                    required=parameter.default is Parameter.empty,
                    default=default,
                )
            )
        return parameters

    def _annotation_to_type(self, annotation: Any) -> str:
        if annotation in (int, "int"):
            return "integer"
        if annotation in (float, "float"):
            return "number"
        if annotation in (bool, "bool"):
            return "boolean"
        if annotation in (list, "list"):
            return "array"
        if annotation in (dict, "dict"):
            return "object"
        return "string"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def register_tool(self, tool: Any) -> None:
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Any | None:
        return self._tools.get(name)

    def get_all_tools(self) -> list[Any]:
        return list(self._tools.values())

    def list_tools(self) -> list[str]:
        return sorted(self._tools)

    def get_tools_description(self) -> str:
        lines = []
        for tool in self.get_all_tools():
            params = ", ".join(parameter.name for parameter in tool.get_parameters())
            lines.append(f"- {tool.name}({params}): {tool.description}")
        return "\n".join(lines)

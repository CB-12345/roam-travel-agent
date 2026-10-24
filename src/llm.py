from __future__ import annotations

import json
import logging
from typing import Any

from src.config import LLMSettings
from src.utils.json_utils import extract_json_object
from src.services.observability import observed_call


class LangChainRuntime:
    JSON_CORRECTION_PROMPT = (
        "你的上一条不是合法 JSON。"
        "如果要继续搜索，请只输出工具调用；如果完成，请只输出 JSON。"
        "不要输出过程说明、解释、Markdown 或其他文本。"
    )
    JSON_ONLY_CORRECTION_PROMPT = (
        "你的上一条不是合法 JSON。现在不能调用工具，请只输出最终 JSON。"
        "不要输出过程说明、解释、Markdown 或其他文本。"
    )
    TOOL_RESULT_FOLLOWUP_PROMPT = (
        "工具执行结果：\n{tool_results}\n\n"
        "请基于这些结果继续。"
        "如果还需要搜索，请只输出工具调用；如果信息已经足够，请只输出最终 JSON。"
        "不要输出过程说明。"
    )

    def __init__(self, settings: LLMSettings):
        self.settings = settings
        self._llm: Any | None = None
        self._ai_message_cls: Any | None = None
        self._human_message_cls: Any | None = None
        self._system_message_cls: Any | None = None
        self._tool_message_cls: Any | None = None
        self.load_error: str = ""
        self._load_langchain()

    @property
    def available(self) -> bool:
        key_ok = bool(self.settings.api_key and self.settings.api_key != "your-key")
        return self.settings.enabled and key_ok and self._llm is not None

    @property
    def provider_label(self) -> str:
        if not self.available:
            return "offline-demo"
        return f"{self.settings.provider}:{self.settings.model}"

    def run_simple(
        self,
        *,
        name: str,
        system_prompt: str,
        user_prompt: str,
        tool_registry: Any | None = None,
        max_tool_iterations: int = 2,
    ) -> str:
        if not self.available:
            return ""

        messages = [
            self._system_message(system_prompt),
            self._human_message(user_prompt),
        ]
        if tool_registry is None or max_tool_iterations <= 0:
            return self._response_text(self._call_model(messages))

        last_text = ""
        for _ in range(max_tool_iterations):
            response = self._invoke_tool_loop(messages, tool_registry)
            last_text = self._response_text(response)
            tool_calls = self._extract_function_tool_calls(response)
            if not tool_calls:
                return last_text

            tool_results = []
            self._append_assistant_and_tools(messages, response, last_text, tool_calls, tool_registry, tool_results)
            messages.append(
                self._human_message(
                    "工具执行结果：\n"
                    + "\n\n".join(tool_results)
                    + "\n\n请基于这些结果继续完成任务。"
                )
            )
        return last_text

    def run_json(
        self,
        *,
        name: str,
        system_prompt: str,
        user_prompt: str,
        tool_registry: Any | None = None,
        max_tool_iterations: int = 2,
    ) -> dict[str, Any] | None:
        data, _ = self.run_json_with_trace(
            name=name,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_registry=tool_registry,
            max_tool_iterations=max_tool_iterations,
        )
        return data

    def run_json_with_trace(
        self,
        *,
        name: str,
        system_prompt: str,
        user_prompt: str,
        tool_registry: Any | None = None,
        max_tool_iterations: int = 2,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if not self.available:
            text = ""
            trace: dict[str, Any] = {"strict_json_loop_enabled": False}
        elif tool_registry is not None and max_tool_iterations > 0:
            text, trace = self._run_strict_json_tool_loop(
                name=name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tool_registry=tool_registry,
                max_tool_iterations=max_tool_iterations,
            )
        else:
            text, trace = self._run_json_without_tools(
                name=name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

        data = extract_json_object(text)
        preview = text.strip().replace("\r", " ").replace("\n", " ")
        trace.update(
            {
                "llm_available": self.available,
                "provider": self.provider_label,
                "raw_output_length": len(text),
                "raw_output_preview": preview[:600],
                "json_parse_ok": data is not None,
                "failure_reason": "" if data is not None else "llm_output_json_parse_failed",
            }
        )
        self._print_agent_output(name, text, data is not None)
        return data, trace

    def _print_agent_output(self, name: str, text: str, json_parse_ok: bool) -> None:
        logging.getLogger(__name__).info("agent=%s json_parse_ok=%s output_chars=%d", name, json_parse_ok, len(text))

    def _run_strict_json_tool_loop(
        self,
        *,
        name: str,
        system_prompt: str,
        user_prompt: str,
        tool_registry: Any,
        max_tool_iterations: int,
    ) -> tuple[str, dict[str, Any]]:
        messages = [
            self._system_message(self._tool_loop_system_prompt(system_prompt, tool_registry)),
            self._human_message(user_prompt),
        ]
        trace: dict[str, Any] = {
            "strict_json_loop_enabled": True,
            "max_tool_iterations": max_tool_iterations,
            "tool_iterations_used": 0,
            "tool_calls_executed": 0,
            "tool_call_names": [],
            "json_correction_attempts": 0,
            "non_json_no_tool_outputs": [],
            "strict_json_stop_reason": "",
        }
        current_iteration = 0
        final_response = ""

        while current_iteration < max_tool_iterations:
            response = self._invoke_tool_loop(messages, tool_registry)
            response_text = self._response_text(response)
            tool_calls = self._extract_function_tool_calls(response)

            if tool_calls:
                tool_results: list[str] = []
                self._append_assistant_and_tools(
                    messages,
                    response,
                    response_text,
                    tool_calls,
                    tool_registry,
                    tool_results,
                )
                for call in tool_calls:
                    trace["tool_calls_executed"] += 1
                    trace["tool_call_names"].append(call["tool_name"])

                messages.append(
                    self._human_message(
                        self.TOOL_RESULT_FOLLOWUP_PROMPT.format(
                            tool_results="\n\n".join(tool_results)
                        )
                    )
                )
                current_iteration += 1
                trace["tool_iterations_used"] = current_iteration
                continue

            if extract_json_object(response_text) is not None:
                final_response = response_text
                trace["strict_json_stop_reason"] = "valid_json"
                break

            trace["non_json_no_tool_outputs"].append(self._preview_text(response_text))
            if trace["json_correction_attempts"] < 1:
                messages.append(self._ai_message(response_text))
                messages.append(self._human_message(self.JSON_CORRECTION_PROMPT))
                trace["json_correction_attempts"] += 1
                continue

            final_response = response_text
            trace["strict_json_stop_reason"] = "non_json_after_correction"
            break

        if current_iteration >= max_tool_iterations and not final_response:
            messages.append(
                self._human_message(
                    "工具调用次数已达上限。请基于已有工具结果只输出最终 JSON，"
                    "不要继续调用工具，不要输出解释。"
                )
            )
            final_response = self._response_text(self._call_model(messages))
            trace["strict_json_stop_reason"] = "max_tool_iterations_final_answer"
            if extract_json_object(final_response) is None:
                trace["non_json_no_tool_outputs"].append(self._preview_text(final_response))
                messages.append(self._ai_message(final_response))
                messages.append(self._human_message(self.JSON_ONLY_CORRECTION_PROMPT))
                final_response = self._response_text(self._call_model(messages))
                trace["json_correction_attempts"] += 1
                trace["strict_json_stop_reason"] = "max_tool_iterations_json_correction"

        return final_response, trace

    def _append_assistant_and_tools(
        self,
        messages: list[Any],
        response: Any,
        response_text: str,
        tool_calls: list[dict[str, Any]],
        tool_registry: Any,
        tool_results: list[str],
    ) -> None:
        if all(call.get("id") for call in tool_calls):
            messages.append(response)
            for call in tool_calls:
                result = self._execute_registry_tool(tool_registry, call["tool_name"], call["parameters"])
                tool_results.append(result)
                messages.append(self._tool_message(result, call["id"]))
            return

        messages.append(self._ai_message(response_text))
        for call in tool_calls:
            result = self._execute_registry_tool(tool_registry, call["tool_name"], call["parameters"])
            tool_results.append(result)

    def _tool_loop_system_prompt(self, system_prompt: str, tool_registry: Any) -> str:
        tools_description = ""
        try:
            tools_description = tool_registry.get_tools_description()
        except Exception:
            pass
        if tools_description:
            return f"{system_prompt}\n\nAvailable tools:\n{tools_description}"
        return system_prompt

    def _invoke_tool_loop(self, messages: list[Any], tool_registry: Any) -> Any:
        tool_schemas = self._build_tool_schemas(tool_registry)
        model = self._llm
        if tool_schemas:
            try:
                model = self._llm.bind_tools(tool_schemas, tool_choice="auto")
            except (AttributeError, NotImplementedError, TypeError):
                pass
        # A real provider failure must propagate, not trigger a duplicate request.
        return self._call_model(messages, model)

    def _call_model(self, messages: list[Any], model: Any = None) -> Any:
        return observed_call("llm", self.provider_label, lambda: (model if model is not None else self._llm).invoke(messages))

    def _build_tool_schemas(self, tool_registry: Any) -> list[dict[str, Any]]:
        try:
            tools = list(tool_registry.get_all_tools())
        except Exception:
            return []

        schemas: list[dict[str, Any]] = []
        for tool in tools:
            properties: dict[str, Any] = {}
            required: list[str] = []
            try:
                parameters = tool.get_parameters()
            except Exception:
                parameters = []
            for param in parameters:
                properties[param.name] = {
                    "type": self._json_schema_type(getattr(param, "type", "string")),
                    "description": getattr(param, "description", "") or "",
                }
                default = getattr(param, "default", None)
                if default is not None:
                    properties[param.name]["default"] = default
                if getattr(param, "required", True):
                    required.append(param.name)
            schema: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                    },
                },
            }
            if required:
                schema["function"]["parameters"]["required"] = required
            schemas.append(schema)
        return schemas

    def _json_schema_type(self, param_type: str) -> str:
        normalized = (param_type or "").lower()
        if normalized in {"string", "number", "integer", "boolean", "array", "object"}:
            return normalized
        return "string"

    def _response_text(self, response: Any) -> str:
        if hasattr(response, "content"):
            return self._content_to_text(response.content)
        return str(response)

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or item))
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(content or "")

    def _extract_function_tool_calls(self, response: Any) -> list[dict[str, Any]]:
        raw_calls = getattr(response, "tool_calls", None) or []
        if not raw_calls:
            raw_calls = getattr(response, "additional_kwargs", {}).get("tool_calls", []) or []

        calls: list[dict[str, Any]] = []
        for raw_call in raw_calls:
            parsed = self._parse_tool_call(raw_call)
            if parsed is not None:
                calls.append(parsed)
        return calls

    def _parse_tool_call(self, raw_call: Any) -> dict[str, Any] | None:
        if isinstance(raw_call, dict):
            function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
            name = raw_call.get("name") or raw_call.get("tool_name") or function.get("name")
            args = raw_call.get("args") or raw_call.get("parameters") or function.get("arguments") or {}
            call_id = raw_call.get("id") or raw_call.get("tool_call_id") or ""
        else:
            function = getattr(raw_call, "function", None)
            name = getattr(raw_call, "name", "") or getattr(function, "name", "")
            args = getattr(raw_call, "args", None) or getattr(function, "arguments", None) or {}
            call_id = getattr(raw_call, "id", "")

        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except Exception:
                args = {}
        if not isinstance(args, dict) or not name:
            return None
        return {
            "id": str(call_id or ""),
            "tool_name": str(name),
            "parameters": args,
            "raw": raw_call,
        }

    def _execute_registry_tool(self, tool_registry: Any, tool_name: str, parameters: dict[str, Any]) -> str:
        resolved_name = self._resolve_tool_name(tool_registry, tool_name)
        try:
            tool = tool_registry.get_tool(resolved_name)
        except Exception:
            tool = None
        if tool is None:
            return json.dumps(
                {
                    "tool_error": True,
                    "reason": "tool_not_found",
                    "tool_name": tool_name,
                },
                ensure_ascii=False,
            )

        runner = getattr(tool, "run_with_timing", None) or getattr(tool, "run")
        response = observed_call("tool", resolved_name, lambda: runner(parameters))
        if hasattr(response, "text"):
            return str(response.text)
        return str(response)

    def _resolve_tool_name(self, tool_registry: Any, tool_name: str) -> str:
        try:
            if tool_registry.get_tool(tool_name) is not None:
                return tool_name
            prefixed = f"travel_{tool_name}"
            if tool_registry.get_tool(prefixed) is not None:
                return prefixed
        except Exception:
            pass
        return tool_name

    def _run_json_without_tools(
        self,
        *,
        name: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, dict[str, Any]]:
        messages = [
            self._system_message(system_prompt),
            self._human_message(user_prompt),
        ]
        trace: dict[str, Any] = {
            "strict_json_loop_enabled": False,
            "json_correction_attempts": 0,
            "non_json_no_tool_outputs": [],
            "strict_json_stop_reason": "plain_json_call",
        }
        text = self._response_text(self._call_model(messages))
        if extract_json_object(text) is not None:
            return text, trace

        trace["non_json_no_tool_outputs"].append(self._preview_text(text))
        messages.append(self._ai_message(text))
        messages.append(self._human_message(self.JSON_ONLY_CORRECTION_PROMPT))
        corrected = self._response_text(self._call_model(messages))
        trace["json_correction_attempts"] = 1
        trace["strict_json_stop_reason"] = "plain_json_correction"
        return corrected, trace

    def _preview_text(self, text: str) -> str:
        return text.strip().replace("\r", " ").replace("\n", " ")[:300]

    def run_reflection(
        self,
        *,
        name: str,
        task: str,
        custom_prompts: dict[str, str] | None = None,
        max_iterations: int = 2,
    ) -> str:
        if not self.available:
            return ""

        prompt_parts = []
        if custom_prompts:
            prompt_parts.append(json.dumps(custom_prompts, ensure_ascii=False))
        prompt_parts.append(task)
        messages = [
            self._system_message("You are a careful assistant. Complete the task directly."),
            self._human_message("\n\n".join(prompt_parts)),
        ]
        return self._response_text(self._call_model(messages))

    def build_context(
        self,
        *,
        user_query: str,
        system_instructions: str,
        packets: list[tuple[str, dict[str, Any]]],
        max_tokens: int = 5000,
    ) -> str:
        body = "\n\n".join(content for content, _ in packets if content.strip())
        return f"{system_instructions}\n\nCurrent task: {user_query}\n\n{body}"

    def _system_message(self, content: str) -> Any:
        return self._system_message_cls(content=content)

    def _human_message(self, content: str) -> Any:
        return self._human_message_cls(content=content)

    def _ai_message(self, content: str) -> Any:
        return self._ai_message_cls(content=content)

    def _tool_message(self, content: str, tool_call_id: str) -> Any:
        return self._tool_message_cls(content=content, tool_call_id=tool_call_id)

    def _load_langchain(self) -> None:
        key_ok = bool(self.settings.api_key and self.settings.api_key != "your-key")
        if not key_ok:
            return
        try:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
            from langchain_openai import ChatOpenAI

            self._llm = ChatOpenAI(
                api_key=self.settings.api_key,
                model=self.settings.model,
                base_url=self.settings.base_url or None,
                temperature=self.settings.temperature,
                timeout=self.settings.timeout_seconds,
            )
            self._ai_message_cls = AIMessage
            self._human_message_cls = HumanMessage
            self._system_message_cls = SystemMessage
            self._tool_message_cls = ToolMessage
        except Exception as exc:
            self._llm = None
            self._ai_message_cls = None
            self._human_message_cls = None
            self._system_message_cls = None
            self._tool_message_cls = None
            self.load_error = str(exc)

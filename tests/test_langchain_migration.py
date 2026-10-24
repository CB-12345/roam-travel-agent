from __future__ import annotations

from langchain_core.messages import AIMessage

from src.config import LLMSettings
from src.llm import LangChainRuntime
from src.mcp.tooling import ToolRegistry
from src.mcp.travel_backend import TravelDataBackend
from src.mcp.travel_tool import TravelBackendTool


def test_travel_tools_register_with_local_tooling() -> None:
    registry = ToolRegistry()
    travel_tool = TravelBackendTool(TravelDataBackend())

    for tool in travel_tool.get_expanded_tools():
        registry.register_tool(tool)

    assert registry.list_tools() == [
        "travel_estimate_local_transport",
        "travel_geocode_city",
        "travel_get_city_profile",
        "travel_get_weather_forecast",
        "travel_search_attraction_pois",
        "travel_search_attractions",
        "travel_search_hotels",
        "travel_search_restaurants",
    ]


def test_langchain_tool_schema_keeps_existing_tool_names() -> None:
    registry = ToolRegistry()
    travel_tool = TravelBackendTool(TravelDataBackend())
    for tool in travel_tool.get_expanded_tools():
        registry.register_tool(tool)

    runtime = LangChainRuntime(
        LLMSettings(
            enabled=False,
            provider="test",
            model="test-model",
            api_key="your-key",
            base_url="",
            temperature=0.0,
            timeout_seconds=1,
        )
    )

    schemas = runtime._build_tool_schemas(registry)
    names = [schema["function"]["name"] for schema in schemas]
    hotel_schema = next(schema for schema in schemas if schema["function"]["name"] == "travel_search_hotels")

    assert "travel_search_hotels" in names
    assert "travel_search_attraction_pois" in names
    assert "city" in hotel_schema["function"]["parameters"]["required"]
    assert hotel_schema["function"]["parameters"]["properties"]["limit"]["type"] == "integer"


def test_langchain_tool_calls_are_parsed() -> None:
    runtime = LangChainRuntime(
        LLMSettings(
            enabled=False,
            provider="test",
            model="test-model",
            api_key="your-key",
            base_url="",
            temperature=0.0,
            timeout_seconds=1,
        )
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "travel_search_attraction_pois",
                "args": {"city": "广州", "query": "博物馆", "limit": 2},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )

    calls = runtime._extract_function_tool_calls(message)

    assert calls == [
        {
            "id": "call-1",
            "tool_name": "travel_search_attraction_pois",
            "parameters": {"city": "广州", "query": "博物馆", "limit": 2},
            "raw": {
                "name": "travel_search_attraction_pois",
                "args": {"city": "广州", "query": "博物馆", "limit": 2},
                "id": "call-1",
                "type": "tool_call",
            },
        }
    ]

from __future__ import annotations

from fastmcp import FastMCP

from src.mcp.travel_backend import TravelDataBackend


def build_travel_mcp_server(backend: TravelDataBackend | None = None) -> FastMCP:
    data_backend = backend or TravelDataBackend()
    server = FastMCP("TravelAssistantServer")

    @server.tool()
    def get_city_profile(city: str) -> dict:
        """Return a short city profile and recommended night area."""
        return data_backend.get_city_profile(city)

    @server.tool()
    def search_attractions(
        city: str,
        preferences: str = "",
        travelers: int = 1,
        daily_budget: float = 500.0,
        target_count: int = 6,
        budget: str = "",
        pace: str = "",
    ) -> dict:
        """Search attractions that match the travel preferences and budget."""
        if budget and not daily_budget:
            try:
                daily_budget = float(str(budget).split("-")[-1])
            except Exception:
                pass
        return data_backend.search_attractions(
            city,
            preferences,
            travelers,
            daily_budget,
            target_count=target_count,
            budget=budget,
            pace=pace,
        )

    @server.tool()
    def search_attraction_pois(city: str, query: str, limit: int = 6) -> dict:
        """Search raw attraction POI candidates by one concrete keyword chosen by the agent."""
        return data_backend.search_attraction_pois(city=city, query=query, limit=limit)

    @server.tool()
    def search_hotels(
        city: str,
        budget_min: float = 0,
        budget_max: float = 0,
        travelers: int = 2,
        stay_nights: int = 1,
        hotel_style: str = "comfort",
        limit: int = 3,
        area_hint: str = "",
        search_focus: str = "main",
        total_budget: str = "",
        stay_length: str = "",
        style: str = "",
    ) -> dict:
        """Search hotel candidates using total budget and stay length."""
        return data_backend.search_hotels(
            city,
            budget_min,
            budget_max,
            travelers,
            stay_nights,
            hotel_style,
            limit=limit,
            area_hint=area_hint,
            search_focus=search_focus,
            total_budget=total_budget,
            stay_length=stay_length,
            style=style,
        )

    @server.tool()
    def search_restaurants(
        city: str,
        anchor: str = "",
        preferences: str = "",
        budget_hint: float = 0,
        travelers: int = 2,
        radius_m: int = 2000,
    ) -> dict:
        """Search restaurant candidates around a city area or anchor POI."""
        return data_backend.search_restaurants(
            city=city,
            anchor=anchor,
            preferences=preferences,
            budget_hint=budget_hint,
            travelers=travelers,
            radius_m=radius_m,
        )

    @server.tool()
    def get_weather_forecast(city: str, start_date: str, end_date: str) -> dict:
        """Return a weather forecast for the trip date range."""
        return data_backend.get_weather_forecast(city, start_date, end_date)

    @server.tool()
    def estimate_local_transport(
        city: str,
        origin_name: str,
        destination_name: str,
        mode: str = "public_transit",
        transit_preference: str = "recommended",
    ) -> dict:
        """Estimate city-level transport distance, duration, and taxi cost."""
        return data_backend.estimate_local_transport(city, origin_name, destination_name, mode, transit_preference)

    @server.tool()
    def geocode_city(city: str) -> dict:
        """Geocode a city using AMap when available."""
        return data_backend.geocode_city(city) or {}

    return server

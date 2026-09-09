"""Async client for the BuildSim REST API."""

import httpx
from typing import Any


class BuildSimClient:
    """Thin async wrapper around the BuildSim HTTP API."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # -- helpers --

    async def _get(self, path: str, **params) -> Any:
        r = await self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, data: Any) -> Any:
        r = await self._client.post(path, json=data)
        r.raise_for_status()
        return r.json() if r.content else None

    async def _put(self, path: str, data: Any) -> Any:
        r = await self._client.put(path, json=data)
        r.raise_for_status()
        return r.json() if r.content else None

    async def _delete(self, path: str) -> None:
        r = await self._client.delete(path)
        r.raise_for_status()

    # -- health --

    async def health(self) -> dict:
        return await self._get("/healthz")

    # -- building --

    async def get_building(self) -> dict:
        return await self._get("/api/building")

    async def get_floor(self, level: str) -> dict:
        return await self._get(f"/api/building/floors/{level}")

    async def get_cross_floor_edges(self) -> list:
        return await self._get("/api/building/cross-floor-edges")

    # -- graph --

    async def get_graph(self, level: str | None = None) -> dict:
        params = {"level": level} if level else {}
        return await self._get("/api/graph", **params)

    async def get_route(self, start: int, end: int, level: str | None = None) -> dict:
        params = {"start": start, "end": end}
        if level:
            params["level"] = level
        return await self._get("/api/graph/route", **params)

    # -- equipment --

    async def create_equipment(self, equipment: dict) -> dict:
        return await self._post("/api/equipment", equipment)

    async def bulk_create_equipment(self, equipment_list: list[dict]) -> Any:
        return await self._post("/api/equipment/bulk", equipment_list)

    async def list_equipment(self) -> list[dict]:
        return await self._get("/api/equipment")

    async def get_equipment(self, equipment_id: str) -> dict:
        return await self._get(f"/api/equipment/{equipment_id}")

    async def update_equipment(self, equipment_id: str, data: dict) -> dict:
        return await self._put(f"/api/equipment/{equipment_id}", data)

    async def delete_equipment(self, equipment_id: str) -> None:
        await self._delete(f"/api/equipment/{equipment_id}")

    async def notify_equipment(self, data: dict) -> Any:
        return await self._post("/api/equipment/notify", data)

    # -- sensors --

    async def add_sensor(self, equipment_id: str, sensor: dict) -> dict:
        return await self._post(f"/api/equipment/{equipment_id}/sensors", sensor)

    async def list_sensors(self, equipment_id: str) -> list[dict]:
        return await self._get(f"/api/equipment/{equipment_id}/sensors")

    async def get_sensor(self, sensor_id: str) -> dict:
        return await self._get(f"/api/sensors/{sensor_id}")

    async def set_sensor_value(self, sensor_id: str, value: str, data_type: str = "text") -> Any:
        return await self._put(f"/api/sensors/{sensor_id}/value", {
            "value": value,
            "data_type": data_type,
        })

    # -- actuators --

    async def add_actuator(self, equipment_id: str, actuator: dict) -> dict:
        return await self._post(f"/api/equipment/{equipment_id}/actuators", actuator)

    async def list_actuators(self, equipment_id: str) -> list[dict]:
        return await self._get(f"/api/equipment/{equipment_id}/actuators")

    async def get_actuator(self, actuator_id: str) -> dict:
        return await self._get(f"/api/actuators/{actuator_id}")

    async def set_actuator_state(self, actuator_id: str, state: str) -> Any:
        return await self._put(f"/api/actuators/{actuator_id}/state", {"state": state})

    # -- visualization --

    async def get_room_layers(self) -> list[dict]:
        return await self._get("/api/room-layers")

    async def set_room_layers(self, layers: list[dict]) -> Any:
        return await self._put("/api/room-layers", layers)

    async def get_effects(self) -> list[dict]:
        return await self._get("/api/effects")

    async def set_effects(self, effects: list[dict]) -> Any:
        return await self._put("/api/effects", effects)

    async def get_entities(self) -> list[dict]:
        return await self._get("/api/entities")

    async def set_entities(self, entities: list[dict]) -> Any:
        return await self._put("/api/entities", entities)

    async def get_room_appearance(self) -> list[dict]:
        return await self._get("/api/room-appearance")

    async def set_room_appearance(self, appearance: list[dict]) -> Any:
        return await self._put("/api/room-appearance", appearance)

    async def get_alerts(self) -> list[dict]:
        return await self._get("/api/alerts")

    async def set_alerts(self, alerts: list[dict]) -> Any:
        return await self._put("/api/alerts", alerts)

    async def get_doors(self) -> list[dict]:
        return await self._get("/api/doors")

    async def set_doors(self, doors: list[dict]) -> Any:
        return await self._put("/api/doors", doors)

    # -- occupancy & coverage --

    async def get_occupancy(self) -> Any:
        return await self._get("/api/occupancy")

    async def set_occupancy(self, data: Any) -> Any:
        return await self._put("/api/occupancy", data)

    async def get_coverage(self) -> Any:
        return await self._get("/api/coverage")

    async def set_coverage(self, data: Any) -> Any:
        return await self._put("/api/coverage", data)

    # -- sessions --

    async def list_sessions(self) -> list[dict]:
        return await self._get("/api/sessions")

    async def create_session(self, data: dict) -> dict:
        return await self._post("/api/sessions", data)

    async def get_session(self, session_id: str) -> dict:
        return await self._get(f"/api/sessions/{session_id}")

    async def delete_session(self, session_id: str) -> None:
        await self._delete(f"/api/sessions/{session_id}")

    async def set_session_highlights(self, session_id: str, highlights: list[dict]) -> Any:
        return await self._put(f"/api/sessions/{session_id}/highlights", highlights)

    async def set_session_route(self, session_id: str, route: dict) -> Any:
        return await self._put(f"/api/sessions/{session_id}/route", route)

    async def set_session_coverage(self, session_id: str, coverage: Any) -> Any:
        return await self._put(f"/api/sessions/{session_id}/coverage", coverage)

    async def set_session_occupancy(self, session_id: str, occupancy: Any) -> Any:
        return await self._put(f"/api/sessions/{session_id}/occupancy", occupancy)

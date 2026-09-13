"""Fire spread prediction using spatial room adjacency."""

import math
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RoomRisk:
    room: str
    room_id: int
    level: str
    distance: float
    risk_score: float


class FireSpreadPredictor:

    def __init__(self, spread_radius=50.0):
        self.spread_radius = spread_radius
        self._rooms = {}
        self._room_positions = {}

    def load_graph(self, graph_data):
        for node in graph_data.get("nodes", []):
            self._rooms[node["name"]] = node["id"]
            self._room_positions[node["name"]] = (node["x"], node["y"])
        logger.info(f"Loaded {len(self._rooms)} rooms")

    def _distance(self, room_a, room_b):
        if room_a not in self._room_positions or room_b not in self._room_positions:
            return float("inf")
        ax, ay = self._room_positions[room_a]
        bx, by = self._room_positions[room_b]
        return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)

    def predict_spread(self, fire_room, fire_level, temperature=None, elapsed=0):
        if fire_room not in self._room_positions:
            return []

        at_risk = []
        for room_name in self._room_positions:
            if room_name == fire_room:
                continue
            dist = self._distance(fire_room, room_name)
            if dist > self.spread_radius:
                continue

            distance_factor = 1.0 - (dist / self.spread_radius)
            time_factor = min(1.0, elapsed / 300.0)
            temp_factor = 0.0
            if temperature and temperature > 30:
                temp_factor = min(0.3, (temperature - 30) / 50.0)

            risk = distance_factor * 0.6 + time_factor * 0.3 + temp_factor * 0.1
            risk = max(0.0, min(1.0, risk))

            if risk > 0.1:
                at_risk.append(RoomRisk(
                    room=room_name,
                    room_id=self._rooms[room_name],
                    level=fire_level,
                    distance=round(dist, 1),
                    risk_score=round(risk, 3),
                ))

        at_risk.sort(key=lambda r: r.risk_score, reverse=True)
        return at_risk

    def get_room_id(self, room_name):
        return self._rooms.get(room_name)

    def get_neighbors(self, room_name, radius=None):
        radius = radius or self.spread_radius
        neighbors = []
        for other in self._room_positions:
            if other == room_name:
                continue
            dist = self._distance(room_name, other)
            if dist <= radius:
                neighbors.append((other, round(dist, 1)))
        neighbors.sort(key=lambda x: x[1])
        return neighbors

"""Occupancy simulator — weekday schedule with per-room variation.

Drives the evacuation part: occupants need to be tracked so the agent
knows how many people are in danger and where to direct them.
"""

import logging
import random
from datetime import datetime

logger = logging.getLogger(__name__)

# Room type → max occupancy
ROOM_CAPACITY = {
    "room": 30,
    "corridor": 5,
}


class OccupancySimulator:
    """Generates realistic occupancy patterns for building rooms.

    Weekday schedule:
    - 07:00-09:00: arrivals
    - 09:00-11:30: morning sessions
    - 11:30-13:00: lunch (occupancy drops)
    - 13:00-16:00: afternoon sessions
    - 16:00-18:00: departures
    """

    def __init__(self, room_types: dict[str, str] = None):
        self.room_types = room_types or {}
        self._base_occupancy: dict[str, int] = {}

    def set_room_types(self, room_types: dict[str, str]):
        """room_types: {room_name: 'room' or 'corridor'}"""
        self.room_types = room_types

    def get_occupancy(self, room: str, sim_hour: float = None) -> int:
        """Get occupancy for a room at a given hour (0-24).

        If sim_hour is None, uses current wall clock time.
        """
        if sim_hour is None:
            sim_hour = datetime.now().hour + datetime.now().minute / 60.0

        room_type = self.room_types.get(room, "room")
        capacity = ROOM_CAPACITY.get(room_type, 20)

        # Base schedule factor (0-1)
        factor = self._schedule_factor(sim_hour)

        # Room type adjustment
        if room_type == "corridor":
            factor *= 0.3  # Corridors have transient occupancy

        # Per-room variation (consistent for same room name)
        seed = hash(room) % 1000
        rng = random.Random(seed)
        room_factor = rng.uniform(0.3, 1.0)

        # Randomness per call
        noise = random.gauss(0, 0.1)

        occupancy = int(capacity * factor * room_factor + noise)
        return max(0, min(capacity, occupancy))

    def _schedule_factor(self, hour: float) -> float:
        """Returns 0-1 occupancy factor based on time of day."""
        if hour < 6:
            return 0.0
        elif hour < 7:
            return 0.05
        elif hour < 9:
            # Arrival ramp: 7-9
            t = (hour - 7) / 2.0
            return 0.1 + 0.7 * t
        elif hour < 11.5:
            # Morning sessions
            return 0.8 + random.uniform(-0.05, 0.05)
        elif hour < 13:
            # Lunch drop
            return 0.3 + random.uniform(-0.05, 0.05)
        elif hour < 16:
            # Afternoon sessions
            return 0.7 + random.uniform(-0.05, 0.05)
        elif hour < 18:
            # Departure ramp: 16-18
            t = (hour - 16) / 2.0
            return 0.7 * (1.0 - t)
        else:
            return 0.02

    def get_all_occupancy(self, rooms: list[str], sim_hour: float = None) -> dict[str, int]:
        """Get occupancy for all rooms at once."""
        return {room: self.get_occupancy(room, sim_hour) for room in rooms}

    def get_total_occupancy(self, rooms: list[str], sim_hour: float = None) -> int:
        """Total people in the building."""
        return sum(self.get_all_occupancy(rooms, sim_hour).values())

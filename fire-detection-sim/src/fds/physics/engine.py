"""Physics engine — the ground truth.

Computes temperature, smoke density, and CO concentration per room.
Models heat propagation, smoke diffusion, occupant heat gain, wall loss.
Sprinklers are mechanical: a glass bulb breaks at 68C and water flows.
No electronic command activates them. Once active, they stay active
until the fire is extinguished (bulb cannot re-seal).
This is the ONLY source of truth. Everything else samples from it.
"""

import logging
import math
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Rooms whose centroids are closer than this (graph units, ~metres) share a wall and
# exchange smoke/heat. 30 keeps A109 -> A108/A110 (24-28) and excludes A109 -> A323 (45,
# across a courtyard). Rooms joined by a graph edge (door/opening) are neighbours regardless.
NEIGHBOR_RADIUS = float(os.getenv("NEIGHBOR_RADIUS", "30"))


@dataclass
class RoomState:
    """Ground truth state for one room."""

    name: str
    level: str
    temperature: float = 21.0
    smoke: float = 0.0
    co: float = 0.0
    occupancy: int = 0
    on_fire: bool = False
    fire_intensity: float = 0.0
    sprinkler_active: bool = False


@dataclass
class FireEvent:
    room: str
    start_time: float
    intensity: float = 1.0


class PhysicsEngine:
    """Simulates building physics across all rooms.

    Stepped on a fixed tick. All values are ground truth.
    """

    def __init__(
        self,
        outdoor_temp: float = -5.0,
        wall_loss_rate: float = 0.002,
        fire_heat_rate: float = 0.8,
        smoke_generation_rate: float = 2.0,
        co_generation_rate: float = 5.0,
        smoke_decay_rate: float = 0.02,
        co_decay_rate: float = 0.01,
        sprinkler_effectiveness: float = 0.85,
        sprinkler_activation_temp: float = 68.0,
    ):
        self.outdoor_temp = outdoor_temp
        self.wall_loss_rate = wall_loss_rate
        self.fire_heat_rate = fire_heat_rate
        self.smoke_generation_rate = smoke_generation_rate
        self.co_generation_rate = co_generation_rate
        self.smoke_decay_rate = smoke_decay_rate
        self.co_decay_rate = co_decay_rate
        self.sprinkler_effectiveness = sprinkler_effectiveness
        self.sprinkler_activation_temp = sprinkler_activation_temp

        self.rooms: dict[str, RoomState] = {}
        self.neighbors: dict[str, list[tuple[str, float]]] = {}
        self.fires: list[FireEvent] = []
        self._start_time = time.time()
        self._tick_count = 0
        self._sim_time = 0.0

    def add_room(self, name, level, initial_temp=21.0):
        self.rooms[name] = RoomState(name=name, level=level, temperature=initial_temp)
        if name not in self.neighbors:
            self.neighbors[name] = []

    def set_neighbors(self, room, neighbor_list):
        """neighbor_list: [(room_name, distance), ...]"""
        self.neighbors[room] = neighbor_list

    def start_fire(self, room, intensity=1.0):
        if room not in self.rooms:
            logger.warning(f"Cannot start fire: room {room} not in engine")
            return
        event = FireEvent(room=room, start_time=self._sim_time, intensity=intensity)
        self.fires.append(event)
        self.rooms[room].on_fire = True
        self.rooms[room].fire_intensity = intensity
        logger.info(f"FIRE STARTED in {room} (intensity={intensity})")

    def stop_fire(self, room):
        """Stop a fire. Also resets the sprinkler head (replaced after event)."""
        self.fires = [f for f in self.fires if f.room != room]
        if room in self.rooms:
            self.rooms[room].on_fire = False
            self.rooms[room].fire_intensity = 0.0
            self.rooms[room].sprinkler_active = False
        logger.info(f"Fire stopped in {room}")

    def set_sprinkler(self, room, active):
        """Force sprinkler state. Only for tests -- real sprinklers are mechanical
        and activate automatically in step() when temperature >= activation_temp."""
        if room in self.rooms:
            self.rooms[room].sprinkler_active = active

    def set_occupancy(self, room, count):
        if room in self.rooms:
            self.rooms[room].occupancy = count

    def get_room(self, room) -> RoomState | None:
        return self.rooms.get(room)

    def get_all_rooms(self) -> dict[str, RoomState]:
        return dict(self.rooms)

    def elapsed(self):
        return self._sim_time

    def step(self, dt=2.0):
        """Advance the simulation by dt seconds."""
        self._tick_count += 1
        self._sim_time += dt

        for name, room in self.rooms.items():
            # --- Mechanical sprinkler (glass bulb breaks at activation temp) ---
            if not room.sprinkler_active and room.temperature >= self.sprinkler_activation_temp:
                room.sprinkler_active = True
                logger.info(
                    f"SPRINKLER ACTIVATED (mechanical) in {name}: "
                    f"temp={room.temperature:.1f}C >= {self.sprinkler_activation_temp}C"
                )

            # --- Temperature ---
            # Wall heat loss
            delta_wall = self.wall_loss_rate * (room.temperature - self.outdoor_temp) * dt
            # Occupant heat gain (~0.1 C per person per step)
            heat_occupants = 0.001 * room.occupancy * dt

            # Fire heat gain
            heat_fire = 0.0
            if room.on_fire:
                elapsed = self._sim_time - next(
                    (f.start_time for f in self.fires if f.room == name), self._sim_time
                )
                ramp = 1.0 / (1.0 + math.exp(-0.1 * (elapsed - 20)))
                heat_fire = self.fire_heat_rate * room.fire_intensity * ramp * dt
                if room.sprinkler_active:
                    heat_fire *= 1.0 - self.sprinkler_effectiveness

            sprinkler_cooling = 0.0
            if room.sprinkler_active:
                # Water cools the room toward ambient: ~0.5C per step
                sprinkler_cooling = 0.5 * dt * max(0.0, (room.temperature - 21.0) / 50.0)

            room.temperature += heat_fire + heat_occupants - delta_wall - sprinkler_cooling

            # --- Smoke ---
            if room.on_fire:
                smoke_gen = self.smoke_generation_rate * room.fire_intensity * dt
                if room.sprinkler_active:
                    smoke_gen *= 0.3
                room.smoke += smoke_gen
            room.smoke = max(0.0, room.smoke - self.smoke_decay_rate * room.smoke * dt)

            # --- CO ---
            if room.on_fire:
                co_gen = self.co_generation_rate * room.fire_intensity * dt
                if room.sprinkler_active:
                    co_gen *= 0.2
                room.co += co_gen
            room.co = max(0.0, room.co - self.co_decay_rate * room.co * dt)

        # --- Propagation to neighbors ---
        propagation = {}
        for name, room in self.rooms.items():
            if room.on_fire:
                for neighbor_name, distance in self.neighbors.get(name, []):
                    if neighbor_name in self.rooms:
                        factor = max(0.0, 1.0 - (distance / 60.0))
                        if neighbor_name not in propagation:
                            propagation[neighbor_name] = {"temp": 0, "smoke": 0, "co": 0}
                        propagation[neighbor_name]["temp"] += (
                            0.15 * factor * room.fire_intensity * dt
                        )
                        propagation[neighbor_name]["smoke"] += 0.1 * factor * room.smoke * dt
                        propagation[neighbor_name]["co"] += 0.05 * factor * room.co * dt

        for name, deltas in propagation.items():
            self.rooms[name].temperature += deltas["temp"]
            self.rooms[name].smoke += deltas["smoke"]
            self.rooms[name].co += deltas["co"]

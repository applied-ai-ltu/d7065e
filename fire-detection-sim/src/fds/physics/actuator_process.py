"""Actuator processes — travel toward commanded state.

Actuators don't jump to the target instantly. They travel at a physical rate
and can be stuck or slow. They report achieved state back to BuildSim.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from fds.buildsim.client import BuildSimClient
from fds.physics.engine import PhysicsEngine

logger = logging.getLogger(__name__)


@dataclass
class ActuatorConfig:
    actuator_id: str
    equipment_id: str
    room: str
    level: str
    actuator_type: str
    travel_rate: float = 1.0  # units per second
    stuck_probability: float = 0.01


class ActuatorProcess:
    """Simulates physical actuator behavior with travel time and failure modes."""

    def __init__(self, config: ActuatorConfig, engine: PhysicsEngine):
        self.config = config
        self.engine = engine
        self._commanded_state = "off"
        self._current_state = "off"
        self._current_value = 0.0
        self._target_value = 0.0
        self._is_stuck = False
        self._running = False

    async def register(self, bs: BuildSimClient):
        try:
            await bs.get_equipment(self.config.equipment_id)
        except Exception:
            await bs.create_equipment({
                "id": self.config.equipment_id,
                "name": f"{self.config.actuator_type} ({self.config.room})",
                "type": "actuator",
                "category": "fire_response",
                "level": self.config.level,
                "room": self.config.room,
                "status": "running",
                "sensors": [],
                "actuators": [{
                    "id": self.config.actuator_id,
                    "name": self.config.actuator_type.replace("_", " ").capitalize(),
                    "type": self.config.actuator_type,
                    "state": self._current_state,
                }],
            })
            logger.info(f"Registered actuator: {self.config.actuator_id}")

    def command(self, state):
        """Receive a command. The actuator will travel toward it."""
        old = self._commanded_state
        self._commanded_state = state

        if self.config.actuator_type == "sprinkler":
            self._target_value = 1.0 if state == "active" else 0.0
        elif self.config.actuator_type == "fire_door":
            self._target_value = {"closed_locked": 0.0, "closed_unlocked": 0.5, "open": 1.0}.get(state, 0.0)

        logger.info(f"Actuator {self.config.actuator_id}: commanded {old} -> {state}")

    async def run(self, bs: BuildSimClient, interval: float = 1.0):
        """Main actuator loop: travel toward commanded state, report achieved state."""
        await self.register(bs)
        self._running = True
        logger.info(f"Actuator {self.config.actuator_id} started")

        while self._running:
            import random

            # Check for stuck
            if not self._is_stuck and random.random() < self.config.stuck_probability:
                self._is_stuck = True
                logger.warning(f"Actuator {self.config.actuator_id} is STUCK")

            if self._is_stuck:
                # Small chance of recovery
                if random.random() < 0.02:
                    self._is_stuck = False
                    logger.info(f"Actuator {self.config.actuator_id} recovered from stuck")
            else:
                # Travel toward target
                diff = self._target_value - self._current_value
                if abs(diff) > 0.01:
                    step = min(abs(diff), self.config.travel_rate * interval)
                    self._current_value += step if diff > 0 else -step

            # Determine state from value
            if self.config.actuator_type == "sprinkler":
                self._current_state = "active" if self._current_value > 0.5 else "off"
                self.engine.set_sprinkler(self.config.room, self._current_value > 0.5)
            elif self.config.actuator_type == "fire_door":
                if self._current_value < 0.25:
                    self._current_state = "closed_locked"
                elif self._current_value < 0.75:
                    self._current_state = "closed_unlocked"
                else:
                    self._current_state = "open"

            # Report achieved state to BuildSim
            try:
                await bs.set_actuator_state(self.config.actuator_id, self._current_state)
            except Exception as e:
                logger.warning(f"Failed to report actuator state: {e}")

            await asyncio.sleep(interval)

    def stop(self):
        self._running = False

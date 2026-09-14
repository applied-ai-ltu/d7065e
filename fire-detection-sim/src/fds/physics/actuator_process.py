"""Actuator processes — poll BuildSim for commands, travel toward target, report state.

Reference architecture:
- Agent writes commanded state to BuildSim (acts as command)
- Actuator polls BuildSim, detects command, travels toward it
- Actuator reports achieved state back to BuildSim
- Physics engine reads achieved state from BuildSim
"""

import asyncio
import logging
import random
from dataclasses import dataclass

from fds.buildsim.client import BuildSimClient

logger = logging.getLogger(__name__)


@dataclass
class ActuatorConfig:
    actuator_id: str
    equipment_id: str
    room: str
    level: str
    actuator_type: str
    travel_rate: float = 0.5
    stuck_probability: float = 0.01


class ActuatorProcess:
    """Polls BuildSim for commands, travels toward target, reports achieved state."""

    def __init__(self, config: ActuatorConfig):
        self.config = config
        self._current_value = 0.0
        self._target_value = 0.0
        self._last_known_command = "off"
        self._is_stuck = False
        self._running = False

    def _state_to_value(self, state):
        if self.config.actuator_type == "sprinkler":
            return 1.0 if state == "active" else 0.0
        elif self.config.actuator_type == "fire_door":
            return {"closed_locked": 0.0, "closed_unlocked": 0.5, "open": 1.0}.get(state, 0.0)
        return 0.0

    def _value_to_state(self):
        if self.config.actuator_type == "sprinkler":
            return "active" if self._current_value > 0.5 else "off"
        elif self.config.actuator_type == "fire_door":
            if self._current_value < 0.25:
                return "closed_locked"
            elif self._current_value < 0.75:
                return "closed_unlocked"
            else:
                return "open"
        return "off"

    async def register(self, bs):
        try:
            await bs.get_equipment(self.config.equipment_id)
        except Exception:
            initial = "off" if self.config.actuator_type == "sprinkler" else "closed_locked"
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
                    "state": initial,
                }],
            })
            logger.info(f"Registered actuator: {self.config.actuator_id}")

    async def _poll_command(self, bs):
        try:
            actuator = await bs.get_actuator(self.config.actuator_id)
            return actuator.get("state", None)
        except Exception:
            return None

    async def _report_state(self, bs, state):
        try:
            await bs.set_actuator_state(self.config.actuator_id, state)
        except Exception as e:
            logger.warning(f"Failed to report state for {self.config.actuator_id}: {e}")

    async def run(self, bs, interval=1.0):
        await self.register(bs)
        self._running = True
        logger.info(f"Actuator {self.config.actuator_id} started (rate={self.config.travel_rate}/s)")

        while self._running:
            commanded = await self._poll_command(bs)
            if commanded and commanded != self._last_known_command:
                self._last_known_command = commanded
                self._target_value = self._state_to_value(commanded)
                logger.info(f"Actuator {self.config.actuator_id}: new command -> {commanded}")

            if not self._is_stuck and random.random() < self.config.stuck_probability:
                self._is_stuck = True
                logger.warning(f"Actuator {self.config.actuator_id} is STUCK")

            if self._is_stuck:
                if random.random() < 0.02:
                    self._is_stuck = False
                    logger.info(f"Actuator {self.config.actuator_id} recovered")

            if not self._is_stuck:
                diff = self._target_value - self._current_value
                if abs(diff) > 0.01:
                    step = min(abs(diff), self.config.travel_rate * interval)
                    self._current_value += step if diff > 0 else -step

            achieved = self._value_to_state()
            await self._report_state(bs, achieved)

            await asyncio.sleep(interval)

    def stop(self):
        self._running = False

    def get_current_state(self):
        return self._value_to_state()

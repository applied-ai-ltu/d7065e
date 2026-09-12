"""Base actuator with BuildSim state reporting."""

import asyncio
import logging

from fds.buildsim.client import BuildSimClient
from fds.config import Config

logger = logging.getLogger(__name__)


class BaseActuator:
    """Base class for actuator clients."""

    def __init__(
        self,
        config: Config,
        equipment_id: str,
        actuator_id: str,
        room: str,
        level: str,
        actuator_type: str,
        initial_state: str = "off",
    ):
        self.config = config
        self.equipment_id = equipment_id
        self.actuator_id = actuator_id
        self.room = room
        self.level = level
        self.actuator_type = actuator_type
        self.state = initial_state
        self._running = False

    async def register(self, bs: BuildSimClient):
        try:
            await bs.get_equipment(self.equipment_id)
            logger.info(f"Equipment {self.equipment_id} already exists")
        except Exception:
            await bs.create_equipment({
                "id": self.equipment_id,
                "name": f"{self.actuator_type} ({self.room})",
                "type": "actuator",
                "category": "fire_response",
                "level": self.level,
                "room": self.room,
                "status": "running",
                "sensors": [],
                "actuators": [{
                    "id": self.actuator_id,
                    "name": self.actuator_type.replace("_", " ").capitalize(),
                    "type": self.actuator_type,
                    "state": self.state,
                }],
            })
            logger.info(f"Registered {self.actuator_type}: {self.actuator_id} in {self.room}")

    async def set_state(self, bs: BuildSimClient, new_state: str):
        old = self.state
        self.state = new_state
        try:
            await bs.set_actuator_state(self.actuator_id, new_state)
            logger.info(f"{self.actuator_id}: {old} -> {new_state}")
        except Exception as e:
            logger.warning(f"Failed to update actuator state: {e}")

    async def get_state(self):
        return self.state

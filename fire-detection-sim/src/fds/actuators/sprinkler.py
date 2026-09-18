"""Sprinkler actuator. States: off, active."""

from fds.actuators.base import BaseActuator


class SprinklerActuator(BaseActuator):
    def __init__(self, **kwargs):
        super().__init__(actuator_type="sprinkler", initial_state="off", **kwargs)

    async def activate(self, bs):
        await self.set_state(bs, "active")

    async def deactivate(self, bs):
        await self.set_state(bs, "off")

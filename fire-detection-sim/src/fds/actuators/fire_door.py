"""Fire door actuator. States: closed_locked, closed_unlocked, open."""

from fds.actuators.base import BaseActuator


class FireDoorActuator(BaseActuator):

    def __init__(self, **kwargs):
        super().__init__(actuator_type="fire_door", initial_state="closed_locked", **kwargs)

    async def unlock(self, bs):
        await self.set_state(bs, "closed_unlocked")

    async def lock(self, bs):
        await self.set_state(bs, "closed_locked")

    async def open_door(self, bs):
        await self.set_state(bs, "open")

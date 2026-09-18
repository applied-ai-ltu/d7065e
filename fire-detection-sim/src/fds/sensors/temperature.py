"""Temperature sensor simulator.

Simple thermal model: heat from occupants and fire, loss through walls
proportional to indoor-outdoor delta. Fire causes sharp rise in source
room and slower propagation to neighbours.
"""

import math
import random

from fds.sensors.base import BaseSensor


class TemperatureSensor(BaseSensor):
    def __init__(
        self,
        fire_start=None,
        outdoor_temp=-5.0,
        initial_temp=21.0,
        wall_loss_rate=0.002,
        **kwargs,
    ):
        super().__init__(sensor_type="temperature", unit="C", **kwargs)
        self.fire_start = fire_start
        self.outdoor_temp = outdoor_temp
        self.initial_temp = initial_temp
        self.wall_loss_rate = wall_loss_rate
        self._current_temp = initial_temp
        self._sprinkler_active = False

    def set_sprinkler_active(self, active):
        self._sprinkler_active = active

    def generate_value(self, elapsed):
        noise = random.gauss(0, 0.1)

        # Wall heat loss
        delta = self._current_temp - self.outdoor_temp
        heat_loss = self.wall_loss_rate * delta

        # Fire heat gain
        heat_gain = 0.0
        if self.fire_start and elapsed >= self.fire_start:
            t = elapsed - self.fire_start
            fire_heat = 0.8 / (1.0 + math.exp(-0.1 * (t - 20)))
            if self._sprinkler_active:
                fire_heat *= 0.15
            heat_gain += fire_heat

        # Occupant heat (small, ~0.05 C per tick)
        heat_gain += 0.01

        self._current_temp += heat_gain - heat_loss + noise
        return round(self._current_temp, 2)

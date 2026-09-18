"""Smoke sensor simulator."""

import math
import random

from fds.sensors.base import BaseSensor


class SmokeSensor(BaseSensor):
    def __init__(self, fire_start=None, false_alarm_times=None, **kwargs):
        super().__init__(sensor_type="smoke", unit="%obs/m", **kwargs)
        self.fire_start = fire_start
        self.false_alarm_times = false_alarm_times or []
        self._baseline = 0.5 + random.uniform(-0.2, 0.2)

    def generate_value(self, elapsed):
        noise = random.gauss(0, 0.15)
        value = self._baseline + noise

        if self.fire_start and elapsed >= self.fire_start:
            t = elapsed - self.fire_start
            fire_intensity = 80.0 / (1.0 + math.exp(-0.15 * (t - 30)))
            fire_noise = random.gauss(0, fire_intensity * 0.08)
            value += fire_intensity + fire_noise

        for alarm_time in self.false_alarm_times:
            dt = elapsed - alarm_time
            if 0 <= dt <= 15:
                spike = 8.0 * math.exp(-0.5 * ((dt - 7) / 3) ** 2)
                value += spike + random.gauss(0, 0.5)

        return max(0.0, value)

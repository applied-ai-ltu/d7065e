"""CO sensor simulator.

Correlates with smoke during real fires (incomplete combustion).
Stays flat during false alarms — this is the key fusion signal.
"""

import math
import random

from fds.sensors.base import BaseSensor


class COSensor(BaseSensor):

    def __init__(self, fire_start=None, false_alarm_times=None, **kwargs):
        super().__init__(sensor_type="co", unit="ppm", **kwargs)
        self.fire_start = fire_start
        self.false_alarm_times = false_alarm_times or []
        self._baseline = 2.0 + random.uniform(-0.5, 0.5)

    def generate_value(self, elapsed):
        noise = random.gauss(0, 0.3)
        value = self._baseline + noise

        # Fire: CO rises with smoke due to incomplete combustion
        if self.fire_start and elapsed >= self.fire_start:
            t = elapsed - self.fire_start
            co_level = 200.0 / (1.0 + math.exp(-0.12 * (t - 35)))
            co_noise = random.gauss(0, co_level * 0.06)
            value += co_level + co_noise

        # False alarms: CO does NOT correlate — stays at baseline
        # This is intentional: the detector uses this mismatch

        return max(0.0, value)

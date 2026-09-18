"""Sensor gateway — degrades ground truth into noisy observations.

Reads from the physics engine, applies realistic sensor degradation:
- Gaussian noise (sigma per sensor type)
- Measurement lag (delayed readings)
- Sensor drift (slow offset over time)
- Stuck/offline sensors (frozen values or no reading)

Writes degraded readings to BuildSim and publishes to MQTT.
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from fds.buildsim.client import BuildSimClient
from fds.config import Config
from fds.physics.engine import PhysicsEngine

logger = logging.getLogger(__name__)


@dataclass
class SensorConfig:
    sensor_id: str
    equipment_id: str
    room: str
    level: str
    sensor_type: str
    unit: str
    noise_sigma: float = 0.0
    lag_seconds: float = 0.0
    drift_rate: float = 0.0
    stuck_probability: float = 0.0


class SensorGateway:
    """Reads ground truth from PhysicsEngine, degrades it, writes to BuildSim + MQTT."""

    def __init__(self, config: Config, engine: PhysicsEngine, interval: float = 2.0):
        self.config = config
        self.engine = engine
        self.interval = interval
        self.sensors: list[SensorConfig] = []
        self._running = False
        self._mqtt = None
        self._drift_offsets: dict[str, float] = {}
        self._stuck_values: dict[str, float | None] = {}
        self._lag_buffers: dict[str, list[tuple[float, float]]] = {}

    def add_sensor(self, sensor: SensorConfig):
        self.sensors.append(sensor)
        self._drift_offsets[sensor.sensor_id] = 0.0
        self._stuck_values[sensor.sensor_id] = None
        self._lag_buffers[sensor.sensor_id] = []

    def _read_truth(self, sensor: SensorConfig) -> float:
        """Read ground truth from physics engine."""
        room = self.engine.get_room(sensor.room)
        if room is None:
            return 0.0
        if sensor.sensor_type == "smoke":
            return room.smoke
        elif sensor.sensor_type == "co":
            return room.co
        elif sensor.sensor_type == "temperature":
            return room.temperature
        return 0.0

    def _degrade(self, sensor: SensorConfig, truth: float, now: float) -> float | None:
        """Apply noise, lag, drift, stuck to ground truth."""
        sid = sensor.sensor_id

        # Stuck sensor: return frozen value
        if self._stuck_values[sid] is not None:
            if random.random() < 0.05:  # 5% chance of recovering per tick
                self._stuck_values[sid] = None
                logger.info(f"Sensor {sid} recovered from stuck state")
            else:
                return self._stuck_values[sid]

        # Random chance of becoming stuck
        if random.random() < sensor.stuck_probability:
            self._stuck_values[sid] = truth
            logger.warning(f"Sensor {sid} is now STUCK at {truth:.2f}")
            return truth

        # Lag: buffer readings and return delayed one
        if sensor.lag_seconds > 0:
            self._lag_buffers[sid].append((now, truth))
            cutoff = now - sensor.lag_seconds
            delayed = [v for t, v in self._lag_buffers[sid] if t <= cutoff]
            # Keep only recent entries
            self._lag_buffers[sid] = [(t, v) for t, v in self._lag_buffers[sid] if t > cutoff - 10]
            if delayed:
                truth = delayed[-1]
            else:
                return None  # No reading yet (still in lag window)

        # Drift: slow offset accumulation
        if sensor.drift_rate > 0:
            self._drift_offsets[sid] += random.gauss(0, sensor.drift_rate)
            truth += self._drift_offsets[sid]

        # Noise
        if sensor.noise_sigma > 0:
            truth += random.gauss(0, sensor.noise_sigma)

        return max(0.0, truth)

    async def register_all(self, bs: BuildSimClient):
        """Register all sensors as equipment in BuildSim."""
        # Group sensors by equipment_id
        equipment = {}
        for s in self.sensors:
            if s.equipment_id not in equipment:
                equipment[s.equipment_id] = {
                    "id": s.equipment_id,
                    "name": f"Sensor unit ({s.room})",
                    "type": "sensor",
                    "category": "fire_detection",
                    "level": s.level,
                    "room": s.room,
                    "status": "running",
                    "sensors": [],
                    "actuators": [],
                }
            equipment[s.equipment_id]["sensors"].append({
                "id": s.sensor_id,
                "name": s.sensor_type.capitalize(),
                "type": s.sensor_type,
                "data_type": "text",
                "unit": s.unit,
                "value": "0.0",
            })

        registered = 0
        skipped = 0
        for eq in equipment.values():
            try:
                await bs.get_equipment(eq["id"])
                skipped += 1
            except Exception:
                try:
                    await bs.create_equipment(eq)
                    registered += 1
                except Exception as e:
                    logger.warning(f"Failed to register {eq['id']}: {e}")
                    skipped += 1
        logger.info(f"Sensors registered: {registered}, skipped: {skipped}")

    async def run(self, bs: BuildSimClient):
        """Main gateway loop."""
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._mqtt.connect(self.config.mqtt.host, self.config.mqtt.port)
        self._mqtt.loop_start()

        await self.register_all(bs)
        self._running = True
        logger.info(f"Sensor gateway started ({len(self.sensors)} sensors, interval={self.interval}s)")

        while self._running:
            now = time.time()

            for sensor in self.sensors:
                truth = self._read_truth(sensor)
                degraded = self._degrade(sensor, truth, now)

                if degraded is None:
                    continue  # In lag window, no reading yet

                value = round(degraded, 2)

                # Write to BuildSim
                try:
                    await bs.set_sensor_value(sensor.sensor_id, str(value))
                except Exception as e:
                    logger.warning(f"BuildSim write failed for {sensor.sensor_id}: {e}")

                # Publish to MQTT (sanitize room name - some contain + which is MQTT wildcard)
                safe_room = sensor.room.replace("+", "_").replace("#", "_")
                topic = f"{self.config.mqtt.topic_prefix}/{sensor.level}/{safe_room}/{sensor.sensor_type}"
                payload = json.dumps({
                    "sensor_id": sensor.sensor_id,
                    "room": sensor.room,
                    "level": sensor.level,
                    "type": sensor.sensor_type,
                    "value": value,
                    "unit": sensor.unit,
                    "timestamp": now,
                })
                self._mqtt.publish(topic, payload, qos=1)

            await asyncio.sleep(self.interval)

    def stop(self):
        self._running = False
        if self._mqtt:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()

"""Base sensor simulator with MQTT publishing and BuildSim updates."""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from fds.buildsim.client import BuildSimClient
from fds.config import Config

logger = logging.getLogger(__name__)


@dataclass
class SensorReading:
    sensor_id: str
    equipment_id: str
    room: str
    level: str
    sensor_type: str
    value: float
    unit: str
    timestamp: float


class BaseSensor(ABC):
    """Base class for all sensor simulators."""

    def __init__(
        self,
        config: Config,
        equipment_id: str,
        sensor_id: str,
        room: str,
        level: str,
        sensor_type: str,
        unit: str,
        interval: float = 1.0,
    ):
        self.config = config
        self.equipment_id = equipment_id
        self.sensor_id = sensor_id
        self.room = room
        self.level = level
        self.sensor_type = sensor_type
        self.unit = unit
        self.interval = interval
        self._running = False
        self._mqtt = None

    @property
    def mqtt_topic(self) -> str:
        return f"{self.config.mqtt.topic_prefix}/{self.level}/{self.room}/{self.sensor_type}"

    @abstractmethod
    def generate_value(self, elapsed: float) -> float:
        """Generate the next sensor value. elapsed = seconds since start."""

    async def register(self, bs: BuildSimClient):
        """Register equipment and sensor in BuildSim if not already present."""
        try:
            await bs.get_equipment(self.equipment_id)
            logger.info(f"Equipment {self.equipment_id} already exists")
        except Exception:
            await bs.create_equipment(
                {
                    "id": self.equipment_id,
                    "name": f"{self.sensor_type} sensor ({self.room})",
                    "type": "sensor",
                    "category": "fire_detection",
                    "level": self.level,
                    "room": self.room,
                    "status": "running",
                    "sensors": [
                        {
                            "id": self.sensor_id,
                            "name": self.sensor_type.capitalize(),
                            "type": self.sensor_type,
                            "data_type": "text",
                            "unit": self.unit,
                            "value": "0.0",
                        }
                    ],
                    "actuators": [],
                }
            )
            logger.info(f"Registered {self.sensor_type} sensor: {self.sensor_id} in {self.room}")

    async def run(self, bs: BuildSimClient):
        """Main sensor loop: generate, publish MQTT, update BuildSim."""
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._mqtt.connect(self.config.mqtt.host, self.config.mqtt.port)
        self._mqtt.loop_start()

        await self.register(bs)
        self._running = True
        start_time = time.time()
        logger.info(f"Sensor {self.sensor_id} started (interval={self.interval}s)")

        while self._running:
            elapsed = time.time() - start_time
            value = self.generate_value(elapsed)

            reading = SensorReading(
                sensor_id=self.sensor_id,
                equipment_id=self.equipment_id,
                room=self.room,
                level=self.level,
                sensor_type=self.sensor_type,
                value=round(value, 2),
                unit=self.unit,
                timestamp=time.time(),
            )

            payload = json.dumps(
                {
                    "sensor_id": reading.sensor_id,
                    "room": reading.room,
                    "level": reading.level,
                    "type": reading.sensor_type,
                    "value": reading.value,
                    "unit": reading.unit,
                    "timestamp": reading.timestamp,
                }
            )
            self._mqtt.publish(self.mqtt_topic, payload, qos=1)

            try:
                await bs.set_sensor_value(self.sensor_id, str(reading.value))
            except Exception as e:
                logger.warning(f"Failed to update BuildSim: {e}")

            logger.debug(f"{self.sensor_id}: {reading.value} {reading.unit}")
            await asyncio.sleep(self.interval)

    def stop(self):
        self._running = False
        if self._mqtt:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()

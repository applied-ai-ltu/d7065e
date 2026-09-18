"""Run a single smoke sensor for testing."""

import asyncio
import logging

from fds.buildsim.client import BuildSimClient
from fds.config import Config
from fds.sensors.smoke import SmokeSensor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def main():
    config = Config.from_yaml("configs/config.yaml")

    sensor = SmokeSensor(
        config=config,
        equipment_id="smoke-A109",
        sensor_id="A109-smoke",
        room="A109",
        level="level0",
        interval=2.0,
        fire_start=30.0,
    )

    async with BuildSimClient(config.buildsim.url) as bs:
        print(f"Smoke sensor starting in room A109")
        print(f"Fire will start at t=30s")
        print(f"MQTT topic: {sensor.mqtt_topic}")
        print(f"Watch BuildSim: http://localhost:30909")
        print(f"Press Ctrl+C to stop")
        try:
            await sensor.run(bs)
        except KeyboardInterrupt:
            sensor.stop()
            print("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())

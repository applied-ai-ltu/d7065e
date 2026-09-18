"""Run all sensors and pipeline for one room."""

import asyncio
import logging
import os

from fds.buildsim.client import BuildSimClient
from fds.config import Config
from fds.pipeline.consumer import PipelineConsumer
from fds.pipeline.schema import init_database
from fds.sensors.co import COSensor
from fds.sensors.smoke import SmokeSensor
from fds.sensors.temperature import TemperatureSensor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main():
    config = Config.from_yaml("configs/config.yaml")
    config.database.password = os.getenv("DB_PASSWORD", config.database.password)

    # Init DB schema
    await init_database(
        config.database.host,
        config.database.port,
        config.database.user,
        config.database.password,
        config.database.database,
    )

    fire_start = 30.0

    # Create sensors for room A109
    sensors = [
        SmokeSensor(
            config=config,
            equipment_id="smoke-A109",
            sensor_id="A109-smoke",
            room="A109",
            level="level0",
            interval=2.0,
            fire_start=fire_start,
        ),
        COSensor(
            config=config,
            equipment_id="co-A109",
            sensor_id="A109-co",
            room="A109",
            level="level0",
            interval=2.0,
            fire_start=fire_start,
        ),
        TemperatureSensor(
            config=config,
            equipment_id="temp-A109",
            sensor_id="A109-temp",
            room="A109",
            level="level0",
            interval=2.0,
            fire_start=fire_start,
        ),
    ]

    # Pipeline consumer
    consumer = PipelineConsumer(config)

    print("=" * 60)
    print("Fire Detection Sim - One Room Loop")
    print("=" * 60)
    print("Room: A109 (level0)")
    print(f"Fire starts at: t={fire_start}s")
    print("Sensors: smoke, CO, temperature (interval=2s)")
    print(f"MQTT: {config.mqtt.host}:{config.mqtt.port}")
    print(f"DB: {config.database.database}")
    print(f"BuildSim: {config.buildsim.url}")
    print("Press Ctrl+C to stop")
    print("=" * 60)

    async with BuildSimClient(config.buildsim.url) as bs:
        tasks = []
        for sensor in sensors:
            tasks.append(asyncio.create_task(sensor.run(bs)))
        tasks.append(asyncio.create_task(consumer.run()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for sensor in sensors:
                sensor.stop()
            consumer.stop()
            print("All stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutdown.")

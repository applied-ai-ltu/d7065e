"""Run the complete fire detection system: sensors + pipeline + agent."""

import asyncio
import json
import logging
import os
import time

import paho.mqtt.client as mqtt

from fds.buildsim.client import BuildSimClient
from fds.config import Config
from fds.secrets import get_secrets
from fds.sensors.smoke import SmokeSensor
from fds.sensors.co import COSensor
from fds.sensors.temperature import TemperatureSensor
from fds.models.detector import FireDetector
from fds.agent.agent import FireAgent
from fds.pipeline.schema import init_database
from fds.pipeline.consumer import PipelineConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main():
    config = Config.from_yaml("configs/config.yaml")
    secrets = get_secrets()
    config.database.password = secrets.get("db_password", "")

    # Init DB
    await init_database(
        config.database.host, config.database.port,
        config.database.user, config.database.password,
        config.database.database,
    )

    fire_start = 60.0
    room = "A109"
    level = "level0"

    # Load detector
    detector = FireDetector()
    try:
        detector.load("models/detector.pkl")
        logger.info("Detector loaded")
    except FileNotFoundError:
        logger.warning("No trained model found, training now...")
        from fds.models.trainer import train_detector
        detector = train_detector()

    # Create sensors
    sensors = [
        SmokeSensor(
            config=config, equipment_id=f"smoke-{room}", sensor_id=f"{room}-smoke",
            room=room, level=level, interval=2.0, fire_start=fire_start,
        ),
        COSensor(
            config=config, equipment_id=f"co-{room}", sensor_id=f"{room}-co",
            room=room, level=level, interval=2.0, fire_start=fire_start,
        ),
        TemperatureSensor(
            config=config, equipment_id=f"temp-{room}", sensor_id=f"{room}-temp",
            room=room, level=level, interval=2.0, fire_start=fire_start,
        ),
    ]

    # Pipeline consumer
    consumer = PipelineConsumer(config)

    async with BuildSimClient(config.buildsim.url) as bs:
        # Create agent
        agent = FireAgent(
            bs=bs, detector=detector,
            llm_url=config.llm.base_url,
            llm_api_key=secrets.get("litellm_api_key", ""),
            model=config.llm.model,
            tick_interval=5.0,
        )

        # MQTT subscriber to feed agent
        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        def on_message(client, userdata, msg):
            try:
                data = json.loads(msg.payload.decode())
                agent.update_reading(data["room"], data["type"], data["value"])
            except Exception as e:
                logger.warning(f"Agent feed error: {e}")

        mqtt_client.on_message = on_message
        mqtt_client.connect(config.mqtt.host, config.mqtt.port)
        mqtt_client.subscribe(f"{config.mqtt.topic_prefix}/#", qos=1)
        mqtt_client.loop_start()

        print("=" * 60)
        print("Fire Detection System - Full Loop")
        print("=" * 60)
        print(f"Room: {room} ({level})")
        print(f"Fire starts at: t={fire_start}s")
        print(f"Agent model: {config.llm.model}")
        print(f"Detector: isolation forest (trained)")
        print(f"BuildSim: {config.buildsim.url}")
        print(f"LLM: {config.llm.base_url}")
        print(f"Press Ctrl+C to stop")
        print("=" * 60)

        tasks = [
            asyncio.create_task(sensor.run(bs)) for sensor in sensors
        ]
        tasks.append(asyncio.create_task(consumer.run()))
        tasks.append(asyncio.create_task(agent.run([(room, level)])))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for sensor in sensors:
                sensor.stop()
            consumer.stop()
            agent.stop()
            mqtt_client.loop_stop()
            mqtt_client.disconnect()

            # Save episode log
            log = agent.get_episode_log()
            if log:
                os.makedirs("logs", exist_ok=True)
                with open("logs/episode_log.json", "w") as f:
                    json.dump(log, f, indent=2)
                logger.info(f"Saved {len(log)} episodes to logs/episode_log.json")

            print("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")

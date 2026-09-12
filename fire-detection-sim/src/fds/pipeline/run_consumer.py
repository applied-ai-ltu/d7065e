"""Run the MQTT to TimescaleDB pipeline consumer."""

import asyncio
import logging
import os

from fds.config import Config
from fds.pipeline.schema import init_database
from fds.pipeline.consumer import PipelineConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def main():
    config = Config.from_yaml("configs/config.yaml")
    config.database.password = os.getenv("DB_PASSWORD", config.database.password)

    print("Initializing database schema...")
    await init_database(
        config.database.host,
        config.database.port,
        config.database.user,
        config.database.password,
        config.database.database,
    )

    print("Starting pipeline consumer...")
    print(f"MQTT: {config.mqtt.host}:{config.mqtt.port}")
    print(f"DB: {config.database.host}:{config.database.port}/{config.database.database}")
    print("Press Ctrl+C to stop")

    consumer = PipelineConsumer(config)
    try:
        await consumer.run()
    except KeyboardInterrupt:
        consumer.stop()
        print("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())

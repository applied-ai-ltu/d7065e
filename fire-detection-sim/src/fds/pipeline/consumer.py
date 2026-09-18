"""MQTT to TimescaleDB consumer.

Subscribes to building/# topics, validates readings, writes to TimescaleDB.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime

import asyncpg
import paho.mqtt.client as mqtt

from fds.config import Config

logger = logging.getLogger(__name__)

SCHEMA = "fire_detection_sim"


class PipelineConsumer:
    def __init__(self, config: Config):
        self.config = config
        self._pool = None
        self._mqtt = None
        self._queue = asyncio.Queue()
        self._running = False

    async def _init_db(self):
        self._pool = await asyncpg.create_pool(
            host=self.config.database.host,
            port=self.config.database.port,
            user=self.config.database.user,
            password=self.config.database.password,
            database=self.config.database.database,
            min_size=2,
            max_size=10,
        )
        logger.info("Database pool created")

    def _init_mqtt(self):
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
                self._queue.put_nowait(payload)
            except Exception as e:
                logger.warning(f"Bad MQTT message: {e}")

        self._mqtt.on_message = on_message
        self._mqtt.connect(self.config.mqtt.host, self.config.mqtt.port)
        self._mqtt.subscribe(f"{self.config.mqtt.topic_prefix}/#", qos=1)
        self._mqtt.loop_start()
        logger.info(f"MQTT subscribed to {self.config.mqtt.topic_prefix}/#")

    async def _write_reading(self, reading):
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {SCHEMA}.sensor_readings
                    (time, sensor_id, room, level, sensor_type, value, unit)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                datetime.fromtimestamp(reading["timestamp"], tz=UTC),
                reading["sensor_id"],
                reading["room"],
                reading["level"],
                reading["type"],
                float(reading["value"]),
                reading["unit"],
            )

    async def _drain_queue(self):
        while self._running:
            try:
                reading = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._write_reading(reading)
                logger.debug(f"Stored: {reading['sensor_id']}={reading['value']}")
            except TimeoutError:
                continue
            except Exception as e:
                logger.warning(f"Write failed: {e}")

    async def run(self):
        await self._init_db()
        self._init_mqtt()
        self._running = True
        logger.info("Pipeline consumer running")
        try:
            await self._drain_queue()
        finally:
            self._running = False
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
            await self._pool.close()

    def stop(self):
        self._running = False

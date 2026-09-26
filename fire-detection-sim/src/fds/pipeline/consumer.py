"""MQTT to TimescaleDB consumer.

Subscribes to sensor readings (building/#), edge decisions and alerts
(decisions/#, alerts/#) and edge heartbeats (heartbeat/#) and writes them
to TimescaleDB. Topic routing decides the table; payloads are stored as-is.
"""

import asyncio
import json
import logging
import time
from collections import Counter
from datetime import UTC, datetime

import asyncpg
import paho.mqtt.client as mqtt

from fds.config import Config

logger = logging.getLogger(__name__)

SCHEMA = "fire_detection_sim"
EDGE_TOPICS = ("decisions/#", "alerts/#", "heartbeat/#")
STATS_EVERY_S = 60


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class PipelineConsumer:
    def __init__(self, config: Config):
        self.config = config
        self._pool = None
        self._mqtt = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop = None
        self._running = False
        self._counts: Counter = Counter()

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
            except Exception as e:
                logger.warning(f"Bad MQTT message on {msg.topic}: {e}")
                return
            if self._loop:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, (msg.topic, payload))

        self._mqtt.on_message = on_message
        self._mqtt.connect(self.config.mqtt.host, self.config.mqtt.port)
        topics = [(f"{self.config.mqtt.topic_prefix}/#", 1)] + [(t, 1) for t in EDGE_TOPICS]
        self._mqtt.subscribe(topics)
        self._mqtt.loop_start()
        logger.info(f"MQTT subscribed to {[t for t, _ in topics]}")

    # ------------------------------------------------------------- writers
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

    async def _write_edge(self, topic, p):
        """decisions/{level}/{room} and alerts/{level}/{room} -> edge_decisions."""
        parts = topic.split("/")
        kind = "alert" if parts[0] == "alerts" else "decision"
        level = parts[1] if len(parts) > 1 else p.get("floor") or p.get("level") or ""
        room = p.get("room") or (parts[2] if len(parts) > 2 else "")
        anomaly = p.get("if_anomaly")
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {SCHEMA}.edge_decisions
                    (time, level, room, node, role, kind, decision, severity,
                     smoke, co, temperature, if_ms, if_anomaly)
                    VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
                level,
                room,
                p.get("node"),
                p.get("role"),
                kind,
                str(p.get("decision", "")),
                p.get("severity"),
                _num(p.get("smoke")),
                _num(p.get("co")),
                _num(p.get("temperature")),
                _num(p.get("if_ms")),
                bool(anomaly) if anomaly is not None else None,
            )

    async def _write_heartbeat(self, topic, p):
        """heartbeat/{level}/{role} -> heartbeats."""
        parts = topic.split("/")
        level = parts[1] if len(parts) > 1 else p.get("floor", "")
        role = parts[2] if len(parts) > 2 else p.get("role", "")
        acting = p.get("acting")
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {SCHEMA}.heartbeats (time, level, role, node, acting)
                    VALUES (NOW(), $1, $2, $3, $4)""",
                level,
                role,
                p.get("node"),
                bool(acting) if acting is not None else None,
            )

    # -------------------------------------------------------------- loop
    async def _drain_queue(self):
        last_stats = time.time()
        while self._running:
            try:
                topic, payload = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                if topic.startswith("heartbeat/"):
                    await self._write_heartbeat(topic, payload)
                    self._counts["heartbeats"] += 1
                elif topic.startswith(("decisions/", "alerts/")):
                    await self._write_edge(topic, payload)
                    self._counts["edge_decisions"] += 1
                else:
                    await self._write_reading(payload)
                    self._counts["sensor_readings"] += 1
            except TimeoutError:
                pass
            except Exception as e:
                logger.warning(f"Write failed: {e}")
            if time.time() - last_stats >= STATS_EVERY_S:
                logger.info(f"Pipeline stored: {dict(self._counts)} (queue={self._queue.qsize()})")
                last_stats = time.time()

    async def run(self):
        self._loop = asyncio.get_running_loop()
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

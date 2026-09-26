"""Create TimescaleDB tables and continuous aggregates."""

import logging

import asyncpg

logger = logging.getLogger(__name__)

SCHEMA = "fire_detection_sim"

CREATE_TABLES = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.sensor_readings (
    time        TIMESTAMPTZ NOT NULL,
    sensor_id   TEXT NOT NULL,
    room        TEXT NOT NULL,
    level       TEXT NOT NULL,
    sensor_type TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    unit        TEXT NOT NULL
);

SELECT create_hypertable(
    '{SCHEMA}.sensor_readings',
    by_range('time'),
    if_not_exists => TRUE
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.actuator_events (
    time         TIMESTAMPTZ NOT NULL,
    actuator_id  TEXT NOT NULL,
    room         TEXT NOT NULL,
    level        TEXT NOT NULL,
    actuator_type TEXT NOT NULL,
    old_state    TEXT,
    new_state    TEXT NOT NULL
);

SELECT create_hypertable(
    '{SCHEMA}.actuator_events',
    by_range('time'),
    if_not_exists => TRUE
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.fire_events (
    time       TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL,
    room       TEXT NOT NULL,
    level      TEXT NOT NULL,
    severity   TEXT NOT NULL,
    details    JSONB
);

SELECT create_hypertable(
    '{SCHEMA}.fire_events',
    by_range('time'),
    if_not_exists => TRUE
);

-- Tier-1 (edge) decisions and alerts: every threshold candidate, IF verdict and escalation
CREATE TABLE IF NOT EXISTS {SCHEMA}.edge_decisions (
    time        TIMESTAMPTZ NOT NULL,
    level       TEXT NOT NULL,
    room        TEXT NOT NULL,
    node        TEXT,
    role        TEXT,
    kind        TEXT NOT NULL,
    decision    TEXT NOT NULL,
    severity    TEXT,
    smoke       DOUBLE PRECISION,
    co          DOUBLE PRECISION,
    temperature DOUBLE PRECISION,
    if_ms       DOUBLE PRECISION,
    if_anomaly  BOOLEAN
);

SELECT create_hypertable(
    '{SCHEMA}.edge_decisions',
    by_range('time'),
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS edge_decisions_room_time
    ON {SCHEMA}.edge_decisions (room, time DESC);

-- Edge controller liveness (active/standby per floor)
CREATE TABLE IF NOT EXISTS {SCHEMA}.heartbeats (
    time   TIMESTAMPTZ NOT NULL,
    level  TEXT NOT NULL,
    role   TEXT NOT NULL,
    node   TEXT,
    acting BOOLEAN
);

SELECT create_hypertable(
    '{SCHEMA}.heartbeats',
    by_range('time'),
    if_not_exists => TRUE
);

-- Tier-2 (server/LLM) decisions: confirm, watch_pending, escalated, stand_down,
-- stand_down_refused, failover, server_takeover, server_handback
CREATE TABLE IF NOT EXISTS {SCHEMA}.llm_overrides (
    time          TIMESTAMPTZ NOT NULL,
    room          TEXT NOT NULL,
    level         TEXT NOT NULL,
    override_type TEXT NOT NULL,
    reason        TEXT
);

SELECT create_hypertable(
    '{SCHEMA}.llm_overrides',
    by_range('time'),
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS llm_overrides_type_time
    ON {SCHEMA}.llm_overrides (override_type, time DESC);
"""

CREATE_CONTINUOUS_AGGREGATES = f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS {SCHEMA}.sensor_stats_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    sensor_id,
    room,
    level,
    sensor_type,
    AVG(value) AS avg_value,
    MAX(value) AS max_value,
    MIN(value) AS min_value,
    STDDEV(value) AS stddev_value,
    COUNT(*) AS sample_count,
    LAST(value, time) AS last_value,
    LAST(value, time) - FIRST(value, time) AS delta_value
FROM {SCHEMA}.sensor_readings
GROUP BY bucket, sensor_id, room, level, sensor_type
WITH NO DATA;

SELECT add_continuous_aggregate_policy('{SCHEMA}.sensor_stats_1m',
    start_offset    => INTERVAL '10 minutes',
    end_offset      => INTERVAL '1 minute',
    schedule_interval => INTERVAL '30 seconds',
    if_not_exists   => TRUE
);
"""


async def init_database(host, port, user, password, database):
    conn = await asyncpg.connect(
        host=host, port=port, user=user, password=password, database=database
    )
    try:
        for statement in CREATE_TABLES.split(";"):
            statement = statement.strip()
            if statement:
                try:
                    await conn.execute(statement)
                except Exception as e:
                    logger.warning(f"Schema statement: {e}")

        for statement in CREATE_CONTINUOUS_AGGREGATES.split(";"):
            statement = statement.strip()
            if statement:
                try:
                    await conn.execute(statement)
                except Exception as e:
                    logger.warning(f"Aggregate statement: {e}")

        logger.info("Database schema initialized")
    finally:
        await conn.close()

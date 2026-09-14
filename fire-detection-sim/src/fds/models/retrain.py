"""ClearML-managed detector retraining.

Logs training runs, tracks model versions, supports periodic retraining
on accumulated episode data from TimescaleDB.
"""

import asyncio
import logging
import os

import asyncpg
import numpy as np

from fds.models.detector import FireDetector
from fds.models.trainer import generate_normal_windows, train_detector
from fds.config import Config
from fds.secrets import get_secrets

logger = logging.getLogger(__name__)


async def fetch_training_data(config):
    """Fetch accumulated sensor data from TimescaleDB for retraining."""
    conn = await asyncpg.connect(
        host=config.database.host,
        port=config.database.port,
        user=config.database.user,
        password=config.database.password,
        database=config.database.database,
    )

    rows = await conn.fetch("""
        SELECT
            time_bucket('30 seconds', time) AS bucket,
            room,
            MAX(CASE WHEN sensor_type = 'smoke' THEN value END) AS smoke,
            MAX(CASE WHEN sensor_type = 'co' THEN value END) AS co,
            MAX(CASE WHEN sensor_type = 'temperature' THEN value END) AS temperature
        FROM fire_detection_sim.sensor_readings
        WHERE time > NOW() - INTERVAL '7 days'
        GROUP BY bucket, room
        ORDER BY bucket
    """)

    await conn.close()
    logger.info(f"Fetched {len(rows)} training windows from TimescaleDB")
    return rows


def rows_to_windows(rows, window_size=10):
    """Convert DB rows to training windows."""
    windows = []
    buffer = {"smoke": [], "co": [], "temperature": []}

    for row in rows:
        for key in buffer:
            val = row.get(key)
            if val is not None:
                buffer[key].append(float(val))

        if all(len(v) >= window_size for v in buffer.values()):
            windows.append({
                "smoke": buffer["smoke"][-window_size:],
                "co": buffer["co"][-window_size:],
                "temperature": buffer["temperature"][-window_size:],
            })
            for key in buffer:
                buffer[key] = buffer[key][1:]

    return windows


async def retrain_with_clearml(config):
    """Retrain detector with ClearML experiment tracking."""
    try:
        from clearml import Task

        task = Task.init(
            project_name="d7065e",
            task_name="detector-retrain",
            auto_connect_frameworks=True,
        )

        # Fetch data
        rows = await fetch_training_data(config)

        if len(rows) < 50:
            logger.warning(f"Not enough data for retraining: {len(rows)} rows (need 50+)")
            logger.info("Using synthetic data instead")
            normal_windows = generate_normal_windows(n_windows=1000)
        else:
            normal_windows = rows_to_windows(rows)
            # Filter to normal-only (low smoke, low CO)
            normal_windows = [
                w for w in normal_windows
                if max(w["smoke"]) < 5 and max(w["co"]) < 10
            ]
            if len(normal_windows) < 100:
                logger.info(f"Only {len(normal_windows)} normal windows, supplementing with synthetic")
                normal_windows.extend(generate_normal_windows(n_windows=500))

        logger.info(f"Training on {len(normal_windows)} normal windows")
        task.get_logger().report_scalar("data", "training_windows", len(normal_windows), 0)

        # Train
        detector = FireDetector()
        detector.train(normal_windows)

        # Evaluate
        from fds.models.trainer import generate_fire_windows, generate_false_alarm_windows

        test_normal = generate_normal_windows(n_windows=100)
        test_fire = generate_fire_windows(n_windows=50)
        test_false = generate_false_alarm_windows(n_windows=50)

        correct_normal = sum(1 for w in test_normal if not detector.predict(w)[0])
        correct_fire = sum(1 for w in test_fire if detector.predict(w)[0])
        correct_false = sum(1 for w in test_false if not detector.predict(w)[0])

        metrics = {
            "normal_accuracy": correct_normal / 100,
            "fire_detection_rate": correct_fire / 50,
            "false_alarm_rejection": correct_false / 50,
        }

        for name, value in metrics.items():
            task.get_logger().report_scalar("evaluation", name, value, 0)
            logger.info(f"  {name}: {value:.2%}")

        # Save model
        os.makedirs("models", exist_ok=True)
        detector.save("models/detector.pkl")

        task.upload_artifact("detector_model", "models/detector.pkl")
        task.close()

        logger.info("Retraining complete, model saved and tracked in ClearML")
        return detector, metrics

    except ImportError:
        logger.warning("ClearML not installed, retraining without tracking")
        return train_detector(), {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config = Config.from_yaml("configs/config.yaml")
    secrets = get_secrets()
    config.database.password = secrets.get("db_password", "")

    print("Retraining detector with ClearML tracking...")
    detector, metrics = asyncio.run(retrain_with_clearml(config))
    print(f"Done. Metrics: {metrics}")

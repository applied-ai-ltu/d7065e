"""Test sensor degradation."""

from fds.config import Config
from fds.physics.engine import PhysicsEngine
from fds.physics.sensor_gateway import SensorConfig, SensorGateway


def test_noise_adds_variation():
    engine = PhysicsEngine()
    engine.add_room("A109", "level0")

    config = Config()
    gw = SensorGateway(config, engine)
    sensor = SensorConfig(
        sensor_id="test",
        equipment_id="test-eq",
        room="A109",
        level="level0",
        sensor_type="temperature",
        unit="C",
        noise_sigma=1.0,
    )
    gw.add_sensor(sensor)

    import time

    now = time.time()
    values = [gw._degrade(sensor, 21.0, now + i) for i in range(50)]
    values = [v for v in values if v is not None]
    assert len(set(round(v, 1) for v in values)) > 1, "No variation from noise"


def test_stuck_sensor_freezes():
    engine = PhysicsEngine()
    engine.add_room("A109", "level0")

    config = Config()
    gw = SensorGateway(config, engine)
    sensor = SensorConfig(
        sensor_id="stuck",
        equipment_id="test-eq",
        room="A109",
        level="level0",
        sensor_type="temperature",
        unit="C",
        stuck_probability=1.0,
    )
    gw.sensors.append(sensor)
    gw._drift_offsets["stuck"] = 0.0
    gw._stuck_values["stuck"] = None
    gw._lag_buffers["stuck"] = []

    import random
    import time

    random.seed(1)  # stuck sensors recover with 5% probability per tick; keep it deterministic
    now = time.time()
    val1 = gw._degrade(sensor, 21.0, now)
    val2 = gw._degrade(sensor, 25.0, now + 1)
    assert val2 == val1, "Stuck sensor should return frozen value"

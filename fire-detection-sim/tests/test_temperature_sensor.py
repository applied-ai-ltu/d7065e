"""Test temperature sensor value generation."""

from fds.sensors.temperature import TemperatureSensor
from fds.config import Config


def make_sensor(**kwargs):
    config = Config()
    return TemperatureSensor(
        config=config,
        equipment_id="test-temp",
        sensor_id="test-temp-1",
        room="A109",
        level="level0",
        **kwargs,
    )


def test_stable_without_fire():
    sensor = make_sensor()
    values = [sensor.generate_value(t) for t in range(100)]
    assert all(15 < v < 25 for v in values), f"Temperature unstable: min={min(values)}, max={max(values)}"


def test_fire_raises_temperature():
    sensor = make_sensor(fire_start=10.0)
    for t in range(80):
        sensor.generate_value(t)
    final = sensor.generate_value(80)
    assert final > 25.0, f"Temperature did not rise during fire: {final}"


def test_sprinkler_slows_rise():
    sensor_no_sprinkler = make_sensor(fire_start=10.0)
    sensor_with_sprinkler = make_sensor(fire_start=10.0)
    sensor_with_sprinkler.set_sprinkler_active(True)

    for t in range(80):
        sensor_no_sprinkler.generate_value(t)
        sensor_with_sprinkler.generate_value(t)

    temp_no = sensor_no_sprinkler.generate_value(80)
    temp_with = sensor_with_sprinkler.generate_value(80)
    assert temp_with < temp_no, f"Sprinkler did not slow rise: {temp_with} >= {temp_no}"

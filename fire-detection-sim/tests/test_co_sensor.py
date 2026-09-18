"""Test CO sensor value generation."""

from fds.config import Config
from fds.sensors.co import COSensor


def make_sensor(**kwargs):
    config = Config()
    return COSensor(
        config=config,
        equipment_id="test-co",
        sensor_id="test-co-1",
        room="A109",
        level="level0",
        **kwargs,
    )


def test_baseline_low():
    sensor = make_sensor()
    values = [sensor.generate_value(t) for t in range(100)]
    avg = sum(values) / len(values)
    assert avg < 5.0, f"CO baseline too high: {avg}"


def test_fire_raises_co():
    sensor = make_sensor(fire_start=10.0)
    before = sensor.generate_value(5.0)
    during = sensor.generate_value(80.0)
    assert during > before * 10, f"CO did not rise during fire: {before} -> {during}"


def test_false_alarm_no_co_rise():
    sensor = make_sensor(false_alarm_times=[10.0])
    before = sensor.generate_value(5.0)
    during = sensor.generate_value(17.0)
    assert abs(during - before) < 5.0, f"CO should not spike on false alarm: {before} -> {during}"


def test_values_never_negative():
    sensor = make_sensor()
    values = [sensor.generate_value(t) for t in range(200)]
    assert all(v >= 0 for v in values)

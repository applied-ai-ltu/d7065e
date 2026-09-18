"""Test smoke sensor value generation."""

from fds.config import Config
from fds.sensors.smoke import SmokeSensor


def make_sensor(**kwargs):
    config = Config()
    return SmokeSensor(
        config=config,
        equipment_id="test-smoke",
        sensor_id="test-smoke-1",
        room="A109",
        level="level0",
        **kwargs,
    )


def test_baseline_near_zero():
    sensor = make_sensor()
    values = [sensor.generate_value(t) for t in range(100)]
    avg = sum(values) / len(values)
    assert avg < 3.0, f"Baseline average too high: {avg}"


def test_fire_ramps_up():
    sensor = make_sensor(fire_start=10.0)
    before_fire = sensor.generate_value(5.0)
    during_fire = sensor.generate_value(60.0)
    assert during_fire > before_fire * 5, f"Fire did not ramp: {before_fire} -> {during_fire}"


def test_false_alarm_is_brief():
    sensor = make_sensor(false_alarm_times=[10.0])
    before = sensor.generate_value(5.0)
    during = sensor.generate_value(17.0)
    after = sensor.generate_value(30.0)
    assert during > before * 3, f"False alarm too weak: {before} -> {during}"
    assert after < during, f"False alarm did not decay: {during} -> {after}"


def test_values_never_negative():
    sensor = make_sensor()
    values = [sensor.generate_value(t) for t in range(200)]
    assert all(v >= 0 for v in values), "Negative smoke value"

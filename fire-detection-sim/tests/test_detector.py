"""Test the fire detector."""

from fds.models.detector import FireDetector
from fds.models.trainer import (
    generate_normal_windows,
    generate_fire_windows,
    generate_false_alarm_windows,
)


def test_trains_without_error():
    detector = FireDetector()
    normal = generate_normal_windows(n_windows=100)
    detector.train(normal)
    assert detector.is_fitted


def test_detects_fire():
    detector = FireDetector()
    normal = generate_normal_windows(n_windows=200)
    detector.train(normal)

    fire = generate_fire_windows(n_windows=20)
    detected = sum(1 for w in fire if detector.predict(w)[0])
    assert detected >= 15, f"Only detected {detected}/20 fires"


def test_ignores_normal():
    detector = FireDetector()
    normal = generate_normal_windows(n_windows=200)
    detector.train(normal)

    test = generate_normal_windows(n_windows=50)
    false_alarms = sum(1 for w in test if detector.predict(w)[0])
    assert false_alarms < 10, f"Too many false alarms: {false_alarms}/50"


def test_handles_false_alarm_smoke():
    detector = FireDetector()
    normal = generate_normal_windows(n_windows=200)
    detector.train(normal)

    false = generate_false_alarm_windows(n_windows=20)
    incorrectly_flagged = sum(1 for w in false if detector.predict(w)[0])
    assert incorrectly_flagged < 10, f"Too many false alarms flagged as fire: {incorrectly_flagged}/20"


def test_save_load(tmp_path):
    detector = FireDetector()
    normal = generate_normal_windows(n_windows=100)
    detector.train(normal)

    path = tmp_path / "test_detector.pkl"
    detector.save(path)

    loaded = FireDetector()
    loaded.load(path)
    assert loaded.is_fitted

    fire = generate_fire_windows(n_windows=5)
    for w in fire:
        orig = detector.predict(w)
        load = loaded.predict(w)
        assert orig[0] == load[0]

"""Generate synthetic training data and train the detector."""

import logging
import math
import random

from fds.models.detector import FireDetector

logger = logging.getLogger(__name__)


def generate_normal_windows(n_windows=500, window_size=10):
    """Generate synthetic normal operation data."""
    windows = []
    for _ in range(n_windows):
        smoke_base = 0.5 + random.uniform(-0.2, 0.2)
        co_base = 2.0 + random.uniform(-0.5, 0.5)
        temp_base = 21.0 + random.uniform(-1.0, 1.0)

        smoke = [smoke_base + random.gauss(0, 0.15) for _ in range(window_size)]
        co = [co_base + random.gauss(0, 0.3) for _ in range(window_size)]
        temp = [temp_base + random.gauss(0, 0.1) for _ in range(window_size)]

        # Ensure non-negative
        smoke = [max(0.0, v) for v in smoke]
        co = [max(0.0, v) for v in co]

        windows.append({"smoke": smoke, "co": co, "temperature": temp})

    return windows


def generate_fire_windows(n_windows=50, window_size=10):
    """Generate synthetic fire event data for testing."""
    windows = []
    for _ in range(n_windows):
        t_offset = random.uniform(20, 60)
        smoke = []
        co = []
        temp = []
        for i in range(window_size):
            t = t_offset + i * 2
            s = 0.5 + 80.0 / (1.0 + math.exp(-0.15 * (t - 30))) + random.gauss(0, 3)
            c = 2.0 + 200.0 / (1.0 + math.exp(-0.12 * (t - 35))) + random.gauss(0, 5)
            tp = 21.0 + 0.5 * (t - 20) + random.gauss(0, 0.5)
            smoke.append(max(0.0, s))
            co.append(max(0.0, c))
            temp.append(tp)
        windows.append({"smoke": smoke, "co": co, "temperature": temp})
    return windows


def generate_false_alarm_windows(n_windows=50, window_size=10):
    """Generate false alarm data: smoke spikes without CO correlation."""
    windows = []
    for _ in range(n_windows):
        smoke_base = 0.5
        co_base = 2.0
        temp_base = 21.0

        smoke = []
        co = []
        temp = []
        alarm_peak = random.randint(3, 7)
        for i in range(window_size):
            s = smoke_base + random.gauss(0, 0.15)
            if abs(i - alarm_peak) <= 2:
                s += 8.0 * math.exp(-0.5 * ((i - alarm_peak) / 1.5) ** 2)
            smoke.append(max(0.0, s))
            co.append(max(0.0, co_base + random.gauss(0, 0.3)))
            temp.append(temp_base + random.gauss(0, 0.1))
        windows.append({"smoke": smoke, "co": co, "temperature": temp})
    return windows


def train_detector(save_path="models/detector.pkl", onnx_path="models/detector.onnx"):
    """Train and save the fire detector."""
    detector = FireDetector()

    # Generate training data (normal only)
    normal = generate_normal_windows(n_windows=1000)
    logger.info(f"Generated {len(normal)} normal training windows")

    detector.train(normal)

    # Test on all three scenarios
    test_normal = generate_normal_windows(n_windows=100)
    test_fire = generate_fire_windows(n_windows=50)
    test_false = generate_false_alarm_windows(n_windows=50)

    correct_normal = sum(1 for w in test_normal if not detector.predict(w)[0])
    correct_fire = sum(1 for w in test_fire if detector.predict(w)[0])
    correct_false = sum(1 for w in test_false if not detector.predict(w)[0])

    logger.info(f"Normal correctly classified: {correct_normal}/100")
    logger.info(f"Fires correctly detected: {correct_fire}/50")
    logger.info(f"False alarms correctly ignored: {correct_false}/50")

    detector.save(save_path)
    detector.export_onnx(onnx_path)

    return detector


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    train_detector()

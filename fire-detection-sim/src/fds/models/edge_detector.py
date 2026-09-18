"""Edge detector — runs on Raspberry Pi with ONNX Runtime.

Lightweight inference: loads ONNX model, runs anomaly detection locally.
No scikit-learn needed.
"""

import logging

import numpy as np
import onnxruntime as ort

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class EdgeDetector:
    def __init__(self, model_path="detector.onnx"):
        self.session = ort.InferenceSession(model_path)
        self.input_name = self.session.get_inputs()[0].name
        logger.info(f"ONNX model loaded: {model_path}")

    def predict(self, readings):
        smoke = readings.get("smoke", [0.0])
        co = readings.get("co", [0.0])
        temp = readings.get("temperature", [0.0])

        features = np.array(
            [
                [
                    smoke[-1],
                    co[-1],
                    temp[-1],
                    smoke[-1] - smoke[0] if len(smoke) > 1 else 0.0,
                    co[-1] - co[0] if len(co) > 1 else 0.0,
                    temp[-1] - temp[0] if len(temp) > 1 else 0.0,
                ]
            ],
            dtype=np.float32,
        )

        outputs = self.session.run(None, {self.input_name: features})
        label = int(outputs[0][0])
        is_anomaly = label == -1

        return is_anomaly, label


def run_test():
    detector = EdgeDetector("detector.onnx")

    normal = {"smoke": [0.3, 0.5, 0.4], "co": [2.0, 2.1, 1.9], "temperature": [21.0, 21.1, 21.0]}
    is_fire, score = detector.predict(normal)
    print(f"Normal:  is_fire={is_fire}, label={score}")

    fire = {"smoke": [0.5, 20.0, 45.0], "co": [2.0, 50.0, 120.0], "temperature": [21.0, 28.0, 35.0]}
    is_fire, score = detector.predict(fire)
    print(f"Fire:    is_fire={is_fire}, label={score}")

    false_alarm = {
        "smoke": [0.5, 8.0, 12.0],
        "co": [2.0, 2.1, 2.0],
        "temperature": [21.0, 21.0, 21.1],
    }
    is_fire, score = detector.predict(false_alarm)
    print(f"False:   is_fire={is_fire}, label={score}")

    print("\nEdge detector test complete!")


if __name__ == "__main__":
    run_test()

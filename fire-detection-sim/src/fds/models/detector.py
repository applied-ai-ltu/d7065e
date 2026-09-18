"""Anomaly detection using isolation forest.

Trains on normal sensor data (smoke, CO, temperature).
During inference, flags anomalies as potential fires.
Multi-sensor correlation is the key signal.
"""

import logging
import pickle
from pathlib import Path

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)


class FireDetector:
    def __init__(self, contamination=0.05, n_estimators=200):
        self.model = IsolationForest(
            contamination=contamination,
            n_estimators=n_estimators,
            random_state=42,
        )
        self.scaler = StandardScaler()
        self.is_fitted = False
        self.feature_names = ["smoke", "co", "temperature", "smoke_rate", "co_rate", "temp_rate"]

    def _extract_features(self, readings):
        """Extract features from a window of sensor readings.

        readings: dict with keys smoke, co, temperature — each a list of recent values.
        Returns: feature vector [smoke, co, temp, smoke_rate, co_rate, temp_rate]
        """
        smoke = readings.get("smoke", [0.0])
        co = readings.get("co", [0.0])
        temp = readings.get("temperature", [0.0])

        features = [
            smoke[-1],
            co[-1],
            temp[-1],
            smoke[-1] - smoke[0] if len(smoke) > 1 else 0.0,
            co[-1] - co[0] if len(co) > 1 else 0.0,
            temp[-1] - temp[0] if len(temp) > 1 else 0.0,
        ]
        return features

    def train(self, normal_data):
        """Train on a list of feature vectors from normal operation.

        normal_data: list of dicts, each with smoke, co, temperature lists.
        """
        X = np.array([self._extract_features(d) for d in normal_data])
        self.scaler.fit(X)
        X_scaled = self.scaler.transform(X)
        self.model.fit(X_scaled)
        self.is_fitted = True
        logger.info(f"Detector trained on {len(X)} samples")

    def predict(self, readings):
        """Predict if current readings are anomalous.

        Returns: (is_fire: bool, score: float, features: dict)
        score: lower = more anomalous (-1 to 1 range)
        """
        if not self.is_fitted:
            raise RuntimeError("Detector not trained")

        features = self._extract_features(readings)
        X = np.array([features])
        X_scaled = self.scaler.transform(X)

        score = self.model.decision_function(X_scaled)[0]
        prediction = self.model.predict(X_scaled)[0]
        is_fire = prediction == -1

        feature_dict = dict(zip(self.feature_names, features))
        return is_fire, float(score), feature_dict

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({"model": self.model, "scaler": self.scaler}, f)
        logger.info(f"Detector saved to {path}")

    def load(self, path):
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.model = data["model"]
        self.scaler = data["scaler"]
        self.is_fitted = True
        logger.info(f"Detector loaded from {path}")

    def export_onnx(self, path):
        """Export to ONNX for edge deployment."""
        try:
            import onnx
            from skl2onnx import convert_sklearn
            from skl2onnx.common.data_types import FloatTensorType

            initial_type = [("features", FloatTensorType([None, len(self.feature_names)]))]

            # Build a pipeline with scaler + model
            from sklearn.pipeline import Pipeline

            pipe = Pipeline([("scaler", self.scaler), ("detector", self.model)])

            onnx_model = convert_sklearn(
                pipe, initial_types=initial_type, target_opset={"": 17, "ai.onnx.ml": 3}
            )
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            onnx.save_model(onnx_model, str(path))
            logger.info(f"ONNX model exported to {path}")
        except ImportError:
            logger.warning("skl2onnx not installed, skipping ONNX export")

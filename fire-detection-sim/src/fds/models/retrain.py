"""ClearML-managed isolation-forest retraining for the edge tier.

Data sources (TimescaleDB):
  sensor_readings  -> per-room 5 s windows; "normal" = below the edge thresholds
  fire_events      -> real windows labelled by the LLM (Tier 2) verdict
  edge_decisions   -> IF verdicts + latency as seen on the edge
  llm_overrides    -> LLM verdict per alert (confirm / watch -> stand_down / escalated)

Outputs: models/detector.pkl, models/detector.onnx, edge/detector.onnx, ClearML task.
"""

import asyncio
import json
import logging
import os
import random
import shutil
from collections import defaultdict
from pathlib import Path

import asyncpg
import numpy as np

from fds.config import Config
from fds.models.detector import FireDetector
from fds.models.trainer import (
    generate_false_alarm_windows,
    generate_fire_windows,
    generate_normal_windows,
)
from fds.secrets import get_secrets

logger = logging.getLogger(__name__)

WINDOW = 10  # buckets of BUCKET_S -> 50 s window, same shape as the edge deque
BUCKET_S = 5
HOURS = float(os.environ.get("RETRAIN_HOURS", "48"))
MAX_TRAIN_WINDOWS = int(os.environ.get("RETRAIN_MAX_WINDOWS", "20000"))
# "normal" = would not even become a threshold candidate on the edge (TH_SMOKE / TH_CO / TH_TEMP)
TH_SMOKE, TH_CO, TH_TEMP = 2.0, 5.0, 25.0
CONTAMINATION, N_ESTIMATORS = 0.02, 200
NUISANCE_FRACTION = 0.10  # smoke-only excursions on real baselines, treated as non-fire


# ------------------------------------------------------------------ data
async def _connect(config):
    return await asyncpg.connect(
        host=config.database.host,
        port=config.database.port,
        user=config.database.user,
        password=config.database.password,
        database=config.database.database,
    )


async def fetch_series(conn, hours):
    """Per-room pivoted 5 s buckets -> {room: [(bucket, smoke, co, temp), ...]} sorted by time."""
    rows = await conn.fetch(
        f"""
        SELECT time_bucket('{BUCKET_S} seconds', time) AS bucket, room,
               MAX(CASE WHEN sensor_type = 'smoke' THEN value END) AS smoke,
               MAX(CASE WHEN sensor_type = 'co' THEN value END) AS co,
               MAX(CASE WHEN sensor_type = 'temperature' THEN value END) AS temperature
        FROM fire_detection_sim.sensor_readings
        WHERE time > NOW() - INTERVAL '{hours} hours'
        GROUP BY bucket, room
        ORDER BY room, bucket
        """
    )
    series = defaultdict(list)
    for r in rows:
        if r["smoke"] is None or r["co"] is None or r["temperature"] is None:
            continue
        series[r["room"]].append(
            (r["bucket"], float(r["smoke"]), float(r["co"]), float(r["temperature"]))
        )
    logger.info(f"Fetched {len(rows)} buckets for {len(series)} rooms ({hours:.0f} h)")
    return series


def _window(points):
    return {
        "smoke": [p[1] for p in points],
        "co": [p[2] for p in points],
        "temperature": [p[3] for p in points],
    }


def _is_normal(w):
    return max(w["smoke"]) < TH_SMOKE and max(w["co"]) < TH_CO and max(w["temperature"]) < TH_TEMP


def normal_windows(series, max_windows):
    """Non-overlapping per-room windows that stay below the edge thresholds."""
    out = []
    for pts in series.values():
        for i in range(0, len(pts) - WINDOW + 1, WINDOW):
            w = _window(pts[i : i + WINDOW])
            if _is_normal(w):
                out.append(w)
    random.seed(42)
    if len(out) > max_windows:
        out = random.sample(out, max_windows)
    return out


def nuisance_windows(real_normal, n):
    """Smoke-only excursions (nuisance alarms) on real baselines; CO and temperature untouched.

    Teaches the IF that a single-sensor excursion is non-fire operation, so only
    multi-sensor correlated rises (smoke + CO + temperature) score as anomalies.
    """
    random.seed(7)
    out = []
    for w in random.sample(real_normal, min(n, len(real_normal))):
        peak = random.randint(3, WINDOW - 2)
        amp = random.uniform(2.0, 15.0)
        smoke = [v + amp * np.exp(-0.5 * ((i - peak) / 1.5) ** 2) for i, v in enumerate(w["smoke"])]
        out.append({"smoke": smoke, "co": list(w["co"]), "temperature": list(w["temperature"])})
    return out


def split(items, frac=0.8, seed=3):
    items = list(items)
    random.Random(seed).shuffle(items)
    k = int(len(items) * frac)
    return items[:k], items[k:]


async def fetch_llm_labelled(conn, series, hours):
    """Real windows ending at each LLM assessment, labelled by the LLM's is_fire verdict."""
    rows = await conn.fetch(
        f"""
        SELECT time, room, details FROM fire_detection_sim.fire_events
        WHERE time > NOW() - INTERVAL '{hours} hours' AND details IS NOT NULL
        """
    )
    labelled = []
    for r in rows:
        try:
            d = r["details"] if isinstance(r["details"], dict) else json.loads(r["details"])
            is_fire = bool(d["assessment"]["is_fire"])
        except Exception:
            continue
        pts = series.get(r["room"])
        if not pts:
            continue
        # last bucket at or before the assessment time
        idx = max((i for i, p in enumerate(pts) if p[0] <= r["time"]), default=None)
        if idx is None or idx + 1 < WINDOW:
            continue
        labelled.append((_window(pts[idx + 1 - WINDOW : idx + 1]), is_fire, r["room"]))
    logger.info(
        f"LLM-labelled real windows: {len(labelled)} "
        f"(fire={sum(1 for _, f, _ in labelled if f)}, "
        f"not_fire={sum(1 for _, f, _ in labelled if not f)})"
    )
    return labelled


async def fetch_edge_vs_llm(conn, hours):
    """Edge alerts joined to the LLM's verdict; plus IF latency as measured on the edge."""
    alerts = await conn.fetch(
        f"""
        SELECT time, room, level, severity, if_anomaly, if_ms
        FROM fire_detection_sim.edge_decisions
        WHERE kind = 'alert' AND severity IN ('warning', 'confirmed', 'critical')
          AND time > NOW() - INTERVAL '{hours} hours'
        ORDER BY time
        """
    )
    overrides = await conn.fetch(
        f"""
        SELECT time, room, override_type FROM fire_detection_sim.llm_overrides
        WHERE time > NOW() - INTERVAL '{hours} hours' ORDER BY time
        """
    )
    by_room = defaultdict(list)
    for o in overrides:
        by_room[o["room"]].append((o["time"], o["override_type"]))

    def llm_verdict(alert):
        after = [(t, k) for t, k in by_room.get(alert["room"], []) if t >= alert["time"]]
        first = next(((t, k) for t, k in after if (t - alert["time"]).total_seconds() < 60), None)
        if not first:
            return None
        if first[1] in ("confirm", "escalated"):
            return "fire"
        if first[1] == "watch_pending":
            later = next(
                (k for t, k in after if t > first[0] and (t - first[0]).total_seconds() < 600), None
            )
            return {"stand_down": "false_alarm", "escalated": "fire"}.get(later)
        return None

    pairs = []
    for a in alerts:
        v = llm_verdict(a)
        if v is not None and a["if_anomaly"] is not None:
            pairs.append((bool(a["if_anomaly"]), v == "fire"))
    latencies = [
        float(a["if_ms"])
        for a in (
            await conn.fetch(
                f"""SELECT if_ms FROM fire_detection_sim.edge_decisions
            WHERE if_ms IS NOT NULL AND time > NOW() - INTERVAL '{hours} hours'"""
            )
        )
        if a["if_ms"] is not None
    ]
    logger.info(f"Edge-vs-LLM pairs: {len(pairs)}, IF latency samples: {len(latencies)}")
    return pairs, latencies


# ------------------------------------------------------------- evaluation
def evaluate(detector, labelled):
    """Agreement of the detector with LLM labels on real windows."""
    if not labelled:
        return {}
    tp = fn = tn = fp = 0
    for w, is_fire, _ in labelled:
        pred = detector.predict(w)[0]
        if is_fire and pred:
            tp += 1
        elif is_fire:
            fn += 1
        elif pred:
            fp += 1
        else:
            tn += 1
    n = tp + fn + tn + fp
    return {
        "real_agreement": (tp + tn) / n,
        "real_fire_detection": tp / (tp + fn) if tp + fn else 0.0,
        "real_false_alarm_rejection": tn / (tn + fp) if tn + fp else 0.0,
        "_matrix": [[tn, fp], [fn, tp]],
    }


def evaluate_synthetic(detector):
    test_fire = generate_fire_windows(n_windows=50)
    test_false = generate_false_alarm_windows(n_windows=50)
    return {
        "fire_detection_rate": sum(1 for w in test_fire if detector.predict(w)[0]) / 50,
        "false_alarm_rejection": sum(1 for w in test_false if not detector.predict(w)[0]) / 50,
    }


def onnx_parity(detector, onnx_path, windows):
    """Fraction of windows where ONNX (as the edge runs it) agrees with sklearn."""
    try:
        import onnxruntime as ort
    except ImportError:
        return None
    sess = ort.InferenceSession(str(onnx_path))
    name = sess.get_inputs()[0].name
    agree = 0
    for w in windows:
        x = np.array([detector._extract_features(w)], dtype=np.float32)
        out = sess.run(None, {name: x})
        onnx_fire = int(np.asarray(out[0]).ravel()[0]) == -1
        agree += onnx_fire == detector.predict(w)[0]
    return agree / len(windows) if windows else None


# --------------------------------------------------------------- pipeline
async def retrain(config, task=None):
    log = task.get_logger() if task else None

    def scalar(section, name, value, it=0):
        logger.info(
            f"  {section}/{name}: {value:.3f}"
            if isinstance(value, float)
            else f"  {section}/{name}: {value}"
        )
        if log:
            log.report_scalar(section, name, value, it)

    conn = await _connect(config)
    try:
        series = await fetch_series(conn, HOURS)
        labelled = await fetch_llm_labelled(conn, series, HOURS)
        pairs, latencies = await fetch_edge_vs_llm(conn, HOURS)
    finally:
        await conn.close()

    real_normal = normal_windows(series, MAX_TRAIN_WINDOWS)
    synthetic_added = 0
    if len(real_normal) < 500:
        synthetic_added = 1000
        real_normal.extend(generate_normal_windows(n_windows=synthetic_added))
    normal_train, normal_test = split(real_normal)
    # LLM feedback: windows the LLM rejected are non-fire operation for the IF
    rejected_train, rejected_test = split([(w, f, r) for w, f, r in labelled if not f])
    fires = [(w, f, r) for w, f, r in labelled if f]
    nuisance = nuisance_windows(normal_train, int(len(normal_train) * NUISANCE_FRACTION))
    train = normal_train + nuisance + [w for w, _, _ in rejected_train] * 20
    eval_set = [(w, False, r) for w, r in zip(normal_test, ["_normal"] * len(normal_test))]
    eval_set += fires + rejected_test
    scalar("data", "training_windows_real_normal", len(normal_train))
    scalar("data", "training_windows_nuisance", len(nuisance))
    scalar("data", "training_windows_llm_rejected", len(rejected_train))
    scalar("data", "training_windows_synthetic", synthetic_added)
    scalar("data", "rooms", len(series))
    scalar("data", "llm_labelled_windows", len(labelled))
    scalar("data", "eval_real_normal", len(normal_test))
    scalar("data", "eval_llm_fire", len(fires))
    scalar("data", "eval_llm_rejected_heldout", len(rejected_test))

    # Previous model on the same real set (baseline)
    prev_path = Path("models/detector.pkl")
    if prev_path.exists() and labelled:
        prev = FireDetector()
        prev.load(prev_path)
        for k, v in evaluate(prev, eval_set).items():
            if not k.startswith("_"):
                scalar("baseline_previous_model", k, v)

    detector = FireDetector(contamination=CONTAMINATION, n_estimators=N_ESTIMATORS)
    detector.train(train)

    metrics = evaluate_synthetic(detector)
    real = evaluate(detector, eval_set)
    real_norm_ok = sum(1 for w in normal_test if not detector.predict(w)[0])
    real["real_normal_accuracy"] = real_norm_ok / len(normal_test) if normal_test else 0.0
    if rejected_test:
        rej_ok = sum(1 for w, _, _ in rejected_test if not detector.predict(w)[0])
        real["real_llm_rejected_heldout_rejection"] = rej_ok / len(rejected_test)
    metrics.update({k: v for k, v in real.items() if not k.startswith("_")})
    for k, v in metrics.items():
        scalar("evaluation", k, v)
    if log and "_matrix" in real:
        log.report_confusion_matrix(
            "confusion_matrix",
            "IF vs LLM label (real windows)",
            matrix=np.array(real["_matrix"]),
            xlabels=["IF normal", "IF anomaly"],
            ylabels=["LLM not fire", "LLM fire"],
            iteration=0,
        )

    # Edge-vs-LLM agreement as it actually happened on the edge
    if pairs:
        agree = sum(1 for a, b in pairs if a == b) / len(pairs)
        if_only = sum(1 for a, b in pairs if a and not b)
        llm_only = sum(1 for a, b in pairs if b and not a)
        scalar("edge_vs_llm", "agreement", agree)
        scalar("edge_vs_llm", "if_anomaly_llm_rejected", if_only)
        scalar("edge_vs_llm", "llm_fire_if_missed", llm_only)
        scalar("edge_vs_llm", "pairs", len(pairs))
        metrics["edge_llm_agreement"] = agree
    if latencies:
        lat = np.array(latencies)
        scalar("edge_latency_ms", "p50", float(np.percentile(lat, 50)))
        scalar("edge_latency_ms", "p95", float(np.percentile(lat, 95)))
        scalar("edge_latency_ms", "max", float(lat.max()))
        if log:
            log.report_histogram("if_latency_ms", "edge", values=latencies, iteration=0)

    # Save + export + parity
    os.makedirs("models", exist_ok=True)
    detector.save("models/detector.pkl")
    detector.export_onnx("models/detector.onnx")
    if Path("models/detector.onnx").exists():
        shutil.copy("models/detector.onnx", "edge/detector.onnx")
        parity = onnx_parity(detector, "models/detector.onnx", [w for w, _, _ in eval_set][:400])
        if parity is not None:
            scalar("export", "onnx_sklearn_parity", parity)
            metrics["onnx_parity"] = parity

    if task:
        task.connect(metrics, name="evaluation_metrics")
        task.connect(
            {
                "hours": HOURS,
                "window": WINDOW,
                "bucket_s": BUCKET_S,
                "contamination": CONTAMINATION,
                "nuisance_fraction": NUISANCE_FRACTION,
                "llm_rejected_weight": 20,
                "n_estimators": N_ESTIMATORS,
                "thresholds": {"smoke": TH_SMOKE, "co": TH_CO, "temperature": TH_TEMP},
                "training_windows": len(train),
            },
            name="training_config",
        )
        task.upload_artifact("detector_pkl", "models/detector.pkl")
        task.upload_artifact("detector_onnx", "models/detector.onnx")
        task.get_logger().flush()
    return detector, metrics


async def retrain_with_clearml(config):
    task = None
    try:
        from clearml import Task

        task = Task.init(
            project_name="d7065e",
            task_name="detector-retrain",
            tags=["two-tier", "edge"],
            auto_connect_frameworks=False,
        )
    except Exception as e:
        logger.warning(f"ClearML unavailable ({e}); retraining without tracking")
    try:
        return await retrain(config, task)
    finally:
        if task:
            task.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = Config.from_yaml("configs/config.yaml")
    config.database.password = get_secrets().get("db_password", "")
    _, m = asyncio.run(retrain_with_clearml(config))
    print("Done:", json.dumps(m, indent=2))

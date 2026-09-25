"""Explainable load-cell signal analysis for the software/hardware boundary.

The detector is advisory: an anomaly is not medication removal, and removal
does not prove consumption. Training/evaluation uses deterministic synthetic
signals because physical sensor data is not available yet.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import precision_score, recall_score, f1_score

SENSOR_MODEL_VERSION = "sensor-iforest-v1"
MODEL_PATH = Path(os.environ.get(
    "SENSOR_MODEL_PATH",
    str(Path(__file__).resolve().parent / "instance" / "sensor_anomaly_model.joblib"),
))


def generate_sensor_noise(
    baseline: float = 100.0,
    length: int = 40,
    noise_std: float = 0.08,
    drift: float = 0.0,
    seed: int = 42,
) -> list[float]:
    """Generate repeatable HX711-like readings with configurable noise."""
    rng = np.random.default_rng(seed)
    steps = np.linspace(0.0, drift, length)
    return (baseline + steps + rng.normal(0.0, noise_std, length)).round(4).tolist()


def rolling_median(values: list[float], window: int = 5) -> list[float]:
    if window < 1 or window % 2 == 0:
        raise ValueError("rolling median window must be a positive odd number")
    result = []
    for index in range(len(values)):
        start = max(0, index - window + 1)
        result.append(float(np.median(values[start:index + 1])))
    return result


def extract_window_features(
    readings: list[float],
    filtered: list[float],
    sample_period_seconds: float = 1.0,
) -> list[float]:
    if not readings:
        raise ValueError("at least one sensor reading is required")
    values = np.asarray(filtered, dtype=float)
    change = float(values[-1] - values[0])
    duration = max(0.0, (len(values) - 1) * sample_period_seconds)
    return [
        change,
        float(np.var(values)),
        float(np.std(np.diff(values))) if len(values) > 1 else 0.0,
        duration,
        change / duration if duration else 0.0,
    ]


def _model_artifact() -> dict[str, Any]:
    if MODEL_PATH.exists():
        try:
            return joblib.load(MODEL_PATH)
        except (OSError, ValueError, KeyError, EOFError):
            pass
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    stable = [
        extract_window_features(
            generate_sensor_noise(100, 40, noise_std=std, seed=seed),
            rolling_median(generate_sensor_noise(100, 40, noise_std=std, seed=seed)),
        )
        for std, seed in ((0.03, 1), (0.05, 2), (0.08, 3), (0.1, 4), (0.12, 5))
    ]
    normal = stable + [
        extract_window_features(
            [100.0] * 20 + [95.0] * 20,
            rolling_median([100.0] * 20 + [95.0] * 20),
        )
    ]
    model = IsolationForest(
        n_estimators=120, contamination=0.15, random_state=42
    ).fit(np.asarray(normal))
    artifact = {"model": model, "model_version": SENSOR_MODEL_VERSION}
    joblib.dump(artifact, MODEL_PATH)
    return artifact


def analyze_readings(
    readings: list[float],
    expected_removal: float,
    tolerance: float,
    calibrated_offset: float = 0.0,
    noise_threshold: float = 0.15,
    median_window: int = 5,
    sample_period_seconds: float = 1.0,
) -> dict[str, Any]:
    """Compare calibrated threshold detection with Isolation Forest analysis."""
    if len(readings) < 2:
        raise ValueError("at least two sensor readings are required")
    if not all(np.isfinite(float(value)) for value in readings):
        raise ValueError("sensor readings must be finite")
    calibrated = [float(value) + calibrated_offset for value in readings]
    filtered = rolling_median(calibrated, median_window)
    features = extract_window_features(calibrated, filtered, sample_period_seconds)
    change, variance, stability, duration, change_rate = features
    threshold_removal = abs(-change - expected_removal) <= tolerance
    threshold_event = (
        "normal_removal" if threshold_removal
        else "unexpected_increase" if change > noise_threshold
        else "excessive_removal" if -change > expected_removal + tolerance
        else "unstable_reading" if stability > noise_threshold
        else "no_removal"
    )
    artifact = _model_artifact()
    score = float(artifact["model"].decision_function(np.asarray([features]))[0])
    anomaly = bool(artifact["model"].predict(np.asarray([features]))[0] == -1)
    anomaly_type = threshold_event if anomaly else "none"
    return {
        "model_version": SENSOR_MODEL_VERSION,
        "raw_readings": calibrated,
        "filtered_readings": filtered,
        "features": {
            "weight_change": change,
            "variance": variance,
            "stability": stability,
            "duration_seconds": duration,
            "change_rate": change_rate,
        },
        "threshold_detection": {
            "event": threshold_event,
            "removal_detected": threshold_removal,
        },
        "anomaly_detection": {
            "is_anomaly": anomaly,
            "anomaly_score": round(score, 6),
            "event": anomaly_type,
        },
        "interpretation": (
            "Anomaly detection is advisory and does not confirm medication removal "
            "or consumption."
        ),
    }


def generate_labeled_scenarios(seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    scenarios: list[list[float]] = []
    labels: list[int] = []
    for index in range(30):
        scenarios.append(extract_window_features(
            generate_sensor_noise(100, 40, noise_std=0.06, seed=seed + index),
            rolling_median(generate_sensor_noise(100, 40, noise_std=0.06, seed=seed + index)),
        ))
        labels.append(0)
        scenarios.append(extract_window_features(
            generate_sensor_noise(100, 40, noise_std=0.06, seed=seed + index, drift=-5),
            rolling_median(generate_sensor_noise(100, 40, noise_std=0.06, seed=seed + index, drift=-5)),
        ))
        labels.append(0)
        unexpected = generate_sensor_noise(100, 40, noise_std=0.3, seed=seed + index, drift=4)
        scenarios.append(extract_window_features(unexpected, rolling_median(unexpected)))
        labels.append(1)
    return np.asarray(scenarios), np.asarray(labels)


def evaluate_sensor_model() -> dict[str, Any]:
    features, labels = generate_labeled_scenarios()
    split = int(len(features) * 0.8)
    artifact = _model_artifact()
    predictions = artifact["model"].predict(features[split:]) == -1
    truth = labels[split:] == 1
    return {
        "model_version": SENSOR_MODEL_VERSION,
        "data_source": "deterministic synthetic load-cell scenarios",
        "validation_limitations": "Synthetic signals are not representative of calibrated physical hardware.",
        "test_rows": len(truth),
        "precision": round(float(precision_score(truth, predictions, zero_division=0)), 4),
        "recall": round(float(recall_score(truth, predictions, zero_division=0)), 4),
        "f1": round(float(f1_score(truth, predictions, zero_division=0)), 4),
        "scenarios": ["normal", "removal", "unexpected increase", "unstable/noisy"],
    }


def demo_sensor_analyses() -> list[dict[str, Any]]:
    """Return explicitly labelled synthetic examples for the AI Lab."""
    scenarios = (
        ("Stable / no removal", [100.0] * 20, 5.0),
        ("Normal removal", [100.0] * 12 + [95.0] * 12, 5.0),
        ("Unexpected increase", [100.0] * 12 + [104.0] * 12, 5.0),
        ("Unstable readings", generate_sensor_noise(100.0, 24, 0.65, seed=91), 5.0),
    )
    return [
        {
            "scenario": name,
            **analyze_readings(readings, expected, 0.3),
        }
        for name, readings, expected in scenarios
    ]

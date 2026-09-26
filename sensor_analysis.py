"""Calibrated weight-sensor analysis with a held-out synthetic experiment.

Synthetic data covers normal/no-removal weight windows and four sensor
conditions. It is engineering test data, not calibrated physical sensor data.
Neither a weight change nor an anomaly detection result proves ingestion.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

SENSOR_MODEL_VERSION = "sensor-iforest-heldout-v2"
SENSOR_DATA_SOURCE = "seeded synthetic HX711-like windows; separate train and holdout seeds"
SENSOR_LIMITATIONS = (
    "Synthetic signals only; held-out synthetic scenarios are not representative "
    "of calibrated physical hardware or clinical outcomes."
)
FEATURE_NAMES = (
    "filtered_weight_change",
    "filtered_variance",
    "step_stability",
    "duration_seconds",
    "change_rate",
)
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "instance" / "sensor_experiment" / "model.joblib"
TRAIN_SEED = 2025
TEST_SEED = 90210
_artifact: dict[str, Any] | None = None
_loaded_path: Path | None = None


def model_path() -> Path:
    return Path(os.environ.get("SENSOR_MODEL_PATH", str(DEFAULT_MODEL_PATH)))


def generate_sensor_noise(
    baseline: float = 100.0,
    length: int = 40,
    noise_std: float = 0.08,
    drift: float = 0.0,
    seed: int = 42,
) -> list[float]:
    """Generate reproducible load-cell readings with a configurable drift."""
    if length < 2 or noise_std < 0 or not np.isfinite(noise_std + baseline + drift):
        raise ValueError("sensor generation requires finite values, length >= 2 and nonnegative noise")
    rng = np.random.default_rng(seed)
    steps = np.linspace(0.0, drift, length)
    return (baseline + steps + rng.normal(0.0, noise_std, length)).round(4).tolist()


def rolling_median(values: list[float], window: int = 5) -> list[float]:
    """Return a trailing rolling median; early windows use available samples."""
    if window < 1 or window % 2 == 0:
        raise ValueError("rolling median window must be a positive odd number")
    if not all(np.isfinite(value) for value in values):
        raise ValueError("sensor readings must be finite")
    return [
        float(np.median(values[max(0, index - window + 1):index + 1]))
        for index in range(len(values))
    ]


def extract_window_features(
    readings: list[float],
    filtered: list[float],
    sample_period_seconds: float = 1.0,
) -> list[float]:
    if not readings or len(readings) != len(filtered):
        raise ValueError("raw and filtered sensor windows must have equal nonzero lengths")
    if not np.isfinite(sample_period_seconds) or sample_period_seconds <= 0:
        raise ValueError("sample period must be a positive finite number")
    values = np.asarray(filtered, dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("sensor readings must be finite")
    change = float(values[-1] - values[0])
    duration = (len(values) - 1) * sample_period_seconds
    return [
        change,
        float(np.var(values)),
        float(np.std(np.diff(values))) if len(values) > 1 else 0.0,
        float(duration),
        change / duration if duration else 0.0,
    ]


def _window_features(
    scenario: str,
    rng: np.random.Generator,
) -> tuple[list[float], str, float, float]:
    length = int(rng.choice([32, 40, 48, 56]))
    baseline = float(rng.uniform(80, 180))
    tablet_weight = float(rng.uniform(3.0, 8.0))
    tolerance = max(0.2, tablet_weight * 0.08)
    noise = float(rng.uniform(0.025, 0.12))
    values = generate_sensor_noise(baseline, length, noise, seed=int(rng.integers(0, 2**31 - 1)))
    if scenario == "normal_removal":
        amount = tablet_weight + float(rng.uniform(-tolerance * 0.7, tolerance * 0.7))
        change_at = int(rng.integers(length // 3, (2 * length) // 3))
        values = [value - (amount if position >= change_at else 0.0) for position, value in enumerate(values)]
    elif scenario == "sensor_noise":
        values = generate_sensor_noise(
            baseline, length, float(rng.uniform(0.65, 1.15)),
            seed=int(rng.integers(0, 2**31 - 1)),
        )
    elif scenario == "unexpected_increase":
        amount = float(rng.uniform(2.0, 7.0))
        values = [value + (amount if position >= length // 2 else 0.0) for position, value in enumerate(values)]
    elif scenario == "excessive_removal":
        amount = tablet_weight + tolerance + float(rng.uniform(1.0, 4.0))
        values = [value - (amount if position >= length // 2 else 0.0) for position, value in enumerate(values)]
    elif scenario != "no_removal":
        raise ValueError(f"unknown sensor scenario: {scenario}")
    filtered = rolling_median(values)
    features = extract_window_features(values, filtered)
    threshold_event = _threshold_event(
        features[0], features[2], tablet_weight, tolerance, noise_threshold=0.15
    )
    return features, threshold_event, tablet_weight, tolerance


def _threshold_event(
    change: float,
    stability: float,
    expected_removal: float,
    tolerance: float,
    noise_threshold: float,
) -> str:
    removal = -change
    if abs(removal - expected_removal) <= tolerance:
        return "normal_removal"
    if stability > noise_threshold and abs(change) < max(noise_threshold, expected_removal * 0.5):
        return "sensor_noise"
    if change > noise_threshold:
        return "unexpected_increase"
    if removal > expected_removal + tolerance:
        return "excessive_removal"
    return "no_removal"


def _generate_scenario_set(
    seed: int,
    samples_per_class: int,
    scenarios: tuple[str, ...],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    rng = np.random.default_rng(seed)
    values: list[list[float]] = []
    anomaly_labels: list[int] = []
    threshold_labels: list[int] = []
    names: list[str] = []
    anomalous = {"sensor_noise", "unexpected_increase", "excessive_removal"}
    threshold_anomalies = anomalous
    for scenario in scenarios:
        for _ in range(samples_per_class):
            features, threshold_event, expected, tolerance = _window_features(scenario, rng)
            values.append(features)
            anomaly_labels.append(int(scenario in anomalous))
            threshold_labels.append(int(threshold_event in threshold_anomalies))
            names.append(scenario)
    return (
        np.asarray(values, dtype=float),
        np.asarray(anomaly_labels, dtype=int),
        np.asarray(threshold_labels, dtype=int),
        names,
    )


NORMAL_SCENARIOS = ("no_removal", "normal_removal")
HELD_OUT_SCENARIOS = (
    "no_removal",
    "normal_removal",
    "sensor_noise",
    "unexpected_increase",
    "excessive_removal",
)


def _train_artifact() -> dict[str, Any]:
    training_x, _, _, _ = _generate_scenario_set(TRAIN_SEED, 160, NORMAL_SCENARIOS)
    model = IsolationForest(
        n_estimators=200,
        contamination=0.15,
        random_state=TRAIN_SEED,
        n_jobs=-1,
    )
    model.fit(training_x)
    metadata = evaluate_model(model)
    return {
        "model": model,
        "metadata": metadata,
        "model_version": SENSOR_MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
    }


def _get_artifact() -> dict[str, Any]:
    global _artifact, _loaded_path
    path = model_path()
    if _artifact is not None and _loaded_path == path:
        return _artifact
    if path.exists():
        loaded = joblib.load(path)
        if (
            not isinstance(loaded, dict)
            or loaded.get("model_version") != SENSOR_MODEL_VERSION
            or not isinstance(loaded.get("model"), IsolationForest)
            or loaded.get("feature_names") != list(FEATURE_NAMES)
        ):
            raise RuntimeError("sensor model artifact is incompatible; retrain it with scripts/train_sensor_model.py")
        _artifact = loaded
    else:
        _artifact = _train_artifact()
    _loaded_path = path
    return _artifact


def analyze_readings(
    readings: list[float],
    expected_removal: float,
    tolerance: float,
    calibrated_offset: float = 0.0,
    noise_threshold: float = 0.15,
    median_window: int = 5,
    sample_period_seconds: float = 1.0,
) -> dict[str, Any]:
    """Compare calibrated threshold detection to a trained anomaly detector."""
    if len(readings) < 2:
        raise ValueError("at least two sensor readings are required")
    if not all(np.isfinite(float(value)) and 0 <= float(value) <= 100000 for value in readings):
        raise ValueError("sensor readings must be finite weights from 0 to 100000")
    if not np.isfinite(expected_removal) or expected_removal <= 0:
        raise ValueError("expected removal must be a positive finite weight")
    if not np.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be a nonnegative finite weight")
    if not np.isfinite(calibrated_offset) or not np.isfinite(noise_threshold) or noise_threshold <= 0:
        raise ValueError("calibration offset must be finite and noise threshold must be positive")
    calibrated = [float(value) + calibrated_offset for value in readings]
    if not all(0 <= value <= 100000 for value in calibrated):
        raise ValueError("calibrated sensor readings must be from 0 to 100000")
    filtered = rolling_median(calibrated, median_window)
    features = extract_window_features(calibrated, filtered, sample_period_seconds)
    change, variance, stability, duration, change_rate = features
    threshold_event = _threshold_event(change, stability, expected_removal, tolerance, noise_threshold)
    artifact = _get_artifact()
    score = float(artifact["model"].decision_function(np.asarray([features], dtype=float))[0])
    anomaly = bool(artifact["model"].predict(np.asarray([features], dtype=float))[0] == -1)
    return {
        "model_version": artifact["model_version"],
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
            "removal_detected": threshold_event == "normal_removal",
            "anomaly_flag": threshold_event in {"sensor_noise", "unexpected_increase", "excessive_removal"},
        },
        "anomaly_detection": {
            "is_anomaly": anomaly,
            "anomaly_score": round(score, 6),
            "event": threshold_event if anomaly else "none",
        },
        "interpretation": (
            "Weight change indicates a sensor event only. It cannot confirm medication removal "
            "or swallowing; manual confirmation and care protocols still apply."
        ),
    }


def evaluate_model(model: IsolationForest | None = None) -> dict[str, Any]:
    if model is None:
        model = _get_artifact()["model"]
    test_x, labels, threshold_predictions, scenario_names = _generate_scenario_set(
        TEST_SEED, 60, HELD_OUT_SCENARIOS
    )
    ml_predictions = (model.predict(test_x) == -1).astype(int)
    anomaly_scores = -model.decision_function(test_x)

    def metrics(predictions: np.ndarray, scores: np.ndarray | None = None) -> dict[str, Any]:
        return {
            "confusion_matrix": confusion_matrix(labels, predictions, labels=[0, 1]).tolist(),
            "precision": round(float(precision_score(labels, predictions, zero_division=0)), 4),
            "recall": round(float(recall_score(labels, predictions, zero_division=0)), 4),
            "f1": round(float(f1_score(labels, predictions, zero_division=0)), 4),
            "roc_auc": round(float(roc_auc_score(labels, scores)), 4)
            if scores is not None and len(np.unique(labels)) > 1 else None,
        }

    per_scenario = {}
    for scenario in HELD_OUT_SCENARIOS:
        indexes = np.asarray([index for index, name in enumerate(scenario_names) if name == scenario])
        per_scenario[scenario] = {
            "test_rows": int(len(indexes)),
            "expected_anomaly": scenario in {"sensor_noise", "unexpected_increase", "excessive_removal"},
            "isolation_forest_flag_rate": round(float(np.mean(ml_predictions[indexes])), 4),
            "threshold_flag_rate": round(float(np.mean(threshold_predictions[indexes])), 4),
        }
    return {
        "model_version": SENSOR_MODEL_VERSION,
        "data_source": SENSOR_DATA_SOURCE,
        "validation_limitations": SENSOR_LIMITATIONS,
        "feature_names": list(FEATURE_NAMES),
        "split_method": "Independent held-out scenarios generated with test seed 90210; training uses seed 2025 normal windows only.",
        "training_scenarios": list(NORMAL_SCENARIOS),
        "held_out_scenarios": list(HELD_OUT_SCENARIOS),
        "training_rows": 320,
        "test_rows": int(len(labels)),
        "test_seed": TEST_SEED,
        "isolation_forest": metrics(ml_predictions, anomaly_scores),
        "threshold_baseline": metrics(threshold_predictions),
        "per_scenario": per_scenario,
    }


def train_sensor_model(
    output_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    global _artifact, _loaded_path
    trained = _train_artifact()
    path = Path(output_path) if output_path else model_path()
    report = Path(report_path) if report_path else path.with_suffix(".metrics.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(trained, path)
    report.write_text(json.dumps(trained["metadata"], indent=2) + "\n", encoding="utf-8")
    _artifact, _loaded_path = trained, path
    return trained["metadata"]


def evaluate_sensor_model() -> dict[str, Any]:
    return _get_artifact()["metadata"]


def demo_sensor_analyses() -> list[dict[str, Any]]:
    """Return distinct repeatable examples matching the synthetic test labels."""
    rng = np.random.default_rng(TEST_SEED + 7)
    examples: list[tuple[str, list[float], float, float]] = []
    for name in HELD_OUT_SCENARIOS:
        expected = float(rng.uniform(3.0, 8.0))
        tolerance = max(0.2, expected * 0.08)
        readings = _scenario_readings_for_demo(name, expected, rng)
        examples.append((name.replace("_", " ").title(), readings, expected, tolerance))
    return [
        {
            "scenario": name,
            **analyze_readings(readings, expected, tolerance),
        }
        for name, readings, expected, tolerance in examples
    ]


def _scenario_readings_for_demo(
    scenario: str, expected: float, rng: np.random.Generator
) -> list[float]:
    baseline = float(rng.uniform(95, 150))
    length = 40
    values = generate_sensor_noise(baseline, length, 0.06, seed=int(rng.integers(0, 2**31 - 1)))
    midpoint = length // 2
    if scenario == "normal_removal":
        return [value - (expected if index >= midpoint else 0) for index, value in enumerate(values)]
    if scenario == "sensor_noise":
        return generate_sensor_noise(baseline, length, 0.95, seed=int(rng.integers(0, 2**31 - 1)))
    if scenario == "unexpected_increase":
        return [value + (4.0 if index >= midpoint else 0) for index, value in enumerate(values)]
    if scenario == "excessive_removal":
        return [value - (expected + 3.0 if index >= midpoint else 0) for index, value in enumerate(values)]
    return values

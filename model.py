"""Explainable, reproducible medication-adherence model.

Real labelled dose histories are not available yet, so training uses a
documented synthetic generator. Features for each upcoming dose are computed
only from doses before that dose; the target is whether the upcoming dose is
missed. This is a research/demo model and has no clinical validity.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline

MODEL_VERSION = "synthetic-rf-v1"
FEATURE_NAMES = (
    "hour_of_day",
    "prior_dose_count",
    "prior_missed_rate",
    "recent_missed_count",
    "average_response_delay_minutes",
    "schedule_variability_minutes",
)
MINIMUM_HISTORY = 3
_model: Pipeline | None = None
_metadata: dict[str, Any] | None = None


def model_path() -> Path:
    return Path(os.environ.get(
        "ADHERENCE_MODEL_PATH",
        str(Path(__file__).resolve().parent / "instance" / "adherence_model.joblib"),
    ))


def generate_synthetic_dataset(
    patients: int = 80, doses_per_patient: int = 28, seed: int = 42
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate chronological synthetic dose observations.

    Each patient receives a stable latent adherence tendency and schedule
    pattern. The target is sampled for the current dose, while the feature
    vector contains only prior observations.
    """
    rng = np.random.default_rng(seed)
    rows: list[list[float]] = []
    targets: list[int] = []
    timestamps: list[datetime] = []
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for patient in range(patients):
        tendency = float(rng.beta(2.5, 5.0))
        hour = int(rng.choice([7, 8, 12, 18, 20, 22]))
        prior_missed: list[int] = []
        prior_delays: list[float] = []
        for dose_index in range(doses_per_patient):
            current_time = start + timedelta(days=dose_index, hours=patient % 5)
            missed_rate = sum(prior_missed) / len(prior_missed) if prior_missed else 0.0
            rows.append([
                float(hour),
                float(dose_index),
                missed_rate,
                float(sum(prior_missed[-7:])),
                float(np.mean(prior_delays[-10:])) if prior_delays else 0.0,
                float(abs(hour - 12) / 2),
            ])
            risk = min(
                0.95,
                max(0.03, 0.08 + tendency * 0.55 + missed_rate * 0.35
                    + (0.08 if hour < 7 or hour > 21 else 0.0)),
            )
            missed = int(rng.random() < risk)
            delay = float(rng.normal(65 if missed else 12, 15))
            prior_missed.append(missed)
            prior_delays.append(max(0.0, delay))
            targets.append(missed)
            timestamps.append(current_time)
    order = np.argsort([timestamp.timestamp() for timestamp in timestamps])
    return np.asarray(rows)[order], np.asarray(targets)[order], np.asarray(timestamps, dtype=object)[order]


def _metrics(y_true: np.ndarray, probabilities: np.ndarray, baseline: bool = False) -> dict[str, Any]:
    predictions = (probabilities >= 0.5).astype(int)
    result: dict[str, Any] = {
        "confusion_matrix": confusion_matrix(y_true, predictions).tolist(),
        "precision": round(float(precision_score(y_true, predictions, zero_division=0)), 4),
        "recall": round(float(recall_score(y_true, predictions, zero_division=0)), 4),
        "f1": round(float(f1_score(y_true, predictions, zero_division=0)), 4),
        "roc_auc": round(float(roc_auc_score(y_true, probabilities)), 4)
        if len(np.unique(y_true)) > 1 else None,
    }
    if baseline:
        result["type"] = "majority-class baseline"
    return result


def train_model() -> Pipeline:
    """Train, evaluate, and persist the chronological synthetic-data model."""
    global _model, _metadata
    features, targets, timestamps = generate_synthetic_dataset()
    split = int(len(features) * 0.8)
    train_x, test_x = features[:split], features[split:]
    train_y, test_y = targets[:split], targets[split:]
    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("classifier", RandomForestClassifier(
            n_estimators=160, max_depth=8, min_samples_leaf=3,
            random_state=42, class_weight="balanced",
        )),
    ])
    pipeline.fit(train_x, train_y)
    probabilities = pipeline.predict_proba(test_x)[:, 1]
    majority_probability = float(np.mean(train_y))
    metadata = {
        "model_version": MODEL_VERSION,
        "data_source": "deterministic synthetic dose histories",
        "validation_limitations": "Synthetic data only; chronological holdout is not clinical validation.",
        "feature_names": list(FEATURE_NAMES),
        "minimum_history": MINIMUM_HISTORY,
        "training_rows": len(train_x),
        "test_rows": len(test_x),
        "train_end": timestamps[split - 1].isoformat(),
        "test_start": timestamps[split].isoformat(),
        "evaluation": _metrics(test_y, probabilities),
        "baseline": _metrics(test_y, np.full(len(test_y), majority_probability), baseline=True),
    }
    artifact = {"pipeline": pipeline, "metadata": metadata}
    path = model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, path)
    _model, _metadata = pipeline, metadata
    return pipeline


def _load_model() -> None:
    global _model, _metadata
    try:
        artifact = joblib.load(model_path())
        _model = artifact["pipeline"]
        _metadata = artifact["metadata"]
    except (FileNotFoundError, OSError, KeyError, ValueError, EOFError):
        train_model()


def _features_for_patient(patient_id: int) -> tuple[list[float], int, dict[str, Any]]:
    from app import PillLog, Reminder, db, utc_now

    logs = db.session.scalars(
        db.select(PillLog).where(PillLog.patient_id == patient_id)
        .order_by(PillLog.timestamp.asc()).limit(100)
    ).all()
    reminders = db.session.scalars(
        db.select(Reminder).where(Reminder.patient_id == patient_id, Reminder.status == "Active")
    ).all()
    if not reminders:
        return [12.0, float(len(logs)), 0.0, 0.0, 0.0, 0.0], len(logs), {
            "factors": ["No active schedule is available."],
        }
    reminder = reminders[0]
    prior_missed = sum(log.status == "Missed" for log in logs)
    delays = [max(0.0, abs((log.timestamp.hour * 60 + log.timestamp.minute) -
                            (int(reminder.time[:2]) * 60 + int(reminder.time[3:]))) )
              for log in logs]
    missed_rate = prior_missed / len(logs) if logs else 0.0
    features = [
        float(int(reminder.time[:2])),
        float(len(logs)),
        float(missed_rate),
        float(sum(log.status == "Missed" for log in logs[-7:])),
        float(np.mean(delays[-10:])) if delays else 0.0,
        0.0,
    ]
    factors = []
    if missed_rate >= 0.3:
        factors.append(f"{prior_missed} of {len(logs)} recent doses were missed")
    if features[4] >= 30:
        factors.append(f"average response delay is {features[4]:.0f} minutes")
    if features[0] < 7 or features[0] > 21:
        factors.append("schedule is outside typical daytime hours")
    if not factors:
        factors.append("recent history shows low missed-dose and response-delay signals")
    return features, len(logs), {"factors": factors}


def predict_explanation(patient_id: int) -> dict[str, Any]:
    """Return risk, explanations, provenance, and limitations for a patient."""
    global _model, _metadata
    if _model is None or _metadata is None:
        _load_model()
    features, history_count, explanation = _features_for_patient(patient_id)
    if history_count < MINIMUM_HISTORY:
        return {
            "risk_score": None,
            "level": "INSUFFICIENT_DATA",
            "historical_factors": explanation["factors"],
            "model_version": MODEL_VERSION,
            "data_source": "patient history (insufficient for prediction)",
            "validation_limitations": _metadata["validation_limitations"],
            "history_count": history_count,
        }
    probability = float(_model.predict_proba(np.asarray([features]))[0, 1])
    score = round(probability * 100, 1)
    return {
        "risk_score": score,
        "level": risk_level(score),
        "historical_factors": explanation["factors"],
        "model_version": _metadata["model_version"],
        "data_source": _metadata["data_source"],
        "validation_limitations": _metadata["validation_limitations"],
        "history_count": history_count,
    }


def predict_risk(patient_id: int) -> float:
    """Backward-compatible scalar risk accessor."""
    result = predict_explanation(patient_id)
    return float(result["risk_score"] or 0.0)


def model_metadata() -> dict[str, Any]:
    global _model, _metadata
    if _model is None or _metadata is None:
        _load_model()
    return _metadata or {}


def risk_level(risk_score: float) -> str:
    if risk_score >= 60:
        return "HIGH"
    if risk_score >= 30:
        return "MEDIUM"
    return "LOW"

"""Reproducible adherence experiment using documented synthetic dose histories.

There are no consented, clinically labelled medication histories in this
repository. The generator therefore simulates outcomes from hidden adherence
profiles and a persistent latent routine state, rather than assigning labels
from the observable model features. All reported metrics are synthetic
chronological holdout results and are not clinical validation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Sequence
from zoneinfo import ZoneInfo

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

MODEL_VERSION = "synthetic-hidden-state-rf-v2"
DATA_SOURCE = "deterministic synthetic medication histories (seeded hidden-state cohort)"
VALIDATION_LIMITATIONS = (
    "Synthetic data only. The chronological holdout is an engineering experiment, "
    "not clinical validation; predictions must not guide treatment."
)
FEATURE_NAMES = (
    "scheduled_hour",
    "weekday",
    "weekend",
    "prior_dose_count",
    "prior_missed_rate",
    "recent_missed_count_7",
    "days_since_last_missed",
)
MINIMUM_HISTORY = 7
_model: Pipeline | None = None
_metadata: dict[str, Any] | None = None
_loaded_path: Path | None = None
_model_lock = RLock()


def model_path() -> Path:
    return Path(os.environ.get(
        "ADHERENCE_MODEL_PATH",
        str(Path(__file__).resolve().parent / "instance" / "adherence_experiment" / "model.joblib"),
    ))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def feature_vector(
    history: Sequence[tuple[datetime, str]],
    scheduled_at: datetime,
    scheduled_hour: int,
    timezone_name: str = "UTC",
) -> list[float]:
    """Build a feature vector from terminal events strictly before a dose."""
    due_at = _utc(scheduled_at)
    prior = sorted(
        ((_utc(timestamp), status) for timestamp, status in history if _utc(timestamp) < due_at),
        key=lambda item: item[0],
    )
    local_due = due_at.astimezone(ZoneInfo(timezone_name))
    missed = [timestamp for timestamp, status in prior if status == "Missed"]
    days_since_missed = min(
        365.0,
        (due_at - missed[-1]).total_seconds() / 86400 if missed else 365.0,
    )
    return [
        float(scheduled_hour),
        float(local_due.weekday()),
        float(local_due.weekday() >= 5),
        float(len(prior)),
        float(len(missed) / len(prior)) if prior else 0.0,
        float(sum(status == "Missed" for _, status in prior[-7:])),
        float(days_since_missed),
    ]


def generate_synthetic_dataset(
    patients: int = 64, doses_per_patient: int = 120, seed: int = 42
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate daily dose histories with labels from a hidden-state process.

    Each synthetic patient has an unobserved adherence profile and an evolving
    routine state. Dose outcomes are sampled from that latent state; no model
    feature or weighted feature score directly assigns the target label.
    Historical features use only earlier generated outcomes. Rows are returned
    in chronological order for time-based evaluation.
    """
    if patients < 2 or doses_per_patient < 10:
        raise ValueError("synthetic cohort requires at least 2 patients and 10 doses each")
    rng = np.random.default_rng(seed)
    rows: list[list[float]] = []
    targets: list[int] = []
    timestamps: list[datetime] = []
    start = datetime(2025, 1, 6, tzinfo=timezone.utc)
    profile_parameters = (
        {"miss_good": 0.025, "miss_disrupted": 0.20, "enter_disruption": 0.025, "recover": 0.36},
        {"miss_good": 0.075, "miss_disrupted": 0.43, "enter_disruption": 0.055, "recover": 0.20},
        {"miss_good": 0.17, "miss_disrupted": 0.68, "enter_disruption": 0.09, "recover": 0.12},
    )

    for patient in range(patients):
        profile = profile_parameters[int(rng.choice(3, p=(0.45, 0.35, 0.20)))]
        disrupted = bool(rng.random() < 0.2)
        hour = int(rng.choice([7, 8, 9, 12, 18, 20, 22]))
        history: list[tuple[datetime, str]] = []
        for dose_index in range(doses_per_patient):
            due_at = start + timedelta(days=dose_index, hours=patient % 7)
            if disrupted:
                if rng.random() < profile["recover"]:
                    disrupted = False
            elif rng.random() < profile["enter_disruption"]:
                disrupted = True
            rows.append(feature_vector(history, due_at, hour))
            missed_probability = profile["miss_disrupted"] if disrupted else profile["miss_good"]
            missed = int(rng.random() < missed_probability)
            targets.append(missed)
            history.append((due_at + timedelta(minutes=30), "Missed" if missed else "Taken"))
            timestamps.append(due_at)

    order = np.argsort(np.asarray([value.timestamp() for value in timestamps]), kind="stable")
    return np.asarray(rows, dtype=float)[order], np.asarray(targets, dtype=int)[order], np.asarray(timestamps, dtype=object)[order]


def _metrics(y_true: np.ndarray, probabilities: np.ndarray, baseline: bool = False) -> dict[str, Any]:
    predictions = (probabilities >= 0.5).astype(int)
    result: dict[str, Any] = {
        "confusion_matrix": confusion_matrix(y_true, predictions, labels=[0, 1]).tolist(),
        "precision": round(float(precision_score(y_true, predictions, zero_division=0)), 4),
        "recall": round(float(recall_score(y_true, predictions, zero_division=0)), 4),
        "f1": round(float(f1_score(y_true, predictions, zero_division=0)), 4),
        "roc_auc": round(float(roc_auc_score(y_true, probabilities)), 4)
        if len(np.unique(y_true)) > 1 else None,
    }
    if baseline:
        result["type"] = "majority-class baseline"
    return result


def train_model(
    output_path: str | Path | None = None,
    report_path: str | Path | None = None,
    seed: int = 42,
    patients: int = 64,
    doses_per_patient: int = 120,
) -> Pipeline:
    """Train and persist a preprocessing/model pipeline and its holdout report."""
    global _model, _metadata, _loaded_path
    features, targets, timestamps = generate_synthetic_dataset(patients, doses_per_patient, seed)
    unique_dates = sorted({timestamp.date() for timestamp in timestamps})
    split_date = unique_dates[int(len(unique_dates) * 0.8)]
    train_mask = np.asarray([timestamp.date() < split_date for timestamp in timestamps])
    test_mask = ~train_mask
    train_x, test_x = features[train_mask], features[test_mask]
    train_y, test_y = targets[train_mask], targets[test_mask]
    train_times, test_times = timestamps[train_mask], timestamps[test_mask]
    if len(np.unique(train_y)) < 2 or len(np.unique(test_y)) < 2:
        raise ValueError("synthetic chronological split must contain both dose outcomes")

    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("classifier", RandomForestClassifier(
            n_estimators=120,
            max_depth=8,
            min_samples_leaf=5,
            random_state=seed,
            class_weight="balanced",
            n_jobs=-1,
        )),
    ])
    pipeline.fit(train_x, train_y)
    probabilities = pipeline.predict_proba(test_x)[:, 1]
    baseline_probability = float(np.mean(train_y))
    metadata: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "model_type": "RandomForestClassifier with median imputation",
        "data_source": DATA_SOURCE,
        "validation_limitations": VALIDATION_LIMITATIONS,
        "feature_names": list(FEATURE_NAMES),
        "target": "whether the upcoming scheduled dose is recorded as missed",
        "minimum_history": MINIMUM_HISTORY,
        "training_rows": int(len(train_x)),
        "test_rows": int(len(test_x)),
        "train_start": min(train_times).isoformat(),
        "train_end": max(train_times).isoformat(),
        "train_last_dose_at": max(train_times).isoformat(),
        "test_start": min(test_times).isoformat(),
        "test_end": max(test_times).isoformat(),
        "split_method": "chronological 80/20 by unique scheduled-dose date",
        "random_seed": seed,
        "cohort": {"patients": patients, "doses_per_patient": doses_per_patient},
        "evaluation": _metrics(test_y, probabilities),
        "baseline": _metrics(test_y, np.full(len(test_y), baseline_probability), baseline=True),
        "feature_importance": {
            name: round(float(value), 4)
            for name, value in zip(
                FEATURE_NAMES,
                pipeline.named_steps["classifier"].feature_importances_,
            )
        },
    }
    artifact = {"pipeline": pipeline, "metadata": metadata}
    artifact_path = Path(output_path) if output_path else model_path()
    report = Path(report_path) if report_path else artifact_path.with_suffix(".metrics.json")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, artifact_path)
    report.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    _model, _metadata, _loaded_path = pipeline, metadata, artifact_path
    return pipeline


def _load_model() -> None:
    global _model, _metadata, _loaded_path
    path = model_path()
    if not path.exists():
        train_model()
        return
    artifact = joblib.load(path)
    metadata = artifact.get("metadata") if isinstance(artifact, dict) else None
    pipeline = artifact.get("pipeline") if isinstance(artifact, dict) else None
    if not isinstance(metadata, dict) or metadata.get("model_version") != MODEL_VERSION or not isinstance(pipeline, Pipeline):
        raise RuntimeError("adherence model artifact is incompatible; run scripts/train_adherence_model.py")
    _model = pipeline
    _metadata = metadata
    _loaded_path = path


def _ensure_model() -> None:
    with _model_lock:
        if _model is None or _metadata is None or _loaded_path != model_path():
            _load_model()


def _features_for_patient(patient_id: int) -> tuple[list[float] | None, int, dict[str, Any]]:
    from app import PillLog, User, db, next_scheduled_dose

    patient = db.session.get(User, patient_id)
    if patient is None:
        return None, 0, {"factors": ["Patient record is unavailable."], "upcoming_dose": None}
    upcoming = next_scheduled_dose(patient, datetime.now(timezone.utc))
    if upcoming is None:
        return None, 0, {"factors": ["No upcoming active scheduled dose is available."], "upcoming_dose": None}
    reminder = upcoming["reminder"]
    due_at = _utc(datetime.fromisoformat(upcoming["scheduled_at"]))
    logs = db.session.scalars(
        db.select(PillLog)
        .where(PillLog.patient_id == patient_id, PillLog.timestamp < due_at)
        .order_by(PillLog.timestamp.asc())
    ).all()
    history = [(log.timestamp, log.status) for log in logs]
    features = feature_vector(
        history,
        due_at,
        int(reminder["time"].split(":")[0]),
        patient.timezone,
    )
    missed_count = sum(log.status == "Missed" for log in logs)
    factors: list[str] = []
    if logs and missed_count:
        factors.append(f"{missed_count} of {len(logs)} prior recorded doses were missed")
    if features[5]:
        factors.append(f"{int(features[5])} missed dose(s) among the previous seven recorded events")
    if not factors:
        factors.append("No missed doses appear in the available pre-dose history")
    return features, len(logs), {
        "factors": factors,
        "upcoming_dose": {
            "scheduled_at": due_at.isoformat(),
            "medication": reminder["med_name"],
            "time": reminder["time"],
        },
    }


def predict_explanation(patient_id: int) -> dict[str, Any]:
    """Predict an upcoming scheduled dose using only its pre-dose history."""
    _ensure_model()
    features, history_count, explanation = _features_for_patient(patient_id)
    base = {
        "risk_score": None,
        "level": "INSUFFICIENT_DATA",
        "historical_factors": explanation["factors"],
        "model_version": MODEL_VERSION,
        "data_source": DATA_SOURCE,
        "validation_limitations": VALIDATION_LIMITATIONS,
        "history_count": history_count,
        "minimum_history": MINIMUM_HISTORY,
        "upcoming_dose": explanation["upcoming_dose"],
    }
    if features is None:
        base["level"] = "NO_UPCOMING_DOSE"
        return base
    if history_count < MINIMUM_HISTORY:
        return base
    probability = float(_model.predict_proba(np.asarray([features], dtype=float))[0, 1])
    score = round(probability * 100, 1)
    return {
        **base,
        "risk_score": score,
        "miss_probability": round(probability, 4),
        "level": risk_level(score),
        "model_version": _metadata["model_version"],
        "data_source": _metadata["data_source"],
        "validation_limitations": _metadata["validation_limitations"],
    }


def predict_risk(patient_id: int) -> float:
    """Backward-compatible scalar risk accessor."""
    result = predict_explanation(patient_id)
    return float(result["risk_score"] or 0.0)


def model_metadata() -> dict[str, Any]:
    _ensure_model()
    return _metadata or {}


def risk_level(risk_score: float) -> str:
    if risk_score >= 60:
        return "HIGH"
    if risk_score >= 30:
        return "MEDIUM"
    return "LOW"

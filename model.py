"""Random Forest adherence-risk engine.

The model is intentionally trained from a small deterministic bootstrap set so
the application has a working predictor before enough real patient history
exists. Real PillLogs are used for every prediction.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sklearn.ensemble import RandomForestClassifier


FEATURE_NAMES = ("hour_of_day", "past_missed_doses", "response_delay_minutes")
_model: RandomForestClassifier | None = None


def train_model() -> RandomForestClassifier:
    """Train and retain the startup model using adherence-like examples."""
    global _model
    rows: list[list[float]] = []
    targets: list[int] = []
    for hour in range(24):
        for missed in range(0, 9):
            for delay in (0, 5, 15, 30, 60, 120):
                risk_signal = missed * 0.11 + delay / 180 + (0.08 if hour < 6 else 0)
                taken = int(risk_signal < 0.72)
                rows.append([hour, missed, delay])
                targets.append(taken)
    _model = RandomForestClassifier(
        n_estimators=120,
        max_depth=8,
        random_state=42,
        class_weight="balanced",
    )
    _model.fit(np.asarray(rows), np.asarray(targets))
    return _model


def _minutes_from_reminder(timestamp: datetime, reminder_time: str) -> float | None:
    try:
        hour, minute = (int(part) for part in reminder_time.split(":"))
        scheduled = timestamp.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return abs((timestamp - scheduled).total_seconds()) / 60
    except (TypeError, ValueError):
        return None


def _features_for_patient(patient_id: int) -> list[float]:
    # Imported lazily to avoid a module cycle while Flask initializes.
    from app import PillLog, Reminder, db, utc_now

    since = utc_now() - timedelta(days=30)
    logs = db.session.scalars(
        db.select(PillLog)
        .where(PillLog.patient_id == patient_id, PillLog.timestamp >= since)
        .order_by(PillLog.timestamp.desc())
        .limit(50)
    ).all()
    reminders = db.session.scalars(
        db.select(Reminder).where(
            Reminder.patient_id == patient_id,
            Reminder.status == "Active",
        )
    ).all()
    latest = logs[0].timestamp if logs else utc_now()
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=utc_now().tzinfo)
    delays = [
        delay
        for log in logs
        for reminder in reminders
        for delay in [_minutes_from_reminder(log.timestamp, reminder.time)]
        if delay is not None
    ]
    response_delay = min(sum(delays) / len(delays), 240.0) if delays else 0.0
    missed = sum(1 for log in logs if log.status == "Missed")
    return [float(latest.hour), float(missed), response_delay]


def predict_risk(patient_id: int) -> float:
    """Return the current non-adherence probability as a percentage."""
    global _model
    if _model is None:
        train_model()
    features = np.asarray([_features_for_patient(patient_id)])
    probabilities = _model.predict_proba(features)
    missed_class_index = list(_model.classes_).index(0)
    return round(float(probabilities[0][missed_class_index]) * 100, 1)


def risk_level(risk_score: float) -> str:
    if risk_score >= 60:
        return "HIGH"
    if risk_score >= 30:
        return "MEDIUM"
    return "LOW"

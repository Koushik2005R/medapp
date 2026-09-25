"""Database-grounded PillGuard assistant.

This module deliberately answers a small, auditable set of intents. It does
not generate medication advice, dosage changes, or uncited patient facts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


SUPPORTED_HINTS = (
    "schedule", "next dose", "upcoming", "missed", "adherence", "risk",
    "prediction", "event", "feature", "what can", "help",
)


def _source(kind: str, label: str, date: datetime | None = None, record_id: int | None = None) -> dict[str, Any]:
    return {
        "type": kind,
        "label": label,
        "date": date.isoformat() if date else None,
        "record_id": record_id,
    }


def answer_question(question: str, actor: Any, patient: Any, db: Any, reminder_cls: Any,
                    pill_log_cls: Any, risk_fn: Any) -> dict[str, Any]:
    """Answer an authorized question for one patient context."""
    text = question.strip().lower() if isinstance(question, str) else ""
    if not text:
        return {"answer": "Ask about schedules, recorded events, missed doses, adherence, predictions, or features.", "sources": [], "intent": "empty"}
    if not any(hint in text for hint in SUPPORTED_HINTS):
        return {
            "answer": "I can only answer database-grounded questions about medication schedules, recorded events, missed-dose history, adherence, ML predictions, and available features.",
            "sources": [],
            "intent": "unsupported",
        }

    reminders = db.session.scalars(
        db.select(reminder_cls).where(
            reminder_cls.patient_id == patient.id, reminder_cls.status == "Active"
        ).order_by(reminder_cls.time, reminder_cls.id)
    ).all()
    logs = db.session.scalars(
        db.select(pill_log_cls).where(pill_log_cls.patient_id == patient.id)
        .order_by(pill_log_cls.timestamp.desc()).limit(100)
    ).all()
    sources: list[dict[str, Any]] = []

    if any(word in text for word in ("feature", "help", "what can")):
        return {
            "answer": "I can summarize upcoming schedules, recorded medication events, missed-dose history, adherence statistics, and the advisory ML prediction with its historical factors. I cannot prescribe, change doses, or confirm swallowing.",
            "sources": [{"type": "application", "label": "PillGuard supported assistant capabilities", "date": None, "record_id": None}],
            "intent": "features",
        }
    if any(word in text for word in ("schedule", "upcoming", "next dose")):
        if not reminders:
            return {"answer": "No active medication schedule is recorded for this patient.", "sources": [], "intent": "schedule"}
        details = "; ".join(f"{r.time} — {r.med_name} ({r.dosage}), compartment {r.compartment or 'not assigned'}" for r in reminders)
        sources = [_source("reminder", f"{r.med_name} at {r.time}", record_id=r.id) for r in reminders]
        return {"answer": f"Active schedules: {details}.", "sources": sources, "intent": "schedule"}
    if "miss" in text:
        missed = [log for log in logs if log.status == "Missed"]
        if not missed:
            return {"answer": "No missed dose records are available in the recorded history.", "sources": [], "intent": "missed_history"}
        sources = [_source("pill_log", f"Missed dose ({log.timestamp.date().isoformat()})", log.timestamp, log.id) for log in missed[:10]]
        return {"answer": f"I found {len(missed)} missed dose record(s) in the available history. The most recent was {missed[0].timestamp.date().isoformat()}.", "sources": sources, "intent": "missed_history"}
    if "adherence" in text:
        if not logs:
            return {"answer": "No recorded medication events are available to calculate adherence.", "sources": [], "intent": "adherence"}
        completed = sum(log.status != "Missed" for log in logs)
        percent = round(completed / len(logs) * 100, 1)
        sources = [_source("pill_log", f"{log.status} event", log.timestamp, log.id) for log in logs[:10]]
        return {"answer": f"Recorded-event adherence is {percent}% ({completed} of {len(logs)} events not marked missed). This is an observed event statistic, not proof of swallowing.", "sources": sources, "intent": "adherence"}
    if any(word in text for word in ("risk", "prediction")):
        prediction = risk_fn(patient.id)
        factors = "; ".join(prediction.get("historical_factors", []))
        return {
            "answer": (
                f"Advisory missed-dose risk: {prediction['risk_score']}% ({prediction['level']}). "
                f"Historical factors: {factors or 'none available'}. "
                "This is not clinically validated and does not override medication safety rules."
            ) if prediction["risk_score"] is not None else
            "There is insufficient recorded history for an adherence prediction.",
            "sources": [_source("model", f"Model {prediction.get('model_version', 'unavailable')}")],
            "intent": "prediction",
        }
    if "event" in text:
        if not logs:
            return {"answer": "No recorded medication events are available for this patient.", "sources": [], "intent": "events"}
        sources = [_source("pill_log", f"{log.status} event", log.timestamp, log.id) for log in logs[:10]]
        return {"answer": f"The latest recorded event was {logs[0].status} on {logs[0].timestamp.date().isoformat()}. I can only report stored events; weight change does not prove consumption.", "sources": sources, "intent": "events"}
    return {"answer": "I could not map that to a supported database-grounded question. Try asking about the next dose, missed doses, adherence, risk, events, or features.", "sources": [], "intent": "unsupported"}

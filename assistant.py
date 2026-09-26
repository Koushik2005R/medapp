"""Database-grounded PillGuard assistant.

This module answers a small, auditable set of intents. It does not generate
medication advice, dosage changes, or uncited patient facts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


SUPPORTED_HINTS = (
    "schedule", "dose", "upcoming", "missed", "adherence", "risk",
    "prediction", "event", "feature", "help", "navigate", "where",
    "request", "status", "change",
)


def _source(
    kind: str,
    label: str,
    date: datetime | None = None,
    record_id: int | None = None,
) -> dict[str, Any]:
    return {
        "type": kind,
        "label": label,
        "date": date.isoformat() if date else None,
        "record_id": record_id,
    }


def _answer_navigation(actor: Any) -> dict[str, Any]:
    sections = ["Dashboard", "Medications", "Activity", "AI Insights", "Messages", "Settings"]
    if actor.role == "Doctor":
        sections.extend(["Patients", "Requests"])
    elif actor.role == "Caregiver":
        sections.append("Assigned Patients")
    return {
        "answer": f"Use the dashboard navigation to open: {', '.join(sections)}.",
        "sources": [_source("application", "PillGuard dashboard navigation")],
        "intent": "navigation",
    }


def answer_question(
    question: str,
    actor: Any,
    patient: Any,
    db: Any,
    reminder_cls: Any,
    pill_log_cls: Any,
    request_cls: Any,
    risk_fn: Any,
) -> dict[str, Any]:
    """Answer an authorized question for one patient context."""
    text = question.strip().lower() if isinstance(question, str) else ""
    if not text:
        return {
            "answer": "Ask about schedules, recorded events, missed doses, adherence, predictions, change requests, or navigation.",
            "sources": [],
            "intent": "empty",
        }
    if not any(hint in text for hint in SUPPORTED_HINTS):
        return {
            "answer": "I can answer database-grounded questions about schedules, recorded events, missed-dose history, adherence, ML predictions, medication-change request status, application navigation, and available features.",
            "sources": [],
            "intent": "unsupported",
        }

    asks_for_personal_medical_direction = (
        any(phrase in text for phrase in ("should i", "can i take", "may i take", "is it safe", "recommend"))
        and any(word in text for word in ("dose", "medication", "medicine", "pill", "take"))
    ) or any(phrase in text for phrase in (
        "change my dose", "increase my dose", "decrease my dose", "stop taking",
        "stop my medication", "skip a dose", "skip my medication", "double dose", "take extra",
        "too high", "too low", "side effect", "interaction", "interact with",
        "symptom", "allergic reaction",
    ))
    if asks_for_personal_medical_direction:
        return {
            "answer": "I can report your recorded schedule, but I cannot advise whether to start, stop, skip, or change a dose. Please contact your prescribing clinician or pharmacist for personal medication guidance.",
            "sources": [],
            "intent": "medical_boundary",
        }

    asks_where_to_navigate = "where" in text and any(
        word in text
        for word in ("find", "access", "open", "section", "page", "dashboard", "settings", "requests", "messages", "activity")
    )
    if any(word in text for word in ("navigate", "find", "open", "go to", "section", "page")) or asks_where_to_navigate:
        return _answer_navigation(actor)
    if any(word in text for word in ("feature", "help", "what can")):
        return {
            "answer": "I can summarize upcoming schedules, recorded medication events, missed-dose history, adherence statistics, medication-change request status, application navigation, and the advisory ML prediction with historical factors. I cannot prescribe, change doses, or confirm swallowing.",
            "sources": [_source("application", "PillGuard supported assistant capabilities")],
            "intent": "features",
        }
    if patient is None:
        return {
            "answer": "Select an assigned patient to ask about patient-specific care records.",
            "sources": [],
            "intent": "patient_context_required",
        }

    reminders = db.session.scalars(
        db.select(reminder_cls).where(
            reminder_cls.patient_id == patient.id,
            reminder_cls.status == "Active",
        ).order_by(reminder_cls.time, reminder_cls.id)
    ).all()
    log_counts = dict(db.session.execute(
        db.select(pill_log_cls.status, db.func.count(pill_log_cls.id))
        .where(pill_log_cls.patient_id == patient.id)
        .group_by(pill_log_cls.status)
    ).all())
    logs = db.session.scalars(
        db.select(pill_log_cls)
        .where(pill_log_cls.patient_id == patient.id)
        .order_by(pill_log_cls.timestamp.desc())
        .limit(10)
    ).all()

    asks_about_change_request = "request" in text or (
        "change" in text and any(word in text for word in ("status", "pending", "approved", "rejected"))
    )
    if asks_about_change_request:
        requests = db.session.scalars(
            db.select(request_cls)
            .where(request_cls.patient_id == patient.id)
            .order_by(request_cls.created_at.desc(), request_cls.id.desc())
            .limit(10)
        ).all()
        if "pending" in text:
            requests = [item for item in requests if item.status == "PENDING"]
        if not requests:
            detail = "No pending medication-change requests are recorded." if "pending" in text else "No medication-change requests are recorded for this patient."
            return {"answer": detail, "sources": [], "intent": "change_requests"}
        details = [
            f"#{item.id} {item.operation.lower()} {item.medication.name if item.medication else item.requested_data.get('name', 'medication')} — {item.status.lower()}"
            + (f" (doctor's note: {item.decision_reason})" if item.decision_reason else "")
            for item in requests
        ]
        sources = [
            _source("medication_change_request", f"Request #{item.id} · {item.status}", item.created_at, item.id)
            for item in requests
        ]
        return {
            "answer": "Medication-change request status: " + "; ".join(details) + ".",
            "sources": sources,
            "intent": "change_requests",
        }

    if "miss" not in text and any(word in text for word in ("schedule", "upcoming", "next dose", "dose")):
        if not reminders:
            return {
                "answer": "No active medication schedule is recorded for this patient.",
                "sources": [],
                "intent": "schedule",
            }
        details = "; ".join(
            f"{reminder.time} — {reminder.medication.name if reminder.medication else reminder.med_name} ({reminder.dosage}), compartment {reminder.compartment or 'not assigned'}"
            for reminder in reminders
        )
        sources = [
            _source(
                "reminder",
                f"{reminder.medication.name if reminder.medication else reminder.med_name} at {reminder.time}",
                record_id=reminder.id,
            )
            for reminder in reminders
        ]
        return {"answer": f"Active schedules: {details}.", "sources": sources, "intent": "schedule"}

    if "miss" in text:
        missed_count = log_counts.get("Missed", 0)
        if not missed_count:
            return {
                "answer": "No missed dose records are available in the recorded history.",
                "sources": [],
                "intent": "missed_history",
            }
        missed = db.session.scalars(
            db.select(pill_log_cls)
            .where(pill_log_cls.patient_id == patient.id, pill_log_cls.status == "Missed")
            .order_by(pill_log_cls.timestamp.desc())
            .limit(10)
        ).all()
        sources = [
            _source("pill_log", f"Missed dose ({log.timestamp.date().isoformat()})", log.timestamp, log.id)
            for log in missed[:10]
        ]
        return {
            "answer": f"I found {missed_count} missed dose record(s) in the recorded history. The most recent was {missed[0].timestamp.date().isoformat()}.",
            "sources": sources,
            "intent": "missed_history",
        }

    if "adherence" in text:
        total = sum(log_counts.values())
        if not total:
            return {
                "answer": "No recorded medication events are available to calculate adherence.",
                "sources": [],
                "intent": "adherence",
            }
        completed = total - log_counts.get("Missed", 0)
        percent = round(completed / total * 100, 1)
        sources = [_source("pill_log", f"{log.status} event", log.timestamp, log.id) for log in logs[:10]]
        return {
            "answer": f"Recorded-event adherence is {percent}% ({completed} of {total} events not marked missed). This is an observed event statistic, not proof of swallowing.",
            "sources": sources,
            "intent": "adherence",
        }

    if any(word in text for word in ("risk", "prediction")):
        prediction = risk_fn(patient.id)
        factors = "; ".join(prediction.get("historical_factors", []))
        upcoming = prediction.get("upcoming_dose")
        if prediction["risk_score"] is None:
            answer = (
                "There is no upcoming active scheduled dose to predict."
                if prediction.get("level") == "NO_UPCOMING_DOSE"
                else "There is insufficient recorded history for an adherence prediction."
            )
        else:
            dose_context = (
                f"For {upcoming['medication']} scheduled at {upcoming['time']}, "
                if upcoming else ""
            )
            answer = (
                f"{dose_context}advisory missed-dose risk: {prediction['risk_score']}% ({prediction['level']}). "
                f"Historical factors: {factors or 'none available'}. "
                "This is not clinically validated and does not override medication safety rules."
            )
        return {
            "answer": answer,
            "sources": [_source("model", f"Model {prediction.get('model_version', 'unavailable')}")],
            "intent": "prediction",
        }

    if "event" in text:
        if not logs:
            return {
                "answer": "No recorded medication events are available for this patient.",
                "sources": [],
                "intent": "events",
            }
        sources = [_source("pill_log", f"{log.status} event", log.timestamp, log.id) for log in logs[:10]]
        return {
            "answer": f"The latest recorded event was {logs[0].status} on {logs[0].timestamp.date().isoformat()}. I can only report stored events; weight change does not prove consumption.",
            "sources": sources,
            "intent": "events",
        }

    return {
        "answer": "I could not map that to a supported database-grounded question. Try asking about the next dose, missed doses, adherence, risk, events, request status, navigation, or features.",
        "sources": [],
        "intent": "unsupported",
    }

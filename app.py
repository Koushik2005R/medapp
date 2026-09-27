"""Smart pill box monitoring backend.

Run locally with:
    flask --app app run

The SQLite database is created automatically at the path configured by
DATABASE_URL (or ``instance/database.db`` in the project directory).
"""

from __future__ import annotations

import math
import json
import os
import hashlib
import secrets
import re
import queue
import smtplib
import threading
import urllib.error
import urllib.request
from statistics import median
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, Response, jsonify, redirect, render_template, request, session, stream_with_context, url_for
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.orm import Session as OrmSession
from werkzeug.security import check_password_hash, generate_password_hash
from model import model_metadata, predict_explanation
from sensor_analysis import (
    analyze_readings,
    demo_sensor_analyses,
    evaluate_sensor_model,
    generate_sensor_noise,
)
from assistant import answer_question


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)
migrate = Migrate(compare_type=True)
F = TypeVar("F", bound=Callable[..., Any])
_sse_lock = threading.Lock()
_sse_clients: dict[int, list[queue.Queue[str]]] = {}


def publish_event(user_id: int, event_type: str, payload: dict[str, Any]) -> None:
    message = json.dumps({"type": event_type, "payload": payload})
    with _sse_lock:
        clients = tuple(_sse_clients.get(user_id, ()))
    for client_queue in clients:
        client_queue.put(message)


def _queue_patient_event(patient: User, event_type: str, payload: dict[str, Any]) -> None:
    recipient_ids = {
        patient.id,
        patient.linked_doctor_id,
        patient.linked_caregiver_id,
    } - {None}
    pending_events = db.session().info.setdefault("sse_events", [])
    pending_events.extend((user_id, event_type, payload) for user_id in recipient_ids)


@sqlalchemy_event.listens_for(OrmSession, "after_commit")
def _publish_committed_events(session: OrmSession) -> None:
    for user_id, event_type, payload in session.info.pop("sse_events", []):
        publish_event(user_id, event_type, payload)


@sqlalchemy_event.listens_for(OrmSession, "after_rollback")
def _discard_rolled_back_events(session: OrmSession) -> None:
    session.info.pop("sse_events", None)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class User(db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(db.String(120), nullable=False)
    email: Mapped[str] = mapped_column(db.String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(db.String(255), nullable=False)
    role: Mapped[str] = mapped_column(db.String(20), nullable=False)
    timezone: Mapped[str] = mapped_column(db.String(64), nullable=False, default="UTC")
    linked_doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    linked_caregiver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    reminders: Mapped[list["ScheduledDose"]] = relationship(
        back_populates="patient", foreign_keys="ScheduledDose.patient_id"
    )
    doctor: Mapped["User | None"] = relationship(
        remote_side="User.id", foreign_keys=[linked_doctor_id]
    )
    caregiver: Mapped["User | None"] = relationship(
        remote_side="User.id", foreign_keys=[linked_caregiver_id]
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "timezone": self.timezone,
            "linked_doctor_id": self.linked_doctor_id,
            "linked_caregiver_id": self.linked_caregiver_id,
        }


class Medication(db.Model):
    __tablename__ = "medications"
    __table_args__ = (
        CheckConstraint("status IN ('Active', 'Discontinued', 'Archived')", name="ck_medication_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(db.String(150), nullable=False)
    status: Mapped[str] = mapped_column(db.String(20), nullable=False, default="Active")
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    schedules: Mapped[list["ScheduledDose"]] = relationship(back_populates="medication")

    def to_dict(self) -> dict[str, Any]:
        schedule = next((item for item in self.schedules if item.status == "Active"), None)
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "schedule": schedule.to_dict() if schedule else None,
        }


class ScheduledDose(db.Model):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint("status IN ('Active', 'Completed')", name="ck_reminder_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    medication_id: Mapped[int | None] = mapped_column(ForeignKey("medications.id"), index=True)
    # Kept for compatibility with existing SQLite rows and API clients.
    med_name: Mapped[str] = mapped_column(db.String(150), nullable=False)
    time: Mapped[str] = mapped_column(db.String(5), nullable=False)
    dosage: Mapped[str] = mapped_column(db.String(100), nullable=False)
    set_by_role: Mapped[str] = mapped_column(db.String(20), nullable=False)
    status: Mapped[str] = mapped_column(db.String(20), nullable=False, default="Active")
    compartment: Mapped[int | None] = mapped_column(db.Integer)
    tablet_weight: Mapped[float] = mapped_column(db.Float, nullable=False, default=0.5)
    expected_quantity: Mapped[int] = mapped_column(db.Integer, nullable=False, default=1)
    tolerance: Mapped[float] = mapped_column(db.Float, nullable=False, default=0.2)
    calibration_offset: Mapped[float] = mapped_column(db.Float, nullable=False, default=0.0)
    response_window_minutes: Mapped[int] = mapped_column(db.Integer, nullable=False, default=30)
    noise_threshold: Mapped[float] = mapped_column(db.Float, nullable=False, default=0.15)

    patient: Mapped[User] = relationship(back_populates="reminders", foreign_keys=[patient_id])
    medication: Mapped[Medication | None] = relationship(back_populates="schedules")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "med_name": self.medication.name if self.medication else self.med_name,
            "time": self.time,
            "dosage": self.dosage,
            "set_by_role": self.set_by_role,
            "status": self.status,
            "compartment": self.compartment,
            "tablet_weight": self.tablet_weight,
            "expected_quantity": self.expected_quantity,
            "tolerance": self.tolerance,
            "calibration_offset": self.calibration_offset,
            "response_window_minutes": self.response_window_minutes,
            "noise_threshold": self.noise_threshold,
            "timezone": self.patient.timezone,
        }


Reminder = ScheduledDose


class MedicationChangeRequest(db.Model):
    __tablename__ = "medication_change_requests"
    __table_args__ = (
        CheckConstraint("operation IN ('ADD', 'EDIT', 'REMOVE')", name="ck_med_request_operation"),
        CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED')", name="ck_med_request_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    medication_id: Mapped[int | None] = mapped_column(ForeignKey("medications.id"), index=True)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    operation: Mapped[str] = mapped_column(db.String(10), nullable=False)
    requested_data: Mapped[dict[str, Any]] = mapped_column(db.JSON, nullable=False)
    pending_key: Mapped[str | None] = mapped_column(db.String(220), unique=True)
    status: Mapped[str] = mapped_column(db.String(10), nullable=False, default="PENDING")
    decision_reason: Mapped[str | None] = mapped_column(db.String(1000))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))

    patient: Mapped[User] = relationship(foreign_keys=[patient_id])
    medication: Mapped[Medication | None] = relationship()
    requested_by: Mapped[User] = relationship(foreign_keys=[requested_by_id])
    decided_by: Mapped[User | None] = relationship(foreign_keys=[decided_by_id])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "medication_id": self.medication_id,
            "medication_name": self.medication.name if self.medication else self.requested_data.get("name"),
            "requested_by_id": self.requested_by_id,
            "requested_by_name": self.requested_by.name,
            "operation": self.operation,
            "requested_data": self.requested_data,
            "status": self.status,
            "decision_reason": self.decision_reason,
            "decided_by_id": self.decided_by_id,
            "decided_by_name": self.decided_by.name if self.decided_by else None,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


class MedicationAudit(db.Model):
    __tablename__ = "medication_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    medication_id: Mapped[int | None] = mapped_column(ForeignKey("medications.id"), index=True)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("medication_change_requests.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(db.String(30), nullable=False)
    before_data: Mapped[dict[str, Any] | None] = mapped_column(db.JSON)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(db.JSON)
    reason: Mapped[str | None] = mapped_column(db.String(1000))
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "medication_id": self.medication_id,
            "request_id": self.request_id,
            "actor_id": self.actor_id,
            "action": self.action,
            "before_data": self.before_data,
            "after_data": self.after_data,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
        }


@sqlalchemy_event.listens_for(MedicationAudit, "before_update")
def prevent_medication_audit_update(mapper, connection, target):
    raise ValueError("Medication audit records are immutable")


@sqlalchemy_event.listens_for(MedicationAudit, "before_delete")
def prevent_medication_audit_delete(mapper, connection, target):
    raise ValueError("Medication audit records cannot be deleted")


class PillLog(db.Model):
    __tablename__ = "pill_logs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('Taken', 'Missed', 'Manual Override')",
            name="ck_pill_log_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    initial_weight: Mapped[float] = mapped_column(db.Float, nullable=False)
    final_weight: Mapped[float] = mapped_column(db.Float, nullable=False)
    delta_weight: Mapped[float] = mapped_column(db.Float, nullable=False)
    status: Mapped[str] = mapped_column(db.String(20), nullable=False)
    remarks: Mapped[str | None] = mapped_column(db.String(500))
    reminder_id: Mapped[int | None] = mapped_column(ForeignKey("reminders.id"))
    dose_event_id: Mapped[int | None] = mapped_column(ForeignKey("dose_events.id"))
    compartment: Mapped[int | None] = mapped_column(db.Integer)
    source: Mapped[str | None] = mapped_column(db.String(30))
    verification_method: Mapped[str | None] = mapped_column(db.String(50))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "timestamp": self.timestamp.isoformat(),
            "initial_weight": self.initial_weight,
            "final_weight": self.final_weight,
            "delta_weight": self.delta_weight,
            "status": self.status,
            "remarks": self.remarks,
            "reminder_id": self.reminder_id,
            "dose_event_id": self.dose_event_id,
            "compartment": self.compartment,
            "source": self.source,
            "verification_method": self.verification_method,
        }


DOSE_STATES = (
    "IDLE",
    "REMINDER_DUE",
    "ALERTING",
    "REMOVAL_DETECTED",
    "AWAITING_CONFIRMATION",
    "MISSED",
    "MANUALLY_CONFIRMED",
)


class DoseEvent(db.Model):
    __tablename__ = "dose_events"
    __table_args__ = (
        CheckConstraint(
            "state IN ('IDLE', 'REMINDER_DUE', 'ALERTING', 'REMOVAL_DETECTED', "
            "'AWAITING_CONFIRMATION', 'MISSED', 'MANUALLY_CONFIRMED')",
            name="ck_dose_event_state",
        ),
        UniqueConstraint("event_key", name="uq_dose_event_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(db.String(180), nullable=False)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"), nullable=False, index=True)
    compartment: Mapped[int | None] = mapped_column(db.Integer)
    scheduled_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(db.String(30), nullable=False, default="IDLE")
    source: Mapped[str] = mapped_column(db.String(30), nullable=False, default="simulation")
    verification_method: Mapped[str | None] = mapped_column(db.String(50))
    initial_weight: Mapped[float | None] = mapped_column(db.Float)
    final_weight: Mapped[float | None] = mapped_column(db.Float)
    filtered_delta: Mapped[float | None] = mapped_column(db.Float)
    detected_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(db.DateTime(timezone=True))
    notification_sent: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_key": self.event_key,
            "patient_id": self.patient_id,
            "reminder_id": self.reminder_id,
            "compartment": self.compartment,
            "scheduled_at": self.scheduled_at.isoformat(),
            "due_at": self.due_at.isoformat(),
            "state": self.state,
            "source": self.source,
            "verification_method": self.verification_method,
            "initial_weight": self.initial_weight,
            "final_weight": self.final_weight,
            "filtered_delta": self.filtered_delta,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "notification_sent": self.notification_sent,
        }


class SensorEvent(db.Model):
    __tablename__ = "sensor_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    scheduled_dose_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"), nullable=False, index=True)
    dose_event_id: Mapped[int] = mapped_column(ForeignKey("dose_events.id"), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(db.String(30), nullable=False)
    readings_before: Mapped[list[float]] = mapped_column(db.JSON, nullable=False)
    readings_after: Mapped[list[float]] = mapped_column(db.JSON, nullable=False)
    filtered_delta: Mapped[float] = mapped_column(db.Float, nullable=False)
    analysis: Mapped[dict[str, Any]] = mapped_column(db.JSON, nullable=False)


class Notification(db.Model):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    message: Mapped[str] = mapped_column(db.String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
        }


class Message(db.Model):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    body: Mapped[str] = mapped_column(db.String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict[str, Any]:
        sender = db.session.get(User, self.sender_id)
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "sender_id": self.sender_id,
            "sender_name": sender.name if sender else "Unknown",
            "body": self.body,
            "created_at": self.created_at.isoformat(),
        }


class HardwareTraffic(db.Model):
    __tablename__ = "hardware_traffic"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    endpoint: Mapped[str] = mapped_column(db.String(120), nullable=False)
    method: Mapped[str] = mapped_column(db.String(10), nullable=False)
    payload: Mapped[str] = mapped_column(db.Text, nullable=False)
    response_status: Mapped[int] = mapped_column(db.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "endpoint": self.endpoint,
            "method": self.method,
            "payload": self.payload,
            "response_status": self.response_status,
            "created_at": self.created_at.isoformat(),
        }


class Device(db.Model):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(db.String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(db.String(64), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        raise RuntimeError("SECRET_KEY environment variable is required")
    app.config.from_mapping(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true",
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL",
            f"sqlite:///{os.path.join(app.root_path, 'instance', 'database.db')}",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    migrate.init_app(app, db, compare_type=True)
    with app.app_context():
        if app.config.get("TESTING"):
            db.create_all()

    register_routes(app)
    register_csrf(app)
    return app


def register_csrf(app: Flask) -> None:
    @app.get("/api/csrf-token")
    def csrf_token():
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return jsonify({"csrf_token": token})

    @app.before_request
    def validate_csrf():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if not request.path.startswith("/api/") or request.path in {"/api/login", "/api/register"}:
            return None
        if request.path in {
            "/api/hardware/get-schedules",
            "/api/hardware/log-event",
        } and request.headers.get("X-Device-Key"):
            return None
        expected = session.get("csrf_token")
        if not expected or not secrets.compare_digest(
            expected, request.headers.get("X-CSRF-Token", "")
        ):
            return json_error("CSRF token missing or invalid", 400)
        return None


def json_error(message: str, status_code: int):
    return jsonify({"error": message}), status_code


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def device_patient() -> User | None:
    raw_token = request.headers.get("X-Device-Key", "").strip()
    if not raw_token:
        return None
    device = db.session.scalar(
        db.select(Device).where(Device.token_hash == token_digest(raw_token), Device.active.is_(True))
    )
    return db.session.get(User, device.patient_id) if device else None


def valid_weight(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and 0 <= float(value) <= 100000


def valid_samples(value: Any) -> bool:
    return value is None or (
        isinstance(value, list)
        and 1 <= len(value) <= 1000
        and all(valid_weight(reading) for reading in value)
    )


def login_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if "user_id" not in session:
            return json_error("Authentication required", 401)
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def current_user() -> User | None:
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if isinstance(user_id, int) else None


def role_required(*roles: str):
    def decorator(view: F) -> F:
        @wraps(view)
        @login_required
        def wrapped(*args: Any, **kwargs: Any):
            user = current_user()
            if user is None:
                session.clear()
                return json_error("Authenticated user no longer exists", 401)
            if user.role not in roles:
                return json_error("This action is not available for your role", 403)
            return view(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    return decorator


def user_summary(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "timezone": user.timezone,
    }


def get_patient(patient_id: Any) -> User | None:
    return db.session.get(User, patient_id) if isinstance(patient_id, int) and not isinstance(patient_id, bool) else None


def risk_payload(patient_id: int) -> dict[str, Any]:
    return predict_explanation(patient_id)


def can_access_patient(user: User, patient: User) -> bool:
    return (
        user.id == patient.id
        or user.role == "Doctor"
        and patient.linked_doctor_id == user.id
        or user.role == "Caregiver"
        and patient.linked_caregiver_id == user.id
    )


def medication_snapshot(medication: Medication) -> dict[str, Any]:
    return {
        "id": medication.id,
        "patient_id": medication.patient_id,
        "name": medication.name,
        "status": medication.status,
        "schedules": [
            {
                "id": schedule.id,
                "time": schedule.time,
                "dosage": schedule.dosage,
                "status": schedule.status,
                "compartment": schedule.compartment,
                "tablet_weight": schedule.tablet_weight,
                "expected_quantity": schedule.expected_quantity,
                "tolerance": schedule.tolerance,
                "calibration_offset": schedule.calibration_offset,
                "response_window_minutes": schedule.response_window_minutes,
                "noise_threshold": schedule.noise_threshold,
            }
            for schedule in sorted(medication.schedules, key=lambda item: item.id)
        ],
    }


def add_medication_audit(
    patient_id: int,
    actor_id: int,
    action: str,
    medication: Medication | None,
    before_data: dict[str, Any] | None,
    reason: str | None = None,
    request_id: int | None = None,
) -> None:
    db.session.add(MedicationAudit(
        patient_id=patient_id,
        medication_id=medication.id if medication else None,
        request_id=request_id,
        actor_id=actor_id,
        action=action,
        before_data=before_data,
        after_data=medication_snapshot(medication) if medication else None,
        reason=reason,
        created_at=utc_now(),
    ))


def normalize_medication_data(
    raw: Any, operation: str
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict):
        return None, "medication data must be an object"
    allowed = {
        "name", "dosage", "time", "compartment", "tablet_weight",
        "expected_quantity", "tolerance", "calibration_offset",
        "response_window_minutes", "noise_threshold",
    }
    if set(raw) - allowed:
        return None, "medication data contains unsupported fields"
    if operation == "ADD" and not {"name", "dosage", "time"} <= set(raw):
        return None, "name, dosage, and time are required for a new medication"
    if operation == "EDIT" and not raw:
        return None, "at least one medication field is required"
    data = dict(raw)
    for field in ("name", "dosage", "time"):
        if field not in data:
            continue
        value = data[field]
        if not isinstance(value, str) or not value.strip():
            return None, f"{field} must be a non-empty string"
        value = value.strip()
        maximum = 150 if field == "name" else 100 if field == "dosage" else 5
        if len(value) > maximum or field == "time" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            return None, f"{field} is invalid"
        data[field] = value
    if "compartment" in data:
        try:
            if isinstance(data["compartment"], bool):
                raise ValueError
            if isinstance(data["compartment"], float) and not data["compartment"].is_integer():
                raise ValueError
            data["compartment"] = int(data["compartment"]) if data["compartment"] not in (None, "") else None
        except (TypeError, ValueError):
            return None, "compartment must be an integer from 1 to 8"
        if data["compartment"] is not None and not 1 <= data["compartment"] <= 8:
            return None, "compartment must be an integer from 1 to 8"
    for field, default in (
        ("tablet_weight", 0.5),
        ("tolerance", 0.2),
        ("calibration_offset", 0.0),
        ("noise_threshold", 0.15),
    ):
        if field not in data:
            if operation == "ADD":
                data[field] = default
            continue
        try:
            data[field] = float(data[field])
        except (TypeError, ValueError):
            return None, f"{field} must be a finite number"
        if not math.isfinite(data[field]):
            return None, f"{field} must be a finite number"
        if field != "calibration_offset" and data[field] <= 0:
            return None, f"{field} must be greater than zero"
    for field, default in (("expected_quantity", 1), ("response_window_minutes", 30)):
        if field not in data:
            if operation == "ADD":
                data[field] = default
            continue
        try:
            number = int(data[field])
        except (TypeError, ValueError):
            return None, f"{field} is invalid"
        if isinstance(data[field], bool) or str(number) != str(data[field]).strip() and not isinstance(data[field], int):
            return None, f"{field} is invalid"
        maximum = 100 if field == "expected_quantity" else 1440
        if not 1 <= number <= maximum:
            return None, f"{field} is outside the allowed range"
        data[field] = number
    return data, None


def apply_medication_data(
    patient: User,
    data: dict[str, Any],
    actor_id: int,
    set_by_role: str,
    medication: Medication | None = None,
) -> Medication:
    name = data.get("name", medication.name if medication else None)
    is_new = medication is None
    if medication is None:
        medication = Medication(
            patient_id=patient.id,
            name=name,
            status="Active",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db.session.add(medication)
        db.session.flush()
    elif "name" in data:
        medication.name = name
        medication.updated_at = utc_now()
        for schedule in medication.schedules:
            schedule.med_name = name

    schedules = [item for item in medication.schedules if item.status == "Active"]
    schedule = schedules[0] if schedules else None
    if schedule is None and (is_new or "time" in data or "dosage" in data):
        schedule = Reminder(
            patient_id=patient.id,
            medication_id=medication.id,
            medication=medication,
            med_name=medication.name,
            time=data.get("time", "08:00"),
            dosage=data.get("dosage", ""),
            set_by_role=set_by_role,
        )
        db.session.add(schedule)
    elif schedule is not None:
        schedule.med_name = medication.name
        schedule.set_by_role = set_by_role
    if schedule is not None:
        for field in (
            "time", "dosage", "compartment", "tablet_weight", "expected_quantity",
            "tolerance", "calibration_offset", "response_window_minutes", "noise_threshold",
        ):
            if field in data:
                setattr(schedule, field, data[field])
        db.session.flush()
        _queue_patient_event(
            patient, "schedule_updated",
            {"patient_id": patient.id, "schedule": schedule.to_dict()},
        )
    medication.status = "Active"
    medication.updated_at = utc_now()
    return medication


def notify_medication_change(
    patient: User, sender: User, message: str, recipient: User | None = None
) -> None:
    db.session.add(Notification(
        patient_id=patient.id,
        sender_id=sender.id,
        recipient_id=(recipient or patient).id,
        message=message,
        created_at=utc_now(),
    ))


def medication_records(patient_id: int) -> list[dict[str, Any]]:
    medications = db.session.scalars(db.select(Medication).where(
        Medication.patient_id == patient_id
    ).order_by(Medication.name, Medication.id)).all()
    return [item.to_dict() for item in medications]


def medication_requests_for(patient_id: int) -> list[dict[str, Any]]:
    requests_for_patient = db.session.scalars(db.select(MedicationChangeRequest).where(
        MedicationChangeRequest.patient_id == patient_id
    ).order_by(MedicationChangeRequest.created_at.desc())).all()
    return [item.to_dict() for item in requests_for_patient]


def dispatch_alert(patient: User, message: str) -> str:
    """Store an alert and deliver it through TextBee or configured SMTP."""
    sender_id = patient.linked_caregiver_id or patient.id
    db.session.add(
        Notification(
            patient_id=patient.id,
            sender_id=sender_id,
            recipient_id=patient.id,
            message=message,
            created_at=utc_now(),
        )
    )
    textbee_url = os.environ.get("TEXTBEE_API_URL")
    textbee_key = os.environ.get("TEXTBEE_API_KEY")
    phone = os.environ.get("PATIENT_PHONE")
    if textbee_url and textbee_key and phone:
        payload = urllib.request.Request(
            textbee_url,
            data=json.dumps({"phone": phone, "message": message}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {textbee_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(payload, timeout=10):
                return "textbee"
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    if smtp_host and smtp_user and smtp_password:
        email = EmailMessage()
        email["Subject"] = "PillGuard medication alert"
        email["From"] = smtp_user
        email["To"] = patient.email
        email.set_content(message)
        with smtplib.SMTP_SSL(
            smtp_host,
            int(os.environ.get("SMTP_PORT", "465")),
        ) as smtp:
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(email)
        return "smtp"
    return "in_app"


def record_hardware_traffic(endpoint: str, method: str, payload: Any, response_status: int) -> None:
    safe_payload = {
        key: payload[key]
        for key in ("patient_id", "reminder_id", "compartment")
        if isinstance(payload, dict) and key in payload
    }
    db.session.add(
        HardwareTraffic(
            patient_id=safe_payload.get("patient_id"),
            endpoint=endpoint,
            method=method,
            payload=json.dumps(safe_payload, separators=(",", ":")),
            response_status=response_status,
            created_at=utc_now(),
        )
    )


def patient_timezone(patient: User) -> ZoneInfo:
    return ZoneInfo(patient.timezone)


def local_occurrence(patient: User, reminder: Reminder, local_date: date) -> datetime | None:
    hour, minute = (int(part) for part in reminder.time.split(":"))
    local_time = datetime.combine(local_date, time(hour, minute)).replace(
        tzinfo=patient_timezone(patient)
    )
    # A local time in the spring DST gap has no corresponding instant.
    if local_time.astimezone(timezone.utc).astimezone(local_time.tzinfo).replace(tzinfo=None) != local_time.replace(tzinfo=None):
        return None
    return local_time.astimezone(timezone.utc)


def event_schedule(
    patient: User, reminder: Reminder, when: datetime, window_minutes: int | None = None
) -> datetime | None:
    now = as_utc(when).astimezone(patient_timezone(patient))
    response_window = reminder.response_window_minutes if window_minutes is None else window_minutes
    candidates = [
        occurrence
        for local_date in (now.date() - timedelta(days=1), now.date(), now.date() + timedelta(days=1))
        if (occurrence := local_occurrence(patient, reminder, local_date)) is not None
        and occurrence <= as_utc(when)
        and as_utc(when) <= occurrence + timedelta(minutes=response_window)
    ]
    return max(candidates) if candidates else None


def event_key(patient_id: int, reminder_id: int, scheduled_at: datetime) -> str:
    return f"{scheduled_at.isoformat()}:{patient_id}:{reminder_id}"


def event_for_occurrence(
    patient: User, reminder: Reminder, scheduled_at: datetime, source: str
) -> tuple[DoseEvent, bool]:
    key = event_key(patient.id, reminder.id, scheduled_at)
    event = db.session.scalar(db.select(DoseEvent).where(DoseEvent.event_key == key))
    if event is None:
        event = db.session.scalar(db.select(DoseEvent).where(
            DoseEvent.patient_id == patient.id,
            DoseEvent.reminder_id == reminder.id,
            DoseEvent.scheduled_at == scheduled_at,
        ))
    if event is not None:
        return event, False
    event = DoseEvent(
        event_key=key,
        patient_id=patient.id,
        reminder_id=reminder.id,
        compartment=reminder.compartment,
        scheduled_at=scheduled_at,
        due_at=scheduled_at,
        state="REMINDER_DUE",
        source=source,
    )
    db.session.add(event)
    db.session.flush()
    return event, True


def refresh_due_events(patient: User, now: datetime) -> bool:
    now = as_utc(now)
    local_today = now.astimezone(patient_timezone(patient)).date()
    changed = False
    reminders = db.session.scalars(db.select(Reminder).where(
        Reminder.patient_id == patient.id,
        Reminder.status == "Active",
    )).all()
    for reminder in reminders:
        for local_date in (local_today - timedelta(days=1), local_today):
            scheduled_at = local_occurrence(patient, reminder, local_date)
            if scheduled_at is None or scheduled_at > now:
                continue
            event, created = event_for_occurrence(patient, reminder, scheduled_at, "schedule")
            changed = created or changed
            if event.state == "REMINDER_DUE":
                event.state = "ALERTING"
                changed = True
            changed = expire_event(event, now) or changed
    return changed


def next_scheduled_dose(patient: User, now: datetime) -> dict[str, Any] | None:
    now = as_utc(now)
    local_today = now.astimezone(patient_timezone(patient)).date()
    reminders = db.session.scalars(db.select(Reminder).where(
        Reminder.patient_id == patient.id,
        Reminder.status == "Active",
    )).all()
    upcoming = [
        (scheduled_at, reminder)
        for reminder in reminders
        for local_date in (local_today, local_today + timedelta(days=1), local_today + timedelta(days=2))
        if (scheduled_at := local_occurrence(patient, reminder, local_date)) is not None
        and scheduled_at > now
    ]
    if not upcoming:
        return None
    scheduled_at, reminder = min(upcoming, key=lambda item: item[0])
    return {"scheduled_at": scheduled_at.isoformat(), "reminder": reminder.to_dict()}


def get_or_create_dose_event(
    patient: User, reminder: Reminder, when: datetime, source: str
) -> DoseEvent | None:
    scheduled_at = event_schedule(patient, reminder, when)
    if scheduled_at is None:
        return None
    event, _ = event_for_occurrence(patient, reminder, scheduled_at, source)
    if event.state == "REMINDER_DUE" and as_utc(when) >= scheduled_at:
        event.state = "ALERTING"
    return event


def create_pill_log(patient: User, **values: Any) -> PillLog:
    pill_log = PillLog(patient_id=patient.id, **values)
    db.session.add(pill_log)
    db.session.flush()
    _queue_patient_event(
        patient, "log_updated",
        {"patient_id": patient.id, "log": pill_log.to_dict()},
    )
    return pill_log


def expire_event(event: DoseEvent, now: datetime) -> bool:
    reminder = db.session.get(Reminder, event.reminder_id)
    if (
        reminder is None
        or event.state in {"MISSED", "MANUALLY_CONFIRMED"}
        or now <= as_utc(event.due_at) + timedelta(minutes=reminder.response_window_minutes)
    ):
        return False
    event.state = "MISSED"
    event.completed_at = now
    if not db.session.scalar(db.select(PillLog).where(PillLog.dose_event_id == event.id)):
        patient = db.session.get(User, event.patient_id)
        if patient is None:
            raise ValueError("dose event patient no longer exists")
        create_pill_log(
            patient,
            reminder_id=event.reminder_id,
            dose_event_id=event.id,
            compartment=event.compartment,
            timestamp=now,
            initial_weight=event.initial_weight or 0.0,
            final_weight=event.final_weight or 0.0,
            delta_weight=event.filtered_delta or 0.0,
            status="Missed",
            source=event.source,
            verification_method="response_window_expired",
        )
    if not event.notification_sent:
        patient = db.session.get(User, event.patient_id)
        dispatch_alert(patient, "Missed medication detected. Please check the patient's pill box.")
        event.notification_sent = True
    return True


def complete_dose_event(
    event: DoseEvent, now: datetime, verification_method: str
) -> tuple[PillLog, str | None]:
    existing = db.session.scalar(db.select(PillLog).where(PillLog.dose_event_id == event.id))
    if existing:
        return existing, None
    patient = db.session.get(User, event.patient_id)
    if patient is None:
        raise ValueError("dose event patient no longer exists")
    pill_log = create_pill_log(
        patient,
        reminder_id=event.reminder_id,
        dose_event_id=event.id,
        compartment=event.compartment,
        timestamp=now,
        initial_weight=event.initial_weight or 0.0,
        final_weight=event.final_weight or 0.0,
        delta_weight=event.filtered_delta or 0.0,
        status="Manual Override",
        source=event.source,
        verification_method=verification_method,
    )
    event.state = "MANUALLY_CONFIRMED"
    event.verification_method = verification_method
    event.completed_at = now
    return pill_log, None


def reminder_for_input(
    patient_id: int,
    reminder_id: Any,
    compartment: Any,
    when: datetime | None = None,
) -> Reminder | None:
    now = when or utc_now()
    statement = db.select(Reminder).where(
        Reminder.patient_id == patient_id,
        Reminder.status == "Active",
    )
    if isinstance(reminder_id, int) and not isinstance(reminder_id, bool):
        statement = statement.where(Reminder.id == reminder_id)
    elif isinstance(compartment, int) and not isinstance(compartment, bool):
        statement = statement.where(Reminder.compartment == compartment)
    elif reminder_id is not None or compartment is not None:
        return None
    reminders = db.session.scalars(statement.order_by(Reminder.time, Reminder.id)).all()
    if isinstance(reminder_id, int) or isinstance(compartment, int):
        if len(reminders) != 1:
            return None
        reminder = reminders[0]
        return reminder if event_schedule(reminder.patient, reminder, now) is not None else None
    due = [
        reminder for reminder in reminders
        if event_schedule(reminder.patient, reminder, now) is not None
    ]
    return due[0] if len(due) == 1 else None


def active_reminder_for_simulation(
    patient_id: Any,
    reminder_id: Any,
    compartment: Any,
) -> Reminder | None:
    if not isinstance(patient_id, int) or isinstance(patient_id, bool):
        return None
    statement = db.select(Reminder).where(
        Reminder.patient_id == patient_id,
        Reminder.status == "Active",
    )
    if isinstance(reminder_id, int) and not isinstance(reminder_id, bool):
        statement = statement.where(Reminder.id == reminder_id)
    elif isinstance(compartment, int) and not isinstance(compartment, bool):
        statement = statement.where(Reminder.compartment == compartment)
    else:
        return None
    reminders = db.session.scalars(statement.limit(2)).all()
    return reminders[0] if len(reminders) == 1 else None


def process_sensor_reading(
    patient: User,
    reminder: Reminder,
    before: float,
    after: float,
    source: str,
    samples_before: list[float] | None = None,
    samples_after: list[float] | None = None,
    when: datetime | None = None,
    dose_event: DoseEvent | None = None,
) -> tuple[DoseEvent, PillLog | None, dict[str, Any]]:
    now = when or utc_now()
    if dose_event and (dose_event.patient_id != patient.id or dose_event.reminder_id != reminder.id):
        raise ValueError("dose event must match the patient and schedule being processed")
    event = dose_event or get_or_create_dose_event(patient, reminder, now, source)
    if event is None:
        raise ValueError("no scheduled dose is due within its response window")
    if event.state in {"MISSED", "MANUALLY_CONFIRMED"} or event.completed_at:
        return event, db.session.scalar(db.select(PillLog).where(PillLog.dose_event_id == event.id)), {}
    if expire_event(event, now):
        return event, None, {}
    def clean_samples(values: list[float] | None, fallback: float) -> list[float]:
        if values is None:
            return [fallback]
        if not values or len(values) > 1000 or not all(valid_weight(value) for value in values):
            raise ValueError("sensor sample arrays must contain 1 to 1000 valid readings")
        return [float(value) for value in values]

    before_readings = clean_samples(samples_before, before)
    after_readings = clean_samples(samples_after, after)
    before_value = median(before_readings) + reminder.calibration_offset
    after_value = median(after_readings) + reminder.calibration_offset
    readings = before_readings + after_readings
    sensor_analysis = analyze_readings(
        readings,
        reminder.tablet_weight * reminder.expected_quantity,
        reminder.tolerance,
        reminder.calibration_offset,
        reminder.noise_threshold,
    )
    delta = before_value - after_value
    event.initial_weight = before_value
    event.final_weight = after_value
    event.filtered_delta = delta
    event.detected_at = now
    event.source = source
    expected = reminder.tablet_weight * reminder.expected_quantity
    if abs(delta - expected) <= reminder.tolerance:
        event.state = "REMOVAL_DETECTED"
        event.verification_method = "calibrated_weight_delta"
    db.session.add(SensorEvent(
        patient_id=patient.id,
        scheduled_dose_id=reminder.id,
        dose_event_id=event.id,
        recorded_at=now,
        source=source,
        readings_before=before_readings,
        readings_after=after_readings,
        filtered_delta=delta,
        analysis=sensor_analysis,
    ))
    return event, None, sensor_analysis


def alarm_key(patient_id: int, reminder_id: int, when: datetime) -> str:
    return f"{when.date().isoformat()}:{patient_id}:{reminder_id}:{when.hour:02d}:{when.minute:02d}"


def scheduled_alarm_is_acknowledged(patient_id: int) -> bool:
    return any(
        key.startswith(f"{datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()}:{patient_id}:")
        for key in session.get("acknowledged_alarms", [])
    )


def register_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        if current_user() is not None:
            return redirect(url_for("dashboard"))
        return render_template("landing.html")

    @app.get("/login")
    def login_page():
        if current_user() is not None:
            return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="login")

    @app.get("/register")
    def register_page():
        if current_user() is not None:
            return redirect(url_for("dashboard"))
        return render_template(
            "auth.html",
            mode="register",
            allow_public_doctor_registration=os.environ.get("ALLOW_PUBLIC_DOCTOR_REGISTRATION", "").lower() == "true",
        )

    @app.get("/dashboard")
    def dashboard():
        user = current_user()
        if user is None:
            return redirect(url_for("login_page"))
        role_pages = {"Doctor": "doctor", "Patient": "patient", "Caregiver": "caregiver"}
        return redirect(url_for("role_dashboard", role=role_pages[user.role]))

    @app.get("/dashboard/<role>")
    def role_dashboard(role: str):
        user = current_user()
        if user is None:
            return redirect(url_for("login_page"))
        role_pages = {"Doctor": "doctor", "Patient": "patient", "Caregiver": "caregiver"}
        if role_pages.get(user.role) != role:
            return redirect(url_for("role_dashboard", role=role_pages[user.role]))
        return render_template("index.html")

    @app.get("/.well-known/appspecific/com.chrome.devtools.json")
    def chrome_devtools_probe():
        return "", 204

    @app.post("/api/register")
    def register():
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        email = data.get("email")
        password = data.get("password")
        role = data.get("role")

        if not all(isinstance(value, str) and value.strip() for value in (name, email, password)):
            return json_error("name, email, and password are required", 400)
        role = role.title() if isinstance(role, str) else role
        if role not in {"Doctor", "Patient", "Caregiver"}:
            return json_error("role must be Doctor, Patient, or Caregiver", 400)
        if role == "Doctor" and os.environ.get("ALLOW_PUBLIC_DOCTOR_REGISTRATION", "").lower() != "true":
            return json_error("doctor registration requires an administrator invitation", 403)
        if len(password) < 8:
            return json_error("password must be at least 8 characters", 400)

        normalized_email = email.strip().lower()
        if len(name.strip()) > 120 or len(normalized_email) > 255 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_email):
            return json_error("name or email is invalid", 400)
        if db.session.scalar(db.select(User).where(User.email == normalized_email)):
            return json_error("email is already registered", 409)

        user = User(name=name.strip(), email=normalized_email, role=role, password_hash="")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return jsonify({"status": "success", "user": user.to_dict()}), 201

    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        email = data.get("email")
        password = data.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            return json_error("email and password are required", 400)

        user = db.session.scalar(db.select(User).where(User.email == email.strip().lower()))
        if user is None or not user.check_password(password):
            return json_error("invalid email or password", 401)

        session.clear()
        session["user_id"] = user.id
        return jsonify({"status": "success", "user": user.to_dict()})

    @app.get("/api/logout")
    def logout():
        session.clear()
        return jsonify({"status": "success"})

    @app.get("/api/hardware/get-schedules")
    def get_schedules():
        patient_id = request.args.get("patient_id", type=int)
        device_patient_user = device_patient()
        if patient_id is None or patient_id <= 0:
            return json_error("patient_id must be a positive integer", 400)
        if device_patient_user is None or device_patient_user.id != patient_id:
            return json_error("valid device authentication is required", 401)
        if db.session.get(User, patient_id) is None or device_patient_user.role != "Patient":
            return json_error("patient not found", 404)

        reminders = db.session.scalars(
            db.select(Reminder)
            .where(Reminder.patient_id == patient_id, Reminder.status == "Active")
            .order_by(Reminder.time, Reminder.id)
        ).all()
        response = [reminder.to_dict() for reminder in reminders]
        record_hardware_traffic(request.path, request.method, {"patient_id": patient_id}, 200)
        db.session.commit()
        return jsonify(response)

    @app.post("/api/hardware/log-event")
    def log_event():
        data = request.get_json(silent=True) or {}
        patient_id = data.get("patient_id")
        w_before = data.get("w_before")
        w_after = data.get("w_after")
        if not isinstance(patient_id, int) or isinstance(patient_id, bool) or patient_id <= 0:
            return json_error("patient_id must be a positive integer", 400)
        device_patient_user = device_patient()
        if device_patient_user is None or device_patient_user.id != patient_id:
            return json_error("valid device authentication is required", 401)
        if not all(valid_weight(value) for value in (w_before, w_after)):
            return json_error("weights must be finite numbers from 0 to 100000", 400)
        if not all(valid_samples(data.get(key)) for key in ("samples_before", "samples_after")):
            return json_error("sensor sample arrays must contain 1 to 1000 valid readings", 400)
        if db.session.get(User, patient_id) is None or device_patient_user.role != "Patient":
            return json_error("patient not found", 404)
        reminder = reminder_for_input(patient_id, data.get("reminder_id"), data.get("compartment"))
        if reminder is None:
            return json_error("an active medication schedule is required", 409)
        event, pill_log, sensor_analysis = process_sensor_reading(
            device_patient_user, reminder, float(w_before), float(w_after), "hardware",
            data.get("samples_before"), data.get("samples_after"),
        )
        record_hardware_traffic(request.path, request.method, data, 200)
        db.session.commit()
        return jsonify(
            {
                "status": "success",
                "state": event.state,
                "event": event.to_dict(),
                "sensor_analysis": sensor_analysis,
                "log": pill_log.to_dict() if pill_log else None,
            }
        ), 200

    @app.post("/api/simulation/log-event")
    @role_required("Doctor", "Caregiver", "Patient")
    def simulation_log_event():
        data = request.get_json(silent=True) or {}
        patient_id = data.get("patient_id")
        w_before, w_after = data.get("w_before"), data.get("w_after")
        actor = current_user()
        patient = get_patient(patient_id)
        if patient is None or not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient", 403)
        if not all(valid_weight(value) for value in (w_before, w_after)):
            return json_error("weights must be finite numbers from 0 to 100000", 400)
        if not all(valid_samples(data.get(key)) for key in ("samples_before", "samples_after")):
            return json_error("sensor sample arrays must contain 1 to 1000 valid readings", 400)
        reminder = reminder_for_input(patient.id, data.get("reminder_id"), data.get("compartment"))
        if reminder is None:
            return json_error("an active medication schedule is required", 409)
        existing_event = db.session.scalar(
            db.select(DoseEvent).where(
                DoseEvent.patient_id == patient.id,
                DoseEvent.reminder_id == reminder.id,
                DoseEvent.state.in_(("AWAITING_CONFIRMATION", "REMOVAL_DETECTED")),
            ).order_by(DoseEvent.scheduled_at.desc())
        )
        if existing_event is None:
            return json_error("acknowledge the medication alarm before simulating", 409)
        event, pill_log, sensor_analysis = process_sensor_reading(
            patient, reminder, float(w_before), float(w_after), "simulation",
            data.get("samples_before"), data.get("samples_after"),
        )
        record_hardware_traffic("/api/simulation/log-event", "POST", data, 201)
        db.session.commit()
        return jsonify({
            "status": "success",
            "state": event.state,
            "event": event.to_dict(),
            "delta_weight": event.filtered_delta,
            "sensor_analysis": sensor_analysis,
            "risk": risk_payload(patient.id),
            "log": pill_log.to_dict() if pill_log else None,
        }), 200

    @app.get("/api/simulation/events")
    @role_required("Doctor", "Patient", "Caregiver")
    def simulation_events():
        actor = current_user()
        patient = get_patient(request.args.get("patient_id", type=int))
        if patient is None or not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient", 403)
        now = utc_now()
        events = db.session.scalars(
            db.select(DoseEvent).where(DoseEvent.patient_id == patient.id)
            .order_by(DoseEvent.scheduled_at.desc()).limit(30)
        ).all()
        changed = refresh_due_events(patient, now)
        if changed:
            db.session.commit()
            events = db.session.scalars(
                db.select(DoseEvent).where(DoseEvent.patient_id == patient.id)
                .order_by(DoseEvent.scheduled_at.desc()).limit(30)
            ).all()
        return jsonify({"events": [event.to_dict() for event in events]})

    @app.get("/api/hardware/traffic")
    @role_required("Doctor", "Caregiver")
    def hardware_traffic():
        actor = current_user()
        if actor.role == "Doctor":
            patients = db.session.scalars(
                db.select(User.id).where(User.linked_doctor_id == actor.id)
            ).all()
        else:
            patients = db.session.scalars(
                db.select(User.id).where(User.linked_caregiver_id == actor.id)
            ).all()
        if not patients:
            return jsonify({"traffic": []})
        entries = db.session.scalars(
            db.select(HardwareTraffic)
            .where(HardwareTraffic.patient_id.in_(patients))
            .order_by(HardwareTraffic.created_at.desc())
            .limit(50)
        ).all()
        return jsonify({"traffic": [entry.to_dict() for entry in entries]})

    @app.get("/api/me")
    @login_required
    def current_user_endpoint():
        user = db.session.get(User, session["user_id"])
        if user is None:
            session.clear()
            return json_error("Authenticated user no longer exists", 401)
        return jsonify({"user": user.to_dict()})

    @app.get("/api/stream")
    @login_required
    def stream_events():
        user_id = session["user_id"]
        client_queue: queue.Queue[str] = queue.Queue()

        def generate():
            with _sse_lock:
                _sse_clients.setdefault(user_id, []).append(client_queue)
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        message = client_queue.get(timeout=20)
                    except queue.Empty:
                        yield ": heartbeat\n\n"
                    else:
                        yield f"data: {message}\n\n"
            finally:
                with _sse_lock:
                    clients = _sse_clients.get(user_id, [])
                    if client_queue in clients:
                        clients.remove(client_queue)
                    if not clients:
                        _sse_clients.pop(user_id, None)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/aiml/prediction/<int:patient_id>")
    @app.get("/api/aiml/risk/<int:patient_id>")
    @role_required("Doctor", "Patient", "Caregiver")
    def adherence_risk(patient_id: int):
        viewer = current_user()
        patient = get_patient(patient_id)
        allowed = patient is not None and can_access_patient(viewer, patient)
        if not allowed:
            return json_error("You do not have access to this patient's risk", 403)
        return jsonify({"patient_id": patient_id, **risk_payload(patient_id)})

    @app.get("/api/aiml/model")
    @role_required("Doctor", "Patient", "Caregiver")
    def adherence_model_info():
        return jsonify(model_metadata())

    @app.get("/api/sensor/model")
    @role_required("Doctor", "Patient", "Caregiver")
    def sensor_model_info():
        return jsonify(evaluate_sensor_model())

    @app.get("/api/sensor/demo")
    @role_required("Doctor", "Patient", "Caregiver")
    def sensor_demo():
        return jsonify({
            "data_source": "synthetic demo scenarios",
            "validation_limitations": "Synthetic signals are not representative of calibrated physical hardware.",
            "evaluation": evaluate_sensor_model(),
            "scenarios": demo_sensor_analyses(),
        })

    @app.post("/api/assistant/chat")
    @role_required("Doctor", "Patient", "Caregiver")
    def assistant_chat():
        actor = current_user()
        data = request.get_json(silent=True) or {}
        question = data.get("question")
        patient_id = data.get("patient_id")
        if not isinstance(question, str) or not question.strip() or len(question.strip()) > 500:
            return json_error("question is required and must be 500 characters or fewer", 400)
        if actor.role == "Patient":
            patient = actor
        elif patient_id is None:
            patient = None
        else:
            patient = get_patient(patient_id)
            if patient is None or patient.role != "Patient":
                return json_error("patient_id is required for this role", 400)
        if patient is not None and not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient's assistant context", 403)
        result = answer_question(
            question,
            actor,
            patient,
            db,
            Reminder,
            PillLog,
            MedicationChangeRequest,
            predict_explanation,
        )
        if patient is None and result["intent"] not in {
            "navigation", "features", "unsupported", "medical_boundary",
        }:
            return json_error("Select an assigned patient for patient-specific assistant questions", 400)
        return jsonify(result)

    @app.get("/api/doctor/patients")
    @role_required("Doctor")
    def doctor_patients():
        doctor = current_user()
        patients = db.session.scalars(
            db.select(User).where(User.linked_doctor_id == doctor.id).order_by(User.name)
        ).all()
        return jsonify(
            {
                "patients": [
                    {
                        **user_summary(patient),
                        "caregiver": user_summary(patient.caregiver)
                        if patient.caregiver
                        else None,
                        "risk": risk_payload(patient.id),
                        "medications": medication_records(patient.id),
                        "medication_requests": medication_requests_for(patient.id),
                        "reminders": [
                            reminder.to_dict()
                            for reminder in patient.reminders
                            if reminder.status == "Active"
                        ],
                        "logs": [
                            log.to_dict()
                            for log in db.session.scalars(
                                db.select(PillLog)
                                .where(PillLog.patient_id == patient.id)
                                .order_by(PillLog.timestamp.desc())
                                .limit(10)
                            ).all()
                        ],
                    }
                    for patient in patients
                ]
            }
        )

    @app.get("/api/doctor/directory")
    @role_required("Doctor")
    def doctor_directory():
        query = request.args.get("search", "").strip().lower()
        statement = db.select(User).where(User.role == "Patient").order_by(User.name)
        if query:
            statement = statement.where(
                or_(User.email.ilike(f"%{query}%"), User.name.ilike(f"%{query}%"))
            )
        patients = db.session.scalars(statement.limit(100)).all()
        return jsonify(
            {
                "patients": [
                    {
                        **user_summary(patient),
                        "linked_to_doctor": patient.linked_doctor_id == current_user().id,
                        "assigned_doctor_id": patient.linked_doctor_id,
                    }
                    for patient in patients
                ]
            }
        )
    @app.post("/api/doctor/patients/link")
    @role_required("Doctor")
    def link_patient():
        data = request.get_json(silent=True) or {}
        email = data.get("email")
        if not isinstance(email, str) or not email.strip():
            return json_error("patient email is required", 400)
        patient = db.session.scalar(
            db.select(User).where(User.email == email.strip().lower(), User.role == "Patient")
        )
        if patient is None:
            return json_error("patient not found", 404)
        doctor = current_user()
        if patient.linked_doctor_id not in (None, doctor.id):
            return json_error("patient is already assigned to another doctor", 409)
        patient.linked_doctor_id = doctor.id
        db.session.commit()
        return jsonify({"status": "success", "patient": user_summary(patient)})

    @app.post("/api/doctor/patients/<int:patient_id>/caregiver")
    @role_required("Doctor")
    def assign_caregiver(patient_id: int):
        doctor = current_user()
        patient = get_patient(patient_id)
        data = request.get_json(silent=True) or {}
        email = data.get("email")
        if patient is None or patient.role != "Patient" or patient.linked_doctor_id != doctor.id:
            return json_error("assigned patient not found", 404)
        if not isinstance(email, str) or not email.strip():
            return json_error("caregiver email is required", 400)
        caregiver = db.session.scalar(
            db.select(User).where(User.email == email.strip().lower(), User.role == "Caregiver")
        )
        if caregiver is None:
            return json_error("caregiver not found", 404)
        patient.linked_caregiver_id = caregiver.id
        db.session.commit()
        return jsonify({"status": "success", "caregiver": user_summary(caregiver)})

    @app.post("/api/doctor/patients/<int:patient_id>/devices")
    @role_required("Doctor")
    def provision_device(patient_id: int):
        doctor = current_user()
        patient = get_patient(patient_id)
        if patient is None or patient.role != "Patient" or patient.linked_doctor_id != doctor.id:
            return json_error("assigned patient not found", 404)
        data = request.get_json(silent=True) or {}
        name = data.get("name", "Pill box")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            return json_error("device name is required and must be 120 characters or fewer", 400)
        token = secrets.token_urlsafe(32)
        device = Device(
            patient_id=patient.id,
            name=name.strip(),
            token_hash=token_digest(token),
            created_at=utc_now(),
        )
        db.session.add(device)
        db.session.commit()
        return jsonify({
            "status": "success",
            "device": {"id": device.id, "patient_id": device.patient_id, "name": device.name, "active": device.active},
            "device_key": token,
        }), 201

    @app.get("/api/patients/<int:patient_id>/medications")
    @role_required("Doctor", "Patient", "Caregiver")
    def patient_medications(patient_id: int):
        actor = current_user()
        patient = get_patient(patient_id)
        if patient is None or not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient's medications", 403)
        medications = db.session.scalars(
            db.select(Medication)
            .where(Medication.patient_id == patient.id)
            .order_by(Medication.name, Medication.id)
        ).all()
        return jsonify({"medications": [item.to_dict() for item in medications]})

    @app.post("/api/patients/<int:patient_id>/medication-requests")
    @role_required("Patient", "Caregiver")
    def submit_medication_request(patient_id: int):
        actor = current_user()
        patient = get_patient(patient_id)
        if patient is None or not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient's medications", 403)
        if patient.linked_doctor_id is None:
            return json_error("A linked doctor is required to review medication changes", 409)
        doctor = db.session.get(User, patient.linked_doctor_id)
        if doctor is None or doctor.role != "Doctor":
            return json_error("The patient's assigned doctor is unavailable", 409)
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return json_error("request body must be an object", 400)
        operation = data.get("operation")
        if operation not in {"ADD", "EDIT", "REMOVE"}:
            return json_error("operation must be ADD, EDIT, or REMOVE", 400)
        medication_id = data.get("medication_id")
        medication = None
        if operation == "ADD":
            if medication_id is not None:
                return json_error("ADD requests cannot target an existing medication", 400)
            requested_data, error = normalize_medication_data(data.get("requested_data"), operation)
            if error is None and db.session.scalar(db.select(Medication.id).where(
                Medication.patient_id == patient.id,
                db.func.lower(Medication.name) == requested_data["name"].lower(),
                Medication.status == "Active",
            )):
                return json_error("an active medication with this name already exists", 409)
        else:
            if not isinstance(medication_id, int) or isinstance(medication_id, bool):
                return json_error("medication_id is required", 400)
            medication = db.session.get(Medication, medication_id)
            if medication is None or medication.patient_id != patient.id or medication.status != "Active":
                return json_error("active medication not found for this patient", 404)
            requested_data, error = normalize_medication_data(
                {} if operation == "REMOVE" and data.get("requested_data") is None
                else data.get("requested_data", {}),
                operation if operation == "EDIT" else "REMOVE",
            )
        if error:
            return json_error(error, 400)
        if operation == "REMOVE" and requested_data:
            return json_error("REMOVE requests cannot include medication changes", 400)
        if operation == "ADD":
            fingerprint = hashlib.sha256(
                json.dumps(requested_data, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            pending_key = f"{patient.id}:ADD:{fingerprint}"
        else:
            pending_key = f"{patient.id}:{operation}:{medication.id}"
        if db.session.scalar(db.select(MedicationChangeRequest.id).where(
            MedicationChangeRequest.pending_key == pending_key
        )):
            return json_error("an identical medication change is already awaiting review", 409)
        change_request = MedicationChangeRequest(
            patient_id=patient.id,
            medication_id=medication.id if medication else None,
            requested_by_id=actor.id,
            operation=operation,
            requested_data=requested_data,
            pending_key=pending_key,
            status="PENDING",
            created_at=utc_now(),
        )
        db.session.add(change_request)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            return json_error("an identical medication change is already awaiting review", 409)
        notify_medication_change(
            patient,
            actor,
            f"{actor.name} requested to {operation.lower()} medication "
            f"{medication.name if medication else requested_data['name']}.",
            doctor,
        )
        db.session.commit()
        return jsonify({"status": "success", "request": change_request.to_dict()}), 201

    @app.get("/api/patients/<int:patient_id>/medication-requests")
    @role_required("Doctor", "Patient", "Caregiver")
    def patient_medication_requests(patient_id: int):
        actor = current_user()
        patient = get_patient(patient_id)
        if patient is None or not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient's medication requests", 403)
        requests_for_patient = db.session.scalars(
            db.select(MedicationChangeRequest)
            .where(MedicationChangeRequest.patient_id == patient.id)
            .order_by(MedicationChangeRequest.created_at.desc())
        ).all()
        return jsonify({"requests": [item.to_dict() for item in requests_for_patient]})

    @app.get("/api/patients/<int:patient_id>/medication-audit")
    @role_required("Doctor", "Patient", "Caregiver")
    def patient_medication_audit(patient_id: int):
        actor = current_user()
        patient = get_patient(patient_id)
        if patient is None or not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient's medication history", 403)
        entries = db.session.scalars(
            db.select(MedicationAudit)
            .where(MedicationAudit.patient_id == patient.id)
            .order_by(MedicationAudit.created_at.desc(), MedicationAudit.id.desc())
            .limit(200)
        ).all()
        return jsonify({"audit": [entry.to_dict() for entry in entries]})

    @app.get("/api/notifications")
    @role_required("Doctor", "Patient", "Caregiver")
    def user_notifications():
        actor = current_user()
        notifications = db.session.scalars(
            db.select(Notification)
            .where(Notification.recipient_id == actor.id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(100)
        ).all()
        return jsonify({"notifications": [item.to_dict() for item in notifications]})

    @app.get("/api/doctor/medication-requests")
    @role_required("Doctor")
    def doctor_medication_requests():
        doctor = current_user()
        patient_id = request.args.get("patient_id", type=int)
        statement = (
            db.select(MedicationChangeRequest)
            .join(User, MedicationChangeRequest.patient_id == User.id)
            .where(User.linked_doctor_id == doctor.id)
        )
        if patient_id is not None:
            statement = statement.where(MedicationChangeRequest.patient_id == patient_id)
        requests_for_doctor = db.session.scalars(
            statement.order_by(
                MedicationChangeRequest.status,
                MedicationChangeRequest.created_at.desc(),
            )
        ).all()
        return jsonify({"requests": [item.to_dict() for item in requests_for_doctor]})

    @app.patch("/api/doctor/medication-requests/<int:request_id>")
    @role_required("Doctor")
    def decide_medication_request(request_id: int):
        doctor = current_user()
        change_request = db.session.get(MedicationChangeRequest, request_id)
        patient = get_patient(change_request.patient_id) if change_request else None
        if (
            change_request is None
            or patient is None
            or patient.linked_doctor_id != doctor.id
        ):
            return json_error("medication request not found for an assigned patient", 404)
        if change_request.status != "PENDING":
            return json_error("medication request has already been decided", 409)
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return json_error("request body must be an object", 400)
        decision = data.get("decision")
        reason = data.get("reason", "")
        if decision not in {"APPROVED", "REJECTED"}:
            return json_error("decision must be APPROVED or REJECTED", 400)
        if not isinstance(reason, str) or len(reason.strip()) > 1000:
            return json_error("reason must be 1000 characters or fewer", 400)
        reason = reason.strip()
        if decision == "REJECTED" and not reason:
            return json_error("a reason is required when rejecting a request", 400)

        medication = db.session.get(Medication, change_request.medication_id) if change_request.medication_id else None
        before_data = medication_snapshot(medication) if medication else None
        if decision == "APPROVED":
            if change_request.operation == "ADD":
                normalized, error = normalize_medication_data(change_request.requested_data, "ADD")
                if error:
                    return json_error(f"request can no longer be applied: {error}", 409)
                existing = db.session.scalar(db.select(Medication).where(
                    Medication.patient_id == patient.id,
                    db.func.lower(Medication.name) == normalized["name"].lower(),
                    Medication.status == "Active",
                ))
                if existing:
                    return json_error("an active medication with this name already exists", 409)
                medication = apply_medication_data(patient, normalized, doctor.id, doctor.role)
            elif medication is None or medication.patient_id != patient.id or medication.status != "Active":
                return json_error("requested medication is no longer active", 409)
            elif change_request.operation == "EDIT":
                normalized, error = normalize_medication_data(change_request.requested_data, "EDIT")
                if error:
                    return json_error(f"request can no longer be applied: {error}", 409)
                apply_medication_data(patient, normalized, doctor.id, doctor.role, medication)
            else:
                medication.status = "Discontinued"
                medication.updated_at = utc_now()
                for schedule in medication.schedules:
                    if schedule.status == "Active":
                        schedule.status = "Completed"
            add_medication_audit(
                patient.id, doctor.id, f"{change_request.operation}_APPROVED",
                medication, before_data, reason or None, change_request.id,
            )
            notify_medication_change(
                patient, doctor,
                f"Your request to {change_request.operation.lower()} "
                f"{medication.name if medication else 'a medication'} was approved."
                + (f" Doctor's note: {reason}" if reason else ""),
            )
        else:
            add_medication_audit(
                patient.id, doctor.id, "REQUEST_REJECTED", medication,
                before_data, reason, change_request.id,
            )
            notify_medication_change(
                patient, doctor,
                f"Your request to {change_request.operation.lower()} "
                f"{medication.name if medication else change_request.requested_data.get('name', 'a medication')} "
                f"was rejected. Reason: {reason}",
            )
        change_request.status = decision
        change_request.decision_reason = reason or None
        change_request.decided_by_id = doctor.id
        change_request.decided_at = utc_now()
        change_request.pending_key = None
        db.session.commit()
        return jsonify({"status": "success", "request": change_request.to_dict()})

    @app.post("/api/doctor/patients/<int:patient_id>/medications")
    @role_required("Doctor")
    def doctor_add_medication(patient_id: int):
        doctor = current_user()
        patient = get_patient(patient_id)
        if patient is None or patient.linked_doctor_id != doctor.id:
            return json_error("assigned patient not found", 404)
        values, error = normalize_medication_data(request.get_json(silent=True), "ADD")
        if error:
            return json_error(error, 400)
        duplicate = db.session.scalar(db.select(Medication.id).where(
            Medication.patient_id == patient.id,
            db.func.lower(Medication.name) == values["name"].lower(),
            Medication.status == "Active",
        ))
        if duplicate:
            return json_error("an active medication with this name already exists", 409)
        medication = apply_medication_data(patient, values, doctor.id, doctor.role)
        add_medication_audit(patient.id, doctor.id, "CREATED", medication, None)
        notify_medication_change(patient, doctor, f"Your doctor added medication {medication.name}.")
        db.session.commit()
        return jsonify({"status": "success", "medication": medication.to_dict()}), 201

    @app.patch("/api/doctor/medications/<int:medication_id>")
    @role_required("Doctor")
    def doctor_update_medication(medication_id: int):
        doctor = current_user()
        medication = db.session.get(Medication, medication_id)
        patient = get_patient(medication.patient_id) if medication else None
        if medication is None or patient is None or patient.linked_doctor_id != doctor.id:
            return json_error("medication not found for an assigned patient", 404)
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return json_error("request body must be an object", 400)
        status = data.get("status")
        changes = {key: value for key, value in data.items() if key != "status"}
        if medication.status != "Active" and not (
            medication.status == "Discontinued" and status == "Archived" and not changes
        ):
            return json_error("only active medications can be changed", 409)
        if status is not None and status not in {"Discontinued", "Archived"}:
            return json_error("status must be Discontinued or Archived", 400)
        normalized, error = normalize_medication_data(changes, "EDIT") if changes else ({}, None)
        if error:
            return json_error(error, 400)
        if status is None and not changes:
            return json_error("a medication change is required", 400)
        before_data = medication_snapshot(medication)
        if normalized:
            if "name" in normalized and db.session.scalar(db.select(Medication.id).where(
                Medication.patient_id == patient.id,
                Medication.id != medication.id,
                db.func.lower(Medication.name) == normalized["name"].lower(),
                Medication.status == "Active",
            )):
                return json_error("an active medication with this name already exists", 409)
            apply_medication_data(patient, normalized, doctor.id, doctor.role, medication)
        if status:
            medication.status = status
            medication.updated_at = utc_now()
            for schedule in medication.schedules:
                if schedule.status == "Active":
                    schedule.status = "Completed"
        add_medication_audit(patient.id, doctor.id, status or "UPDATED", medication, before_data)
        notify_medication_change(
            patient, doctor, f"Your medication record {medication.name} was {status.lower() if status else 'updated'} by your doctor."
        )
        db.session.commit()
        return jsonify({"status": "success", "medication": medication.to_dict()})

    @app.post("/api/doctor/patients/<int:patient_id>/schedules")
    @role_required("Doctor")
    def create_schedule(patient_id: int):
        actor = current_user()
        patient = get_patient(patient_id)
        data = request.get_json(silent=True) or {}
        med_name, reminder_time, dosage = data.get("med_name"), data.get("time"), data.get("dosage")
        compartment = data.get("compartment")
        if isinstance(compartment, str):
            compartment = compartment.strip() or None
        tablet_weight = data.get("tablet_weight", 0.5)
        expected_quantity = data.get("expected_quantity", 1)
        tolerance = data.get("tolerance", 0.2)
        calibration_offset = data.get("calibration_offset", 0.0)
        response_window_minutes = data.get("response_window_minutes", 30)
        noise_threshold = data.get("noise_threshold", 0.15)
        can_edit = patient and (
            actor.role == "Doctor" and patient.linked_doctor_id == actor.id
        )
        if not can_edit:
            return json_error("only the assigned doctor can add medication directly", 403)
        values, error = normalize_medication_data({
            "name": med_name,
            "time": reminder_time,
            "dosage": dosage,
            "compartment": compartment,
            "tablet_weight": tablet_weight,
            "expected_quantity": expected_quantity,
            "tolerance": tolerance,
            "calibration_offset": calibration_offset,
            "response_window_minutes": response_window_minutes,
            "noise_threshold": noise_threshold,
        }, "ADD")
        if error:
            return json_error(error, 400)
        medication = db.session.scalar(db.select(Medication).where(
            Medication.patient_id == patient.id,
            db.func.lower(Medication.name) == values["name"].lower(),
        ))
        if medication is not None and medication.status != "Active":
            return json_error("a medication with this name is already retained in history", 409)
        before_data = medication_snapshot(medication) if medication else None
        if medication is None:
            medication = Medication(
                patient_id=patient.id,
                name=values["name"],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            db.session.add(medication)
            db.session.flush()
        reminder = Reminder(
            patient_id=patient.id,
            medication_id=medication.id,
            medication=medication,
            med_name=values["name"],
            time=values["time"],
            dosage=values["dosage"],
            set_by_role=actor.role,
            compartment=values["compartment"],
            tablet_weight=values["tablet_weight"],
            expected_quantity=values["expected_quantity"],
            tolerance=values["tolerance"],
            calibration_offset=values["calibration_offset"],
            response_window_minutes=values["response_window_minutes"],
            noise_threshold=values["noise_threshold"],
        )
        db.session.add(reminder)
        medication.updated_at = utc_now()
        add_medication_audit(patient.id, actor.id, "SCHEDULE_ADDED", medication, before_data)
        notify_medication_change(patient, actor, f"Your doctor added a schedule for {medication.name}.")
        _queue_patient_event(
            patient, "schedule_updated",
            {"patient_id": patient.id, "schedule": reminder.to_dict()},
        )
        db.session.commit()
        return jsonify({"status": "success", "schedule": reminder.to_dict()}), 201

    @app.get("/api/patient/dashboard")
    @role_required("Patient")
    def patient_dashboard():
        patient = current_user()
        if refresh_due_events(patient, utc_now()):
            db.session.commit()
        logs = db.session.scalars(
            db.select(PillLog).where(PillLog.patient_id == patient.id).order_by(PillLog.timestamp.desc()).limit(10)
        ).all()
        return jsonify(
            {
                "doctor": user_summary(patient.doctor) if patient.doctor else None,
                "caregiver": user_summary(patient.caregiver) if patient.caregiver else None,
                "risk": risk_payload(patient.id),
                "medications": medication_records(patient.id),
                "medication_requests": medication_requests_for(patient.id),
                "schedules": [r.to_dict() for r in patient.reminders if r.status == "Active"],
                "next_dose": next_scheduled_dose(patient, utc_now()),
                "timezone": patient.timezone,
                "logs": [log.to_dict() for log in logs],
            }
        )

    @app.post("/api/patient/timezone")
    @role_required("Patient")
    def update_patient_timezone():
        patient = current_user()
        data = request.get_json(silent=True) or {}
        zone_name = data.get("timezone")
        if not isinstance(zone_name, str) or not zone_name.strip() or len(zone_name.strip()) > 64:
            return json_error("timezone must be a valid IANA timezone", 400)
        try:
            ZoneInfo(zone_name.strip())
        except ZoneInfoNotFoundError:
            return json_error("timezone must be a valid IANA timezone", 400)
        patient.timezone = zone_name.strip()
        db.session.commit()
        return jsonify({"status": "success", "timezone": patient.timezone})

    @app.get("/api/caregiver/dashboard")
    @role_required("Caregiver")
    def caregiver_dashboard():
        caregiver = current_user()
        patients = db.session.scalars(
            db.select(User).where(User.linked_caregiver_id == caregiver.id).order_by(User.name)
        ).all()
        for patient in patients:
            refresh_due_events(patient, utc_now())
        patient_ids = [patient.id for patient in patients]
        logs = db.session.scalars(
            db.select(PillLog)
            .where(PillLog.patient_id.in_(patient_ids))
            .order_by(PillLog.timestamp.desc())
            .limit(50)
        ).all() if patient_ids else []
        return jsonify(
            {
                "patients": [
                    {
                        **user_summary(patient),
                        "risk": risk_payload(patient.id),
                        "medications": medication_records(patient.id),
                        "medication_requests": medication_requests_for(patient.id),
                        "reminders": [
                            reminder.to_dict()
                            for reminder in patient.reminders
                            if reminder.status == "Active"
                        ],
                    }
                    for patient in patients
                ],
                "logs": [
                    {**log.to_dict(), "patient_name": next(p.name for p in patients if p.id == log.patient_id)}
                    for log in logs
                ],
            }
        )

    @app.post("/api/caregiver/logs/<int:log_id>/override")
    @role_required("Caregiver")
    def override_log(log_id: int):
        caregiver = current_user()
        log = db.session.get(PillLog, log_id)
        patient = get_patient(log.patient_id) if log else None
        if log is None or patient is None or patient.linked_caregiver_id != caregiver.id:
            return json_error("log not found for assigned patient", 404)
        data = request.get_json(silent=True) or {}
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return json_error("override reason is required", 400)
        log.status = "Manual Override"
        log.remarks = reason.strip()
        if log.dose_event_id:
            event = db.session.get(DoseEvent, log.dose_event_id)
            if event and event.state in {"MISSED", "AWAITING_CONFIRMATION", "REMOVAL_DETECTED"}:
                event.state = "MANUALLY_CONFIRMED"
                event.verification_method = "caregiver_override"
                event.completed_at = utc_now()
        db.session.commit()
        return jsonify({"status": "success", "log": log.to_dict()})

    @app.post("/api/caregiver/notify")
    @role_required("Caregiver")
    def notify_patient():
        caregiver = current_user()
        data = request.get_json(silent=True) or {}
        patient = get_patient(data.get("patient_id"))
        message = data.get("message")
        if patient is None or patient.linked_caregiver_id != caregiver.id:
            return json_error("assigned patient not found", 404)
        if not isinstance(message, str) or not message.strip():
            return json_error("message is required", 400)
        notification = Notification(
            patient_id=patient.id,
            sender_id=caregiver.id,
            recipient_id=patient.id,
            message=message.strip(),
            created_at=utc_now(),
        )
        db.session.add(notification)
        db.session.commit()
        return jsonify({"status": "success", "message": "Notification dispatched"})

    @app.post("/api/alarm/acknowledge")
    @role_required("Doctor", "Patient", "Caregiver")
    def acknowledge_alarm():
        actor = current_user()
        data = request.get_json(silent=True) or {}
        patient = get_patient(data.get("patient_id"))
        reminder_id = data.get("reminder_id")
        reminder = db.session.get(Reminder, reminder_id) if isinstance(reminder_id, int) else None
        if patient is None or reminder is None or reminder.patient_id != patient.id:
            return json_error("scheduled reminder not found", 404)
        if not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient", 403)
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        now_utc = now.astimezone(timezone.utc)
        scheduled_at = event_schedule(patient, reminder, now_utc, window_minutes=5)
        if scheduled_at is None:
            return json_error("the medication reminder is not due", 409)
        event, _ = event_for_occurrence(patient, reminder, scheduled_at, "simulation")
        if event.state == "REMINDER_DUE":
            event.state = "ALERTING"
        if event.state in {"MISSED", "MANUALLY_CONFIRMED"}:
            return json_error("this scheduled dose is already closed", 409)
        if event.state == "IDLE":
            return json_error("the medication reminder is not due yet", 409)
        event.state = "AWAITING_CONFIRMATION"
        event.acknowledged_at = now_utc
        acknowledged = session.setdefault("acknowledged_alarms", [])
        key = alarm_key(patient.id, reminder.id, now)
        if key not in acknowledged:
            acknowledged.append(key)
        session.modified = True
        db.session.commit()
        return jsonify({
            "status": "success",
            "patient_id": patient.id,
            "reminder_id": reminder.id,
            "state": event.state,
            "event": event.to_dict(),
        })

    @app.post("/api/simulation/events/<int:event_id>/confirm")
    @role_required("Doctor", "Patient", "Caregiver")
    def confirm_simulation_event(event_id: int):
        actor = current_user()
        event = db.session.get(DoseEvent, event_id)
        patient = get_patient(event.patient_id) if event else None
        if event is None or patient is None or not can_access_patient(actor, patient):
            return json_error("dose event not found", 404)
        if event.state not in {"AWAITING_CONFIRMATION", "REMOVAL_DETECTED"}:
            return json_error("dose event is not awaiting confirmation", 409)
        event.source = event.source or "simulation"
        pill_log, _ = complete_dose_event(event, utc_now(), "manual_confirmation")
        db.session.commit()
        return jsonify({"status": "success", "event": event.to_dict(), "log": pill_log.to_dict()})

    @app.post("/api/simulation/scenario")
    @role_required("Doctor", "Patient", "Caregiver")
    def simulation_scenario():
        actor = current_user()
        data = request.get_json(silent=True) or {}
        patient = get_patient(data.get("patient_id"))
        reminder = active_reminder_for_simulation(
            data.get("patient_id"), data.get("reminder_id"), data.get("compartment")
        )
        scenario = data.get("scenario")
        if patient is None or reminder is None or not can_access_patient(actor, patient):
            return json_error("authorized patient and active schedule are required", 400)
        if not isinstance(scenario, str) or scenario not in {
            "normal_removal", "delayed_removal", "no_response", "no_removal",
            "noise", "unexpected_weight", "unexpected_increase", "excessive_removal",
        }:
            return json_error("unknown simulation scenario", 400)
        now = utc_now()
        simulated_at = now.replace(second=30, microsecond=0)
        event, _ = event_for_occurrence(patient, reminder, simulated_at, "simulation")
        sensor_analysis = {}
        if event.state in {"MISSED", "MANUALLY_CONFIRMED"}:
            return jsonify({"status": "success", "scenario": scenario, "event": event.to_dict()})
        event.acknowledged_at = now
        event.state = "ALERTING"
        expected = reminder.tablet_weight * reminder.expected_quantity
        if scenario == "no_response":
            event.due_at = now - timedelta(minutes=reminder.response_window_minutes + 1)
            expire_event(event, now)
        else:
            before = 100.0
            after = before - expected
            before_samples = [before, before + 0.03, before - 0.02]
            after_samples = [after, after + 0.02, after - 0.01]
            if scenario == "delayed_removal":
                event.due_at = now - timedelta(minutes=max(1, reminder.response_window_minutes - 1))
            if scenario == "no_removal":
                before, after = 100.0, 100.0
                before_samples = [100.0] * 20
                after_samples = [100.0] * 20
            elif scenario == "noise":
                before_samples = generate_sensor_noise(100.0, 24, noise_std=0.8, seed=91)
                after_samples = generate_sensor_noise(100.0, 24, noise_std=0.8, seed=92)
                before_samples = [value - median(before_samples) + 100.0 for value in before_samples]
                after_samples = [value - median(after_samples) + 100.0 for value in after_samples]
                before, after = 100.0, 100.0
            elif scenario in {"unexpected_weight", "unexpected_increase"}:
                before, after = 100.0, 100.0 - expected - (reminder.tolerance * 3)
                if scenario == "unexpected_increase":
                    after = 100.0 + expected
                before_samples = [before] * 20
                after_samples = [after] * 20
            elif scenario == "excessive_removal":
                before, after = 100.0, 100.0 - expected - reminder.tolerance * 3
                before_samples = [before] * 20
                after_samples = [after] * 20
            _, _, sensor_analysis = process_sensor_reading(
                patient, reminder, before, after, "simulation",
                before_samples,
                after_samples,
                now,
                dose_event=event,
            )
        db.session.commit()
        return jsonify({"status": "success", "scenario": scenario, "event": event.to_dict(),
                        "sensor_analysis": sensor_analysis})

    @app.post("/api/notify/alert")
    @role_required("Doctor", "Patient", "Caregiver")
    def send_alert():
        actor = current_user()
        data = request.get_json(silent=True) or {}
        patient = get_patient(data.get("patient_id"))
        message = data.get("message")
        if patient is None or not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient", 403)
        if not isinstance(message, str) or not message.strip():
            return json_error("message is required", 400)
        delivery = dispatch_alert(patient, message.strip())
        db.session.commit()
        return jsonify({"status": "success", "delivery": delivery})

    @app.get("/api/messages")
    @role_required("Doctor", "Patient", "Caregiver")
    def get_messages():
        actor = current_user()
        patient = get_patient(request.args.get("patient_id", type=int))
        if patient is None or not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient's messages", 403)
        messages = db.session.scalars(
            db.select(Message)
            .where(Message.patient_id == patient.id)
            .order_by(Message.created_at.asc())
            .limit(100)
        ).all()
        return jsonify({"messages": [message.to_dict() for message in messages]})

    @app.post("/api/messages")
    @role_required("Doctor", "Patient", "Caregiver")
    def create_message():
        actor = current_user()
        data = request.get_json(silent=True) or {}
        patient = get_patient(data.get("patient_id"))
        body = data.get("message")
        if patient is None or not can_access_patient(actor, patient):
            return json_error("You do not have access to this patient's messages", 403)
        if not isinstance(body, str) or not body.strip():
            return json_error("message is required", 400)
        if len(body.strip()) > 2000:
            return json_error("message must be 2000 characters or fewer", 400)
        message = Message(
            patient_id=patient.id,
            sender_id=actor.id,
            body=body.strip(),
            created_at=utc_now(),
        )
        db.session.add(message)
        db.session.commit()
        return jsonify({"status": "success", "message": message.to_dict()}), 201


app = create_app()


if __name__ == "__main__":
    app.run(threaded=True)

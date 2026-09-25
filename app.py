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
import smtplib
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from typing import Any, Callable, TypeVar

from flask import Flask, jsonify, render_template, request, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint, or_
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash
from model import predict_risk, risk_level, train_model


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)
F = TypeVar("F", bound=Callable[..., Any])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(db.String(120), nullable=False)
    email: Mapped[str] = mapped_column(db.String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(db.String(255), nullable=False)
    role: Mapped[str] = mapped_column(db.String(20), nullable=False)
    linked_doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    linked_caregiver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="patient", foreign_keys="Reminder.patient_id"
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
            "linked_doctor_id": self.linked_doctor_id,
            "linked_caregiver_id": self.linked_caregiver_id,
        }


class Reminder(db.Model):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint("status IN ('Active', 'Completed')", name="ck_reminder_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    med_name: Mapped[str] = mapped_column(db.String(150), nullable=False)
    time: Mapped[str] = mapped_column(db.String(5), nullable=False)
    dosage: Mapped[str] = mapped_column(db.String(100), nullable=False)
    set_by_role: Mapped[str] = mapped_column(db.String(20), nullable=False)
    status: Mapped[str] = mapped_column(db.String(20), nullable=False, default="Active")
    compartment: Mapped[int | None] = mapped_column(db.Integer)

    patient: Mapped[User] = relationship(back_populates="reminders", foreign_keys=[patient_id])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "med_name": self.med_name,
            "time": self.time,
            "dosage": self.dosage,
            "set_by_role": self.set_by_role,
            "status": self.status,
            "compartment": self.compartment,
        }


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
        }


class Notification(db.Model):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    message: Mapped[str] = mapped_column(db.String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False)


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
    with app.app_context():
        db.create_all()
        migrate_schema()
        train_model()

    register_routes(app)
    register_csrf(app)
    return app


def migrate_schema() -> None:
    """Apply additive SQLite migrations while retaining all existing records."""
    db.session.execute(db.text(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    ))
    if db.session.scalar(db.text("SELECT COUNT(*) FROM schema_version")) == 0:
        db.session.execute(db.text("INSERT INTO schema_version(version) VALUES (0)"))
    for table, column, definition in (
        ("reminders", "compartment", "INTEGER"),
    ):
        columns = {
            row[1] for row in db.session.execute(db.text(f"PRAGMA table_info({table})")).all()
        }
        if column not in columns:
            db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
    db.session.execute(db.text("UPDATE schema_version SET version = 1"))
    db.session.commit()


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
        if request.headers.get("X-Device-Key"):
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
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role}


def get_patient(patient_id: Any) -> User | None:
    return db.session.get(User, patient_id) if isinstance(patient_id, int) else None


def risk_payload(patient_id: int) -> dict[str, Any]:
    score = predict_risk(patient_id)
    return {"risk_score": score, "level": risk_level(score)}


def can_access_patient(user: User, patient: User) -> bool:
    return (
        user.id == patient.id
        or user.role == "Doctor"
        and patient.linked_doctor_id == user.id
        or user.role == "Caregiver"
        and patient.linked_caregiver_id == user.id
    )


def dispatch_alert(patient: User, message: str) -> str:
    """Store an alert and deliver it through TextBee or configured SMTP."""
    sender_id = patient.linked_caregiver_id or patient.id
    db.session.add(
        Notification(
            patient_id=patient.id,
            sender_id=sender_id,
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
        with urllib.request.urlopen(payload, timeout=10):
            return "textbee"

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
    db.session.add(
        HardwareTraffic(
            endpoint=endpoint,
            method=method,
            payload=json.dumps(payload, separators=(",", ":")),
            response_status=response_status,
            created_at=utc_now(),
        )
    )


def create_pill_log(patient_id: int, w_before: float, w_after: float) -> tuple[PillLog, str | None]:
    delta_weight = w_before - w_after
    status = "Taken" if delta_weight >= 2.0 else "Missed"
    pill_log = PillLog(
        patient_id=patient_id,
        timestamp=utc_now(),
        initial_weight=w_before,
        final_weight=w_after,
        delta_weight=delta_weight,
        status=status,
    )
    db.session.add(pill_log)
    db.session.flush()
    alert_delivery = None
    if status == "Missed":
        patient = db.session.get(User, patient_id)
        alert_delivery = dispatch_alert(
            patient,
            "Missed medication detected. Please check the patient's pill box.",
        )
    return pill_log, alert_delivery


def alarm_key(patient_id: int, reminder_id: int, when: datetime) -> str:
    return f"{when.date().isoformat()}:{patient_id}:{reminder_id}:{when.hour:02d}:{when.minute:02d}"


def scheduled_alarm_is_acknowledged(patient_id: int) -> bool:
    return any(
        key.startswith(f"{datetime.now().date().isoformat()}:{patient_id}:")
        for key in session.get("acknowledged_alarms", [])
    )


def register_routes(app: Flask) -> None:
    @app.get("/")
    def index():
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
        if float(w_after) > float(w_before):
            return json_error("w_after cannot exceed w_before", 400)
        if db.session.get(User, patient_id) is None or device_patient_user.role != "Patient":
            return json_error("patient not found", 404)

        pill_log, alert_delivery = create_pill_log(
            patient_id, float(w_before), float(w_after)
        )
        status = pill_log.status
        record_hardware_traffic(request.path, request.method, data, 201)
        db.session.commit()
        return jsonify(
            {
                "status": "success",
                "event": status,
                "trigger_buzzer": status == "Missed",
                "alert_delivery": alert_delivery,
                "log": pill_log.to_dict(),
            }
        ), 201

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
        if not scheduled_alarm_is_acknowledged(patient.id):
            return json_error("A scheduled medication alarm must be acknowledged before simulation", 409)
        if not all(valid_weight(value) for value in (w_before, w_after)):
            return json_error("weights must be finite numbers from 0 to 100000", 400)
        if float(w_after) > float(w_before):
            return json_error("w_after cannot exceed w_before", 400)
        pill_log, alert_delivery = create_pill_log(
            patient.id, float(w_before), float(w_after)
        )
        record_hardware_traffic("/api/simulation/log-event", "POST", data, 201)
        db.session.commit()
        return jsonify({
            "status": "success",
            "event": pill_log.status,
            "delta_weight": pill_log.delta_weight,
            "risk": risk_payload(patient.id),
            "trigger_buzzer": pill_log.status == "Missed",
            "alert_delivery": alert_delivery,
            "log": pill_log.to_dict(),
        }), 201

    @app.get("/api/hardware/traffic")
    @role_required("Doctor", "Caregiver")
    def hardware_traffic():
        entries = db.session.scalars(
            db.select(HardwareTraffic)
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

    @app.get("/api/aiml/risk/<int:patient_id>")
    @role_required("Doctor", "Patient", "Caregiver")
    def adherence_risk(patient_id: int):
        viewer = current_user()
        patient = get_patient(patient_id)
        allowed = patient is not None and can_access_patient(viewer, patient)
        if not allowed:
            return json_error("You do not have access to this patient's risk", 403)
        return jsonify({"patient_id": patient_id, **risk_payload(patient_id)})

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

    @app.post("/api/doctor/patients/<int:patient_id>/schedules")
    @role_required("Doctor", "Caregiver")
    def create_schedule(patient_id: int):
        actor = current_user()
        patient = get_patient(patient_id)
        data = request.get_json(silent=True) or {}
        med_name, reminder_time, dosage = data.get("med_name"), data.get("time"), data.get("dosage")
        compartment = data.get("compartment")
        if isinstance(compartment, str) and compartment.strip():
            try:
                compartment = int(compartment)
            except ValueError:
                compartment = None
        can_edit = patient and (
            (actor.role == "Doctor" and patient.linked_doctor_id == actor.id)
            or (actor.role == "Caregiver" and patient.linked_caregiver_id == actor.id)
        )
        if not can_edit:
            return json_error("patient is not assigned to you", 403)
        if not all(isinstance(value, str) and value.strip() for value in (med_name, reminder_time, dosage)):
            return json_error("med_name, time, and dosage are required", 400)
        if len(med_name.strip()) > 150 or len(dosage.strip()) > 100 or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", reminder_time.strip()):
            return json_error("med_name, dosage, or time is invalid", 400)
        if compartment is not None and (
            not isinstance(compartment, int) or isinstance(compartment, bool) or not 1 <= compartment <= 8
        ):
            return json_error("compartment must be an integer from 1 to 8", 400)
        reminder = Reminder(
            patient_id=patient.id,
            med_name=med_name.strip(),
            time=reminder_time.strip(),
            dosage=dosage.strip(),
            set_by_role=actor.role,
            compartment=compartment,
        )
        db.session.add(reminder)
        db.session.commit()
        return jsonify({"status": "success", "schedule": reminder.to_dict()}), 201

    @app.get("/api/patient/dashboard")
    @role_required("Patient")
    def patient_dashboard():
        patient = current_user()
        logs = db.session.scalars(
            db.select(PillLog).where(PillLog.patient_id == patient.id).order_by(PillLog.timestamp.desc()).limit(10)
        ).all()
        return jsonify(
            {
                "doctor": user_summary(patient.doctor) if patient.doctor else None,
                "caregiver": user_summary(patient.caregiver) if patient.caregiver else None,
                "schedules": [r.to_dict() for r in patient.reminders if r.status == "Active"],
                "logs": [log.to_dict() for log in logs],
            }
        )

    @app.get("/api/caregiver/dashboard")
    @role_required("Caregiver")
    def caregiver_dashboard():
        caregiver = current_user()
        patients = db.session.scalars(
            db.select(User).where(User.linked_caregiver_id == caregiver.id).order_by(User.name)
        ).all()
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
        now = datetime.now()
        if reminder.time != now.strftime("%H:%M"):
            return json_error("The medication alarm is not active yet", 409)
        acknowledged = session.setdefault("acknowledged_alarms", [])
        key = alarm_key(patient.id, reminder.id, now)
        if key not in acknowledged:
            acknowledged.append(key)
        session.modified = True
        return jsonify({"status": "success", "patient_id": patient.id, "reminder_id": reminder.id})

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
    app.run()

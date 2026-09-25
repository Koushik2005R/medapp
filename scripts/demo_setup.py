"""Create a repeatable, hardware-free demonstration database.

Usage:
    $env:SECRET_KEY = "demo-only-secret"
    python scripts/demo_setup.py --reset
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Smart Pill Box demo data")
    parser.add_argument("--database", default="instance/demo.db", help="SQLite file for demo data")
    parser.add_argument("--reset", action="store_true", help="replace only the selected demo database")
    args = parser.parse_args()
    if not os.environ.get("SECRET_KEY"):
        raise SystemExit("SECRET_KEY is required. Set it before running this script.")

    database = Path(args.database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    if args.reset and database.exists():
        database.unlink()
    os.environ["DATABASE_URL"] = f"sqlite:///{database}"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from app import Device, Message, Notification, PillLog, Reminder, User, app, db, token_digest, utc_now
    from model import train_model

    with app.app_context():
        db.create_all()
        doctor = _user(User, "Demo Doctor", "doctor.demo@example.com", "Doctor")
        patient = _user(User, "Demo Patient", "patient.demo@example.com", "Patient")
        caregiver = _user(User, "Demo Caregiver", "caregiver.demo@example.com", "Caregiver")
        patient.linked_doctor_id = doctor.id
        patient.linked_caregiver_id = caregiver.id

        reminder = db.session.scalar(db.select(Reminder).where(
            Reminder.patient_id == patient.id, Reminder.med_name == "Demo Vitamin"
        ))
        if reminder is None:
            reminder = Reminder(
                patient_id=patient.id, med_name="Demo Vitamin", time="08:00",
                dosage="1 tablet", set_by_role="Doctor", compartment=1,
                tablet_weight=0.5, expected_quantity=1, tolerance=0.2,
                calibration_offset=0.0, response_window_minutes=30,
                noise_threshold=0.15,
            )
            db.session.add(reminder)
            db.session.flush()

        device_key = "demo-device-key-123"
        device = db.session.scalar(db.select(Device).where(Device.patient_id == patient.id))
        if device is None:
            db.session.add(Device(
                patient_id=patient.id, name="Demo simulated ESP32",
                token_hash=token_digest(device_key), created_at=utc_now(),
            ))

        now = utc_now()
        existing_logs = db.session.scalar(db.select(PillLog.id).where(PillLog.patient_id == patient.id))
        if existing_logs is None:
            for days_ago, status in ((5, "Taken"), (4, "Taken"), (3, "Missed"), (2, "Taken"), (1, "Taken")):
                timestamp = now - timedelta(days=days_ago)
                db.session.add(PillLog(
                    patient_id=patient.id, reminder_id=reminder.id, timestamp=timestamp,
                    initial_weight=100.0, final_weight=99.5 if status == "Taken" else 100.0,
                    delta_weight=0.5 if status == "Taken" else 0.0, status=status,
                    remarks="Synthetic demonstration record", source="demo",
                    verification_method="simulated_sensor" if status == "Taken" else "response_window_expired",
                    compartment=1,
                ))
        if db.session.scalar(db.select(Notification.id).where(Notification.patient_id == patient.id)) is None:
            db.session.add(Notification(
                patient_id=patient.id, sender_id=caregiver.id,
                message="Synthetic demo alert: a response window expired.",
                created_at=now - timedelta(days=3),
            ))
        if db.session.scalar(db.select(Message.id).where(Message.patient_id == patient.id)) is None:
            db.session.add(Message(
                patient_id=patient.id, sender_id=doctor.id,
                body="This is a synthetic demonstration message.",
                created_at=now,
            ))
        db.session.commit()
        train_model()

        print(f"Demo database: {database}")
        print("Demo accounts (password for all: DemoPass123!):")
        print("  Doctor:    doctor.demo@example.com")
        print("  Patient:   patient.demo@example.com")
        print("  Caregiver: caregiver.demo@example.com")
        print(f"  Device key: {device_key}")
        print(f"  Patient ID: {patient.id}; reminder ID: {reminder.id}")
        print("Synthetic records: schedules, taken/missed events, notification, message, and ML artifacts.")


def _user(user_cls, name: str, email: str, role: str):
    from app import db

    user = db.session.scalar(db.select(user_cls).where(user_cls.email == email))
    if user is None:
        user = user_cls(name=name, email=email, role=role, password_hash="")
        user.set_password("DemoPass123!")
        db.session.add(user)
        db.session.flush()
    return user


if __name__ == "__main__":
    main()

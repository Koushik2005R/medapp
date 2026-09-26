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


DEMO_PASSWORD = "DemoPass123!"
DEVICE_KEY = "demo-device-key-123"
LOG_PATTERN = ("Taken", "Taken", "Missed", "Taken", "Manual Override", "Taken", "Missed")


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
    os.environ["ADHERENCE_MODEL_PATH"] = str(database.with_name(f"{database.stem}.adherence-model.joblib"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from app import (
        Device,
        Medication,
        MedicationChangeRequest,
        MedicationAudit,
        Message,
        Notification,
        PillLog,
        Reminder,
        User,
        app,
        db,
        token_digest,
        utc_now,
    )
    from model import train_model

    with app.app_context():
        db.create_all()
        doctor = _user(User, "Demo Doctor", "doctor.demo@example.com", "Doctor")
        patient = _user(User, "Demo Patient", "patient.demo@example.com", "Patient")
        caregiver = _user(User, "Demo Caregiver", "caregiver.demo@example.com", "Caregiver")
        patient.linked_doctor_id = doctor.id
        patient.linked_caregiver_id = caregiver.id

        medications = []
        reminders = []
        for name, time, compartment in (
            ("Demo Vitamin", "08:00", 1),
            ("Demo Evening Medication", "20:00", 2),
        ):
            medication = db.session.scalar(db.select(Medication).where(
                Medication.patient_id == patient.id, Medication.name == name
            ))
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
            medication.status = "Active"
            medications.append(medication)

            reminder = db.session.scalar(db.select(Reminder).where(
                Reminder.patient_id == patient.id,
                db.or_(
                    Reminder.medication_id == medication.id,
                    db.and_(
                        Reminder.medication_id.is_(None),
                        Reminder.med_name == name,
                    ),
                ),
            ).order_by(Reminder.id).limit(1))
            if reminder is None:
                reminder = Reminder(
                    patient_id=patient.id,
                    medication_id=medication.id,
                    med_name=name,
                    time=time,
                    dosage="1 tablet",
                    set_by_role="Doctor",
                    compartment=compartment,
                    tablet_weight=0.5,
                    expected_quantity=1,
                    tolerance=0.2,
                    calibration_offset=0.0,
                    response_window_minutes=30,
                    noise_threshold=0.15,
                )
                db.session.add(reminder)
            else:
                reminder.medication = medication
                reminder.med_name = name
                reminder.time = time
                reminder.dosage = "1 tablet"
                reminder.status = "Active"
            reminders.append(reminder)
        db.session.flush()

        device = db.session.scalar(db.select(Device).where(Device.patient_id == patient.id))
        if device is None:
            db.session.add(Device(
                patient_id=patient.id,
                name="Demo simulated ESP32",
                token_hash=token_digest(DEVICE_KEY),
                created_at=utc_now(),
            ))
        else:
            device.active = True
            device.token_hash = token_digest(DEVICE_KEY)

        now = utc_now()
        for days_ago, status in enumerate(LOG_PATTERN, start=1):
            remark = f"Synthetic demo event {days_ago}"
            log = db.session.scalar(db.select(PillLog).where(
                PillLog.patient_id == patient.id, PillLog.remarks == remark
            ))
            timestamp = now - timedelta(days=days_ago)
            taken = status != "Missed"
            if log is None:
                log = PillLog(
                    patient_id=patient.id,
                    timestamp=timestamp,
                    initial_weight=100.0,
                    final_weight=99.5 if taken else 100.0,
                    delta_weight=0.5 if taken else 0.0,
                    status=status,
                    remarks=remark,
                    reminder_id=reminders[0].id,
                    compartment=1,
                    source="demo",
                    verification_method="simulated_sensor" if taken else "response_window_expired",
                )
                db.session.add(log)
            else:
                log.timestamp = timestamp
                log.status = status

        request_ids = []
        for index, medication in enumerate(medications):
            request = db.session.scalar(
                db.select(MedicationChangeRequest)
                .where(
                    MedicationChangeRequest.patient_id == patient.id,
                    MedicationChangeRequest.medication_id == medication.id,
                    MedicationChangeRequest.operation == "REMOVE",
                )
                .order_by(MedicationChangeRequest.id.desc())
                .limit(1)
            )
            if request is None:
                request = MedicationChangeRequest(
                    patient_id=patient.id,
                    medication_id=medication.id,
                    requested_by_id=caregiver.id,
                    operation="REMOVE",
                    requested_data={},
                    pending_key=f"{patient.id}:REMOVE:{medication.id}",
                )
                db.session.add(request)
                db.session.flush()
            request.status = "PENDING"
            request.decision_reason = None
            request.decided_by_id = None
            request.decided_at = None
            request.created_at = now - timedelta(minutes=index + 1)
            request_ids.append(request.id)
            if not db.session.scalar(db.select(Notification.id).where(
                Notification.recipient_id == doctor.id,
                Notification.message == f"Synthetic demo request {index + 1}.",
            )):
                db.session.add(Notification(
                    patient_id=patient.id,
                    recipient_id=doctor.id,
                    sender_id=caregiver.id,
                    message=f"Synthetic demo request {index + 1}.",
                    created_at=now - timedelta(minutes=index + 1),
                ))

        for index, (sender, recipient, body) in enumerate((
            (doctor, patient, "Welcome to the synthetic PillGuard demonstration."),
            (caregiver, doctor, "The demo missed-dose history and sensor scenarios are ready."),
        )):
            if not db.session.scalar(db.select(Message.id).where(
                Message.patient_id == patient.id, Message.body == body
            )):
                db.session.add(Message(
                    patient_id=patient.id,
                    sender_id=sender.id,
                    body=body,
                    created_at=now - timedelta(minutes=index),
                ))

        if not db.session.scalar(db.select(Notification.id).where(
            Notification.patient_id == patient.id,
            Notification.message == "Synthetic demo missed-dose alert.",
        )):
            db.session.add(Notification(
                patient_id=patient.id,
                recipient_id=patient.id,
                sender_id=caregiver.id,
                message="Synthetic demo missed-dose alert.",
                created_at=now - timedelta(days=2),
            ))

        for medication in medications:
            if not db.session.scalar(db.select(MedicationAudit.id).where(
                MedicationAudit.patient_id == patient.id,
                MedicationAudit.medication_id == medication.id,
                MedicationAudit.action == "DEMO_SEEDED",
            )):
                db.session.add(MedicationAudit(
                    patient_id=patient.id,
                    medication_id=medication.id,
                    actor_id=doctor.id,
                    action="DEMO_SEEDED",
                    after_data={
                        "name": medication.name,
                        "status": medication.status,
                        "demo_only": True,
                    },
                    reason="Synthetic demo fixture",
                    created_at=now,
                ))

        db.session.commit()
        train_model()

        print(f"Demo database: {database}")
        print(f"Demo accounts (password for all: {DEMO_PASSWORD}):")
        print("  Doctor:    doctor.demo@example.com")
        print("  Patient:   patient.demo@example.com")
        print("  Caregiver: caregiver.demo@example.com")
        print(f"  Device key: {DEVICE_KEY}")
        print(f"  Patient ID: {patient.id}; reminder IDs: {', '.join(str(item.id) for item in reminders)}")
        print(f"  Pending removal request IDs: {', '.join(str(item) for item in request_ids)}")
        print("Synthetic records: linked roles, schedules, 7 dose events, two pending removal requests, notifications, messages, and prediction history.")
        print(f"Adherence model artifact (isolated to this demo DB): {os.environ['ADHERENCE_MODEL_PATH']}")


def _user(user_cls, name: str, email: str, role: str):
    from app import db

    user = db.session.scalar(db.select(user_cls).where(user_cls.email == email))
    if user is None:
        user = user_cls(name=name, email=email, role=role, password_hash="")
        db.session.add(user)
        db.session.flush()
    elif user.role != role:
        raise RuntimeError(f"demo account {email} already exists with role {user.role}")
    user.name = name
    user.set_password(DEMO_PASSWORD)
    return user


if __name__ == "__main__":
    main()

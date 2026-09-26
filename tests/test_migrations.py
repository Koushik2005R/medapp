import os
import sqlite3

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import Medication, MedicationAudit, PillLog, Reminder, User, create_app, db, utc_now


def create_legacy_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL,
                email VARCHAR(255) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) NOT NULL, linked_doctor_id INTEGER,
                linked_caregiver_id INTEGER
            );
            CREATE TABLE reminders (
                id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL,
                med_name VARCHAR(150) NOT NULL, time VARCHAR(5) NOT NULL,
                dosage VARCHAR(100) NOT NULL, set_by_role VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL, compartment INTEGER
            );
            CREATE TABLE pill_logs (
                id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL,
                timestamp DATETIME NOT NULL, initial_weight FLOAT NOT NULL,
                final_weight FLOAT NOT NULL, delta_weight FLOAT NOT NULL,
                status VARCHAR(20) NOT NULL, remarks VARCHAR(500)
            );
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL, message VARCHAR(500) NOT NULL,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL, body VARCHAR(2000) NOT NULL,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE hardware_traffic (
                id INTEGER PRIMARY KEY, endpoint VARCHAR(120) NOT NULL,
                method VARCHAR(10) NOT NULL, payload TEXT NOT NULL,
                response_status INTEGER NOT NULL, created_at DATETIME NOT NULL
            );
            INSERT INTO users VALUES (1, 'Legacy patient', 'legacy@example.com', 'hash', 'Patient', NULL, NULL);
            INSERT INTO reminders VALUES (7, 1, 'Legacy medicine', '08:00', '1 tablet', 'Doctor', 'Active', 2);
            INSERT INTO pill_logs VALUES (11, 1, '2025-01-02 08:00:00', 100, 99.5, 0.5, 'Taken', 'legacy record');
            INSERT INTO hardware_traffic VALUES (3, '/api/hardware/log-event', 'POST', '{\"patient_id\":1,\"w_before\":100,\"device_key\":\"secret\"}', 200, '2025-01-02 08:00:00');
        """)


def test_upgrade_preserves_legacy_rows_and_backfills_normalized_models(tmp_path):
    database = tmp_path / "legacy.db"
    create_legacy_database(database)
    app = create_app({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database}",
        "SECRET_KEY": "test-secret-key",
    })

    result = app.test_cli_runner().invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output

    with app.app_context():
        patient = db.session.get(User, 1)
        schedule = db.session.get(Reminder, 7)
        legacy_log = db.session.get(PillLog, 11)
        medication = db.session.get(Medication, schedule.medication_id)
        payload = db.session.scalar(db.text("SELECT payload FROM hardware_traffic WHERE id = 3"))
        traffic_patient_id = db.session.scalar(
            db.text("SELECT patient_id FROM hardware_traffic WHERE id = 3")
        )
        assert patient.timezone == "UTC"
        assert schedule.med_name == "Legacy medicine"
        assert schedule.medication_id is not None
        assert medication.name == "Legacy medicine"
        assert legacy_log.remarks == "legacy record"
        assert payload == '{"patient_id":1,"w_before":100}'
        assert traffic_patient_id == patient.id
        assert db.session.scalar(db.text("SELECT version_num FROM alembic_version")) == "9e09a6d778d7"
        assert db.session.scalar(db.text(
            "SELECT recipient_id FROM notifications WHERE id = 1"
        )) is None
        foreign_keys = inspect(db.engine).get_foreign_keys("hardware_traffic")
        assert any(
            key["constrained_columns"] == ["patient_id"]
            and key["referred_table"] == "users"
            for key in foreign_keys
        )
        db.session.add(MedicationAudit(
            patient_id=patient.id,
            medication_id=medication.id,
            actor_id=patient.id,
            action="TEST",
            created_at=utc_now(),
        ))
        db.session.commit()
        with pytest.raises(IntegrityError, match="immutable"):
            db.session.execute(db.text("UPDATE medication_audit SET action = 'CHANGED'"))
        db.session.rollback()


def test_upgrade_creates_current_schema_from_empty_database(tmp_path):
    database = tmp_path / "empty.db"
    app = create_app({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database}",
        "SECRET_KEY": "test-secret-key",
    })

    result = app.test_cli_runner().invoke(args=["db", "upgrade"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        tables = set(inspect(db.engine).get_table_names())
        assert {
            "medications",
            "reminders",
            "dose_events",
            "sensor_events",
            "medication_change_requests",
            "medication_audit",
            "notifications",
        } <= tables

import os

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import (
    Device,
    Medication,
    MedicationAudit,
    MedicationChangeRequest,
    Notification,
    Reminder,
    User,
    create_app,
    db,
    utc_now,
)


def setup_app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'medications.db'}",
        "SECRET_KEY": "test-secret-key",
    })
    with app.app_context():
        doctor = User(name="Doctor", email="doctor@example.com", role="Doctor", password_hash="")
        doctor.set_password("password123")
        second_doctor = User(name="Other Doctor", email="other-doctor@example.com", role="Doctor", password_hash="")
        second_doctor.set_password("password123")
        patient = User(name="Patient", email="patient@example.com", role="Patient", password_hash="")
        patient.set_password("password123")
        caregiver = User(name="Caregiver", email="caregiver@example.com", role="Caregiver", password_hash="")
        caregiver.set_password("password123")
        other = User(name="Other Patient", email="other@example.com", role="Patient", password_hash="")
        other.set_password("password123")
        db.session.add_all([doctor, second_doctor, patient, caregiver, other])
        db.session.flush()
        patient.linked_doctor_id = doctor.id
        patient.linked_caregiver_id = caregiver.id
        medication = Medication(
            patient_id=patient.id, name="Existing", status="Active",
            created_at=utc_now(), updated_at=utc_now(),
        )
        db.session.add(medication)
        db.session.flush()
        schedule = Reminder(
            patient_id=patient.id, medication_id=medication.id, medication=medication,
            med_name=medication.name, time="08:00", dosage="1 tablet",
            set_by_role="Doctor", compartment=1,
        )
        db.session.add(schedule)
        db.session.commit()
        ids = {
            "doctor": doctor.id,
            "second_doctor": second_doctor.id,
            "patient": patient.id,
            "caregiver": caregiver.id,
            "other": other.id,
            "medication": medication.id,
            "schedule": schedule.id,
        }
    return app, ids


def login(client, email):
    response = client.post("/api/login", json={"email": email, "password": "password123"})
    assert response.status_code == 200
    token = client.get("/api/csrf-token").get_json()["csrf_token"]
    return {"X-CSRF-Token": token}


def test_patient_and_caregiver_can_request_changes_but_only_assigned_doctor_can_decide(tmp_path):
    app, ids = setup_app(tmp_path)
    patient = app.test_client()
    patient_headers = login(patient, "patient@example.com")
    request_body = {
        "operation": "ADD",
        "requested_data": {"name": "New medicine", "dosage": "2 tablets", "time": "09:30"},
    }
    submitted = patient.post(
        f"/api/patients/{ids['patient']}/medication-requests",
        json=request_body,
        headers=patient_headers,
    )
    assert submitted.status_code == 201
    assert patient.get("/api/notifications").get_json()["notifications"] == []
    request_id = submitted.get_json()["request"]["id"]
    duplicate = patient.post(
        f"/api/patients/{ids['patient']}/medication-requests",
        json=request_body,
        headers=patient_headers,
    )
    assert duplicate.status_code == 409
    assert patient.get(f"/api/patients/{ids['patient']}/medications").status_code == 200
    assert patient.get(f"/api/patients/{ids['other']}/medications").status_code == 403

    outsider = app.test_client()
    outsider_headers = login(outsider, "other-doctor@example.com")
    assert outsider.get("/api/doctor/medication-requests").get_json()["requests"] == []
    denied = outsider.patch(
        f"/api/doctor/medication-requests/{request_id}",
        json={"decision": "APPROVED"},
        headers=outsider_headers,
    )
    assert denied.status_code == 404

    doctor = app.test_client()
    doctor_headers = login(doctor, "doctor@example.com")
    doctor_notifications = doctor.get("/api/notifications").get_json()["notifications"]
    assert doctor_notifications[0]["recipient_id"] == ids["doctor"]
    assert "requested to add" in doctor_notifications[0]["message"]
    pending = doctor.get("/api/doctor/medication-requests")
    assert pending.status_code == 200
    assert pending.get_json()["requests"][0]["status"] == "PENDING"
    approved = doctor.patch(
        f"/api/doctor/medication-requests/{request_id}",
        json={"decision": "APPROVED", "reason": "Added to the treatment plan."},
        headers=doctor_headers,
    )
    assert approved.status_code == 200
    assert approved.get_json()["request"]["status"] == "APPROVED"
    assert patient.get(f"/api/patients/{ids['patient']}/medication-requests").get_json()["requests"][0]["decision_reason"] == "Added to the treatment plan."
    assert "was approved" in patient.get("/api/notifications").get_json()["notifications"][0]["message"]
    records = patient.get(f"/api/patients/{ids['patient']}/medications").get_json()["medications"]
    assert any(record["name"] == "New medicine" and record["status"] == "Active" for record in records)
    assert doctor.patch(
        f"/api/doctor/medication-requests/{request_id}",
        json={"decision": "REJECTED", "reason": "duplicate"},
        headers=doctor_headers,
    ).status_code == 409
    with app.app_context():
        assert db.session.scalar(
            db.select(db.func.count()).select_from(Notification).where(
                Notification.patient_id == ids["patient"]
            )
        ) == 2
        audit = db.session.scalar(db.select(MedicationAudit).where(
            MedicationAudit.request_id == request_id
        ))
        assert audit.action == "ADD_APPROVED"
        assert audit.after_data["name"] == "New medicine"
        db.session.delete(audit)
        with pytest.raises(ValueError, match="cannot be deleted"):
            db.session.flush()
        db.session.rollback()


def test_edit_remove_rejection_archive_and_audit_are_history_preserving(tmp_path):
    app, ids = setup_app(tmp_path)
    caregiver = app.test_client()
    caregiver_headers = login(caregiver, "caregiver@example.com")

    edit = caregiver.post(
        f"/api/patients/{ids['patient']}/medication-requests",
        json={
            "operation": "EDIT",
            "medication_id": ids["medication"],
            "requested_data": {"name": "Updated", "dosage": "2 tablets"},
        },
        headers=caregiver_headers,
    )
    assert edit.status_code == 201
    doctor = app.test_client()
    doctor_headers = login(doctor, "doctor@example.com")
    assert doctor.patch(
        f"/api/doctor/medication-requests/{edit.get_json()['request']['id']}",
        json={"decision": "APPROVED"},
        headers=doctor_headers,
    ).status_code == 200

    removal = caregiver.post(
        f"/api/patients/{ids['patient']}/medication-requests",
        json={"operation": "REMOVE", "medication_id": ids["medication"]},
        headers=caregiver_headers,
    )
    assert removal.status_code == 201
    missing_reason = doctor.patch(
        f"/api/doctor/medication-requests/{removal.get_json()['request']['id']}",
        json={"decision": "REJECTED"},
        headers=doctor_headers,
    )
    assert missing_reason.status_code == 400
    rejected = doctor.patch(
        f"/api/doctor/medication-requests/{removal.get_json()['request']['id']}",
        json={"decision": "REJECTED", "reason": "Keep taking it until reviewed at the next visit."},
        headers=doctor_headers,
    )
    assert rejected.status_code == 200
    assert rejected.get_json()["request"]["decision_reason"]

    request_direct = caregiver.post(
        f"/api/doctor/patients/{ids['patient']}/schedules",
        json={"med_name": "Blocked", "time": "10:00", "dosage": "1 tablet"},
        headers=caregiver_headers,
    )
    assert request_direct.status_code == 403

    archived = doctor.patch(
        f"/api/doctor/medications/{ids['medication']}",
        json={"status": "Archived"},
        headers=doctor_headers,
    )
    assert archived.status_code == 200
    assert archived.get_json()["medication"]["status"] == "Archived"
    with app.app_context():
        medication = db.session.get(Medication, ids["medication"])
        schedule = db.session.get(Reminder, ids["schedule"])
        assert medication.name == "Updated"
        assert schedule.status == "Completed"
        assert db.session.scalar(db.select(MedicationAudit.id).where(
            MedicationAudit.medication_id == ids["medication"]
        )) is not None
    medication_list = caregiver.get(f"/api/patients/{ids['patient']}/medications").get_json()["medications"]
    assert any(item["id"] == ids["medication"] and item["status"] == "Archived" for item in medication_list)


def test_direct_medication_changes_require_assigned_doctor_and_are_audited(tmp_path):
    app, ids = setup_app(tmp_path)
    patient = app.test_client()
    patient_headers = login(patient, "patient@example.com")
    denied = patient.post(
        f"/api/doctor/patients/{ids['patient']}/medications",
        json={"name": "Unauthorized", "dosage": "1 tablet", "time": "09:00"},
        headers=patient_headers,
    )
    assert denied.status_code == 403

    doctor = app.test_client()
    doctor_headers = login(doctor, "doctor@example.com")
    created = doctor.post(
        f"/api/doctor/patients/{ids['patient']}/medications",
        json={"name": "Direct", "dosage": "1 tablet", "time": "09:00"},
        headers=doctor_headers,
    )
    assert created.status_code == 201
    assert doctor.patch(
        f"/api/doctor/medications/{created.get_json()['medication']['id']}",
        json={"name": "Renamed", "time": "10:00"},
        headers=doctor_headers,
    ).status_code == 200
    audit = patient.get(f"/api/patients/{ids['patient']}/medication-audit")
    assert audit.status_code == 200
    assert {item["action"] for item in audit.get_json()["audit"]} >= {"CREATED", "UPDATED"}

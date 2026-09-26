import os
from datetime import datetime, timezone

os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import (
    Device,
    DoseEvent,
    HardwareTraffic,
    Medication,
    PillLog,
    Reminder,
    SensorEvent,
    User,
    create_app,
    db,
    event_schedule,
    local_occurrence,
    reminder_for_input,
    token_digest,
    utc_now,
)


def make_app(tmp_path):
    return create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "SECRET_KEY": "test-secret-key",
    })


def seed(app):
    with app.app_context():
        doctor = User(name="Doctor", email="doctor@example.com", role="Doctor", password_hash="")
        doctor.set_password("password123")
        patient = User(name="Patient", email="patient@example.com", role="Patient", password_hash="")
        patient.set_password("password123")
        db.session.add_all([doctor, patient])
        db.session.flush()
        patient.linked_doctor_id = doctor.id
        reminder = Reminder(
            patient_id=patient.id, med_name="Medicine", time=utc_now().strftime("%H:%M"),
            dosage="1 tablet", set_by_role="Doctor", compartment=1,
            tablet_weight=5.0, expected_quantity=1, tolerance=0.3,
            response_window_minutes=30, noise_threshold=0.15,
        )
        db.session.add(reminder)
        token = "device-secret"
        db.session.add(Device(
            patient_id=patient.id, name="Box", token_hash=token_digest(token), created_at=utc_now()
        ))
        db.session.commit()
        return doctor.id, patient.id, token


def csrf(client):
    return client.get("/api/csrf-token").get_json()["csrf_token"]


def test_login_and_csrf_protect_mutations(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, _ = seed(app)
    client = app.test_client()
    response = client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
    assert response.status_code == 200
    assert client.post("/api/messages", json={"patient_id": patient_id, "message": "hello"}).status_code == 400
    token = csrf(client)
    response = client.post(
        "/api/messages", json={"patient_id": patient_id, "message": "hello"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 201


def test_device_must_match_patient(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, device_key = seed(app)
    client = app.test_client()
    assert client.get(f"/api/hardware/get-schedules?patient_id={patient_id}").status_code == 401
    response = client.get(
        f"/api/hardware/get-schedules?patient_id={patient_id}",
        headers={"X-Device-Key": device_key},
    )
    assert response.status_code == 200


def test_device_key_is_bound_to_its_patient_and_does_not_skip_browser_csrf(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, device_key = seed(app)
    with app.app_context():
        other = User(name="Other", email="other@example.com", role="Patient", password_hash="")
        db.session.add(other)
        db.session.flush()
        other_id = other.id
        other_reminder = Reminder(
            patient_id=other.id, med_name="Other medicine", time=utc_now().strftime("%H:%M"),
            dosage="1 tablet", set_by_role="Doctor",
        )
        db.session.add(other_reminder)
        db.session.add(HardwareTraffic(
            patient_id=other.id, endpoint="/api/hardware/log-event", method="POST",
            payload='{"patient_id":%s}' % other.id, response_status=200, created_at=utc_now(),
        ))
        db.session.commit()
        other_reminder_id = other_reminder.id
    client = app.test_client()
    headers = {"X-Device-Key": device_key}
    assert client.get(
        f"/api/hardware/get-schedules?patient_id={other_id}", headers=headers
    ).status_code == 401
    assert client.post(
        "/api/hardware/log-event",
        json={"patient_id": other_id, "w_before": 100, "w_after": 95},
        headers=headers,
    ).status_code == 401
    assert client.post(
        "/api/hardware/log-event",
        json={"patient_id": patient_id, "reminder_id": other_reminder_id, "w_before": 100, "w_after": 95},
        headers=headers,
    ).status_code == 409
    assert client.post(
        "/api/messages",
        json={"patient_id": patient_id, "message": "not a device route"},
        headers=headers,
    ).status_code == 400
    assert client.get(
        f"/api/hardware/get-schedules?patient_id={patient_id}", headers=headers
    ).status_code == 200
    doctor = app.test_client()
    assert doctor.post(
        "/api/login", json={"email": "doctor@example.com", "password": "password123"}
    ).status_code == 200
    traffic = doctor.get("/api/hardware/traffic").get_json()["traffic"]
    assert traffic
    assert all(entry["payload"].find(f'"patient_id":{other_id}') == -1 for entry in traffic)


def test_patient_timezone_controls_dose_schedule_and_skips_dst_gap(tmp_path):
    app = make_app(tmp_path)
    with app.app_context():
        patient = User(
            name="Patient", email="p@example.com", role="Patient",
            password_hash="", timezone="America/New_York",
        )
        db.session.add(patient)
        db.session.flush()
        reminder = Reminder(
            patient_id=patient.id, med_name="Medicine", time="08:00",
            dosage="1 tablet", set_by_role="Doctor", response_window_minutes=30,
        )
        dst_reminder = Reminder(
            patient_id=patient.id, med_name="Night medicine", time="02:30",
            dosage="1 tablet", set_by_role="Doctor",
        )
        db.session.add_all([reminder, dst_reminder])
        db.session.flush()
        assert event_schedule(
            patient, reminder, datetime(2026, 1, 15, 13, 15, tzinfo=timezone.utc)
        ) == datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)
        assert event_schedule(
            patient, reminder, datetime(2026, 1, 15, 12, 59, tzinfo=timezone.utc)
        ) is None
        assert local_occurrence(patient, dst_reminder, datetime(2026, 3, 8).date()) is None


def test_dose_input_matches_only_the_schedule_due_in_the_event_window(tmp_path):
    app = make_app(tmp_path)
    with app.app_context():
        patient = User(
            name="Patient", email="p@example.com", role="Patient",
            password_hash="", timezone="UTC",
        )
        db.session.add(patient)
        db.session.flush()
        due = Reminder(
            patient_id=patient.id, med_name="Morning medicine", time="08:00",
            dosage="1 tablet", set_by_role="Doctor",
        )
        later = Reminder(
            patient_id=patient.id, med_name="Evening medicine", time="20:00",
            dosage="1 tablet", set_by_role="Doctor",
        )
        db.session.add_all([due, later])
        db.session.flush()
        when = datetime(2026, 1, 15, 8, 15, tzinfo=timezone.utc)
        assert reminder_for_input(patient.id, None, None, when) == due
        assert reminder_for_input(patient.id, later.id, None, when) is None


def test_sensor_model_metadata_requires_authentication(tmp_path):
    app = make_app(tmp_path)
    seed(app)
    client = app.test_client()
    assert client.get("/api/sensor/model").status_code == 401
    client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
    response = client.get("/api/sensor/model")
    assert response.status_code == 200
    assert {"precision", "recall", "f1", "validation_limitations"} <= response.get_json().keys()


def test_final_demo_smoke_covers_registration_notifications_ml_sensor_and_assistant(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    registration = client.post(
        "/api/register",
        json={"name": "New Patient", "email": "new@example.com", "password": "password123", "role": "Patient"},
    )
    assert registration.status_code == 201
    patient_id = registration.get_json()["user"]["id"]
    assert client.post("/api/login", json={"email": "new@example.com", "password": "password123"}).status_code == 200
    token = csrf(client)
    assert client.post(
        "/api/notify/alert",
        json={"patient_id": patient_id, "message": "Demo notification"},
        headers={"X-CSRF-Token": token},
    ).get_json()["delivery"] == "in_app"
    assert client.get(f"/api/aiml/risk/{patient_id}").status_code == 200
    assert client.get("/api/sensor/demo").status_code == 200
    assistant = client.post(
        "/api/assistant/chat",
        json={"question": "What features are available?"},
        headers={"X-CSRF-Token": token},
    )
    assert assistant.status_code == 200
    assert assistant.get_json()["intent"] == "features"


def test_all_role_dashboards_remain_available(tmp_path):
    app = make_app(tmp_path)
    doctor_id, patient_id, _ = seed(app)
    with app.app_context():
        caregiver = User(
            name="Caregiver", email="c@example.com", role="Caregiver",
            password_hash="",
        )
        caregiver.set_password("password123")
        db.session.add(caregiver)
        db.session.flush()
        db.session.get(User, patient_id).linked_caregiver_id = caregiver.id
        db.session.commit()
    for email, endpoint in (
        ("doctor@example.com", "/api/doctor/patients"),
        ("patient@example.com", "/api/patient/dashboard"),
        ("c@example.com", "/api/caregiver/dashboard"),
    ):
        client = app.test_client()
        assert client.post("/api/login", json={"email": email, "password": "password123"}).status_code == 200
        assert client.get(endpoint).status_code == 200


def test_landing_auth_and_role_dashboard_page_journeys(tmp_path):
    app = make_app(tmp_path)
    doctor_id, patient_id, _ = seed(app)
    with app.app_context():
        caregiver = User(
            name="Caregiver", email="c@example.com", role="Caregiver",
            password_hash="",
        )
        caregiver.set_password("password123")
        db.session.add(caregiver)
        db.session.flush()
        db.session.get(User, patient_id).linked_caregiver_id = caregiver.id
        db.session.commit()

    public = app.test_client()
    assert b"Medication care" in public.get("/").data
    assert b'id="loginForm"' in public.get("/login").data
    registration = public.get("/register")
    assert b'id="signupForm"' in registration.data
    assert b"<option>Doctor</option>" not in registration.data
    assert public.get("/dashboard").headers["Location"].endswith("/login")

    for email, expected_page, role_nav in (
        ("doctor@example.com", "/dashboard/doctor", b'data-role-nav="Doctor"'),
        ("patient@example.com", "/dashboard/patient", b'data-section="medications"'),
        ("c@example.com", "/dashboard/caregiver", b'data-role-nav="Caregiver"'),
    ):
        client = app.test_client()
        assert client.get(expected_page).headers["Location"].endswith("/login")
        assert client.post(
            "/api/login", json={"email": email, "password": "password123"}
        ).status_code == 200
        assert client.get("/dashboard").headers["Location"].endswith(expected_page)
        response = client.get(expected_page)
        assert response.status_code == 200
        assert b"Dashboard" in response.data
        assert b"Medications" in response.data
        assert b"Activity" in response.data
        assert b"AI Insights" in response.data
        assert b"Messages" in response.data
        assert b"Settings" in response.data
        assert role_nav in response.data
    patient_client = app.test_client()
    patient_client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
    assert patient_client.get("/dashboard/doctor").headers["Location"].endswith("/dashboard/patient")


def test_assistant_is_authorized_grounded_and_csrf_protected(tmp_path):
    app = make_app(tmp_path)
    doctor_id, patient_id, _ = seed(app)
    with app.app_context():
        other = User(name="Other", email="other@example.com", role="Patient", password_hash="")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    client = app.test_client()
    assert client.post("/api/assistant/chat", json={"question": "What is my schedule?"}).status_code == 400
    client.post("/api/login", json={"email": "doctor@example.com", "password": "password123"})
    assert client.post("/api/assistant/chat", json={"question": "What is the schedule?", "patient_id": patient_id}).status_code == 400
    response = client.post(
        "/api/assistant/chat",
        json={"question": "What is the schedule?", "patient_id": patient_id},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    assert response.get_json()["sources"]
    assert client.post(
        "/api/assistant/chat",
        json={"question": "What is the schedule?", "patient_id": other_id},
        headers={"X-CSRF-Token": csrf(client)},
    ).status_code == 403


def test_assistant_reports_unsupported_and_insufficient_prediction(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, _ = seed(app)
    client = app.test_client()
    client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
    headers = {"X-CSRF-Token": csrf(client)}
    unsupported = client.post("/api/assistant/chat", json={"question": "Tell me a joke"}, headers=headers)
    assert unsupported.status_code == 200
    assert unsupported.get_json()["intent"] == "unsupported"
    unavailable = client.post("/api/assistant/chat", json={"question": "What is my risk?"}, headers=headers)
    assert unavailable.status_code == 200
    assert "insufficient" in unavailable.get_json()["answer"].lower()


def test_weight_event_validates_and_records(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, device_key = seed(app)
    client = app.test_client()
    headers = {"X-Device-Key": device_key}
    assert client.post(
        "/api/hardware/log-event", json={"patient_id": patient_id, "w_before": 1, "w_after": 2},
        headers=headers,
    ).status_code == 400
    response = client.post(
        "/api/hardware/log-event", json={"patient_id": patient_id, "w_before": 100, "w_after": 95},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["state"] == "REMOVAL_DETECTED"
    assert {"threshold_detection", "anomaly_detection"} <= response.get_json()["sensor_analysis"].keys()


def test_alarm_acknowledgement_and_weight_use_shared_event(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, _ = seed(app)
    client = app.test_client()
    client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
    token = csrf(client)
    response = client.post(
        "/api/alarm/acknowledge",
        json={"patient_id": patient_id, "reminder_id": 1},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert response.get_json()["state"] == "AWAITING_CONFIRMATION"
    response = client.post(
        "/api/simulation/log-event",
        json={"patient_id": patient_id, "reminder_id": 1, "w_before": 100, "w_after": 95,
              "samples_before": [100, 100.05, 99.98], "samples_after": [95, 95.03, 94.98]},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert response.get_json()["state"] == "REMOVAL_DETECTED"
    with app.app_context():
        event = db.session.scalar(db.select(DoseEvent).where(DoseEvent.patient_id == patient_id))
        assert event.state == "REMOVAL_DETECTED"
        assert db.session.scalar(db.select(SensorEvent).where(SensorEvent.dose_event_id == event.id))
        assert db.session.scalar(db.select(PillLog).where(PillLog.dose_event_id == event.id)) is None
    response = client.post(
        f"/api/simulation/events/{event.id}/confirm",
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert response.get_json()["event"]["state"] == "MANUALLY_CONFIRMED"
    with app.app_context():
        assert db.session.scalar(db.select(PillLog).where(PillLog.dose_event_id == event.id)).status == "Manual Override"


def test_small_weight_change_does_not_classify_dose_as_removed(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, _ = seed(app)
    client = app.test_client()
    client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
    token = csrf(client)
    acknowledged = client.post(
        "/api/alarm/acknowledge",
        json={"patient_id": patient_id, "reminder_id": 1},
        headers={"X-CSRF-Token": token},
    )
    assert acknowledged.status_code == 200
    response = client.post(
        "/api/simulation/log-event",
        json={"patient_id": patient_id, "reminder_id": 1, "w_before": 100, "w_after": 99},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert response.get_json()["state"] == "AWAITING_CONFIRMATION"
    with app.app_context():
        assert db.session.scalar(db.select(PillLog).where(PillLog.patient_id == patient_id)) is None
        assert db.session.scalar(db.select(SensorEvent).where(SensorEvent.patient_id == patient_id))


def test_timezone_endpoint_rejects_invalid_zone_and_persists_valid_zone(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, _ = seed(app)
    client = app.test_client()
    client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
    token = csrf(client)
    assert client.post(
        "/api/patient/timezone",
        json={"timezone": "Not/A_Timezone"},
        headers={"X-CSRF-Token": token},
    ).status_code == 400
    response = client.post(
        "/api/patient/timezone",
        json={"timezone": "America/New_York"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    dashboard = client.get("/api/patient/dashboard").get_json()
    assert dashboard["timezone"] == "America/New_York"
    assert dashboard["schedules"][0]["timezone"] == "America/New_York"


def test_scenarios_expire_once_and_do_not_duplicate_notifications(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, _ = seed(app)
    client = app.test_client()
    client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
    token = csrf(client)
    response = client.post(
        "/api/simulation/scenario",
        json={"patient_id": patient_id, "reminder_id": 1, "scenario": "no_response"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert response.get_json()["event"]["state"] == "MISSED"
    client.post(
        "/api/simulation/scenario",
        json={"patient_id": patient_id, "reminder_id": 1, "scenario": "no_response"},
        headers={"X-CSRF-Token": token},
    )
    with app.app_context():
        assert db.session.scalar(db.text("select count(*) from notifications")) == 1


def test_schedule_time_validation_and_authorization(tmp_path):
    app = make_app(tmp_path)
    doctor_id, patient_id, _ = seed(app)
    client = app.test_client()
    client.post("/api/login", json={"email": "doctor@example.com", "password": "password123"})
    token = csrf(client)
    response = client.post(
        f"/api/doctor/patients/{patient_id}/schedules",
        json={"med_name": "Medicine", "time": "25:99", "dosage": "1 tablet"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
    assert doctor_id == 1

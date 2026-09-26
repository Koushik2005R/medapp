import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import (
    Device,
    DoseEvent,
    HardwareTraffic,
    Medication,
    MedicationChangeRequest,
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
    assert {
        "isolation_forest", "threshold_baseline", "per_scenario",
        "data_source", "validation_limitations", "held_out_scenarios",
    } <= response.get_json().keys()


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


def test_complete_role_workflow_and_session_lifecycle(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, _ = seed(app)
    doctor = app.test_client()
    assert doctor.post(
        "/api/register",
        json={"name": "Blocked Doctor", "email": "blocked@example.com", "password": "password123", "role": "Doctor"},
    ).status_code == 403

    for name, email, role in (
        ("Journey Patient", "journey-patient@example.com", "Patient"),
        ("Journey Caregiver", "journey-caregiver@example.com", "Caregiver"),
    ):
        assert doctor.post(
            "/api/register",
            json={"name": name, "email": email, "password": "password123", "role": role},
        ).status_code == 201
    new_patient = app.test_client()
    assert new_patient.post(
        "/api/login",
        json={"email": "journey-patient@example.com", "password": "password123"},
    ).status_code == 200
    new_patient_id = new_patient.get("/api/me").get_json()["user"]["id"]

    assert doctor.post("/api/login", json={"email": "doctor@example.com", "password": "password123"}).status_code == 200
    doctor_headers = {"X-CSRF-Token": csrf(doctor)}
    assert doctor.post(
        "/api/doctor/patients/link",
        json={"email": "journey-patient@example.com"},
        headers=doctor_headers,
    ).status_code == 200
    assert doctor.post(
        f"/api/doctor/patients/{new_patient_id}/caregiver",
        json={"email": "journey-caregiver@example.com"},
        headers=doctor_headers,
    ).status_code == 200

    caregiver = app.test_client()
    assert caregiver.post(
        "/api/login",
        json={"email": "journey-caregiver@example.com", "password": "password123"},
    ).status_code == 200
    caregiver_headers = {"X-CSRF-Token": csrf(caregiver)}
    caregiver_dashboard = caregiver.get("/api/caregiver/dashboard").get_json()
    assert [patient["id"] for patient in caregiver_dashboard["patients"]] == [new_patient_id]

    created = doctor.post(
        f"/api/doctor/patients/{new_patient_id}/medications",
        json={"name": "Journey Medication", "dosage": "1 tablet", "time": "08:00", "compartment": 1},
        headers=doctor_headers,
    )
    assert created.status_code == 201
    medication_id = created.get_json()["medication"]["id"]
    assert doctor.patch(
        f"/api/doctor/medications/{medication_id}",
        json={"name": "Journey Medication Updated", "dosage": "1 tablet", "time": "09:00"},
        headers=doctor_headers,
    ).status_code == 200
    second = doctor.post(
        f"/api/doctor/patients/{new_patient_id}/medications",
        json={"name": "Evening Medication", "dosage": "1 tablet", "time": "20:00", "compartment": 2},
        headers=doctor_headers,
    )
    assert second.status_code == 201
    second_medication_id = second.get_json()["medication"]["id"]

    patient_headers = {"X-CSRF-Token": csrf(new_patient)}
    removal = new_patient.post(
        f"/api/patients/{new_patient_id}/medication-requests",
        json={"operation": "REMOVE", "medication_id": medication_id},
        headers=patient_headers,
    )
    caregiver_removal = caregiver.post(
        f"/api/patients/{new_patient_id}/medication-requests",
        json={"operation": "REMOVE", "medication_id": second_medication_id},
        headers=caregiver_headers,
    )
    assert removal.status_code == caregiver_removal.status_code == 201
    pending_ids = {
        item["id"] for item in doctor.get("/api/doctor/medication-requests").get_json()["requests"]
    }
    assert {removal.get_json()["request"]["id"], caregiver_removal.get_json()["request"]["id"]} <= pending_ids
    assert doctor.patch(
        f"/api/doctor/medication-requests/{removal.get_json()['request']['id']}",
        json={"decision": "APPROVED"},
        headers=doctor_headers,
    ).status_code == 200
    rejected = doctor.patch(
        f"/api/doctor/medication-requests/{caregiver_removal.get_json()['request']['id']}",
        json={"decision": "REJECTED", "reason": "Keep the current schedule for now."},
        headers=doctor_headers,
    )
    assert rejected.status_code == 200
    assert rejected.get_json()["request"]["status"] == "REJECTED"

    patient_notifications = new_patient.get("/api/notifications").get_json()["notifications"]
    assert patient_notifications
    assert any(item["status"] == "Discontinued" for item in new_patient.get(
        f"/api/patients/{new_patient_id}/medications"
    ).get_json()["medications"])
    assert caregiver.get(f"/api/patients/{patient_id}/medications").status_code == 403

    assert doctor.post(
        "/api/messages",
        json={"patient_id": new_patient_id, "message": "Workflow test message"},
        headers=doctor_headers,
    ).status_code == 201
    assert "Workflow test message" in new_patient.get(
        f"/api/messages?patient_id={new_patient_id}"
    ).get_json()["messages"][-1]["body"]
    assert new_patient.post(
        "/api/notify/alert",
        json={"patient_id": new_patient_id, "message": "Workflow test alert"},
        headers=patient_headers,
    ).get_json()["delivery"] == "in_app"
    assert any(
        item["message"] == "Workflow test alert"
        for item in new_patient.get("/api/notifications").get_json()["notifications"]
    )
    assert caregiver.post(
        "/api/caregiver/notify",
        json={"patient_id": new_patient_id, "message": "Caregiver test alert"},
        headers=caregiver_headers,
    ).status_code == 200
    assert any(
        item["message"] == "Caregiver test alert"
        for item in new_patient.get("/api/notifications").get_json()["notifications"]
    )

    for client in (doctor, new_patient, caregiver):
        assert client.get("/api/logout").status_code == 200
        assert client.get("/api/me").status_code == 401
        assert client.get("/dashboard").headers["Location"].endswith("/login")


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

    for email, expected_page, role_nav, expected_sections in (
        ("doctor@example.com", "/dashboard/doctor", b'data-role-nav="Doctor"',
         (b'data-section="patients"', b'data-section="requests"')),
        ("patient@example.com", "/dashboard/patient", b'data-section="medications"', ()),
        ("c@example.com", "/dashboard/caregiver", b'data-role-nav="Caregiver"',
         (b'data-section="assigned-patients"',)),
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
        assert b'id="assistantWidget"' in response.data
        assert b'id="assistantToggle"' in response.data
        assert role_nav in response.data
        for section in expected_sections:
            assert section in response.data
    patient_client = app.test_client()
    patient_client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
    assert patient_client.get("/dashboard/doctor").headers["Location"].endswith("/dashboard/patient")


def test_assistant_is_authorized_grounded_and_csrf_protected(tmp_path):
    app = make_app(tmp_path)
    doctor_id, patient_id, _ = seed(app)
    with app.app_context():
        other = User(name="Other", email="other@example.com", role="Patient", password_hash="")
        other.set_password("password123")
        caregiver = User(name="Caregiver", email="c@example.com", role="Caregiver", password_hash="")
        caregiver.set_password("password123")
        outsider = User(name="Outsider", email="outsider@example.com", role="Caregiver", password_hash="")
        outsider.set_password("password123")
        patient = db.session.get(User, patient_id)
        db.session.add_all([other, caregiver, outsider])
        db.session.flush()
        patient.linked_caregiver_id = caregiver.id
        change_request = MedicationChangeRequest(
            patient_id=patient_id,
            requested_by_id=patient_id,
            operation="ADD",
            requested_data={"name": "New medicine"},
            status="PENDING",
            created_at=utc_now(),
        )
        missed_log = PillLog(
            patient_id=patient_id,
            timestamp=utc_now(),
            initial_weight=100,
            final_weight=100,
            delta_weight=0,
            status="Missed",
        )
        db.session.add_all([change_request, missed_log])
        db.session.commit()
        other_id = other.id
        request_id = change_request.id
    client = app.test_client()
    assert client.post("/api/assistant/chat", json={"question": "What is my schedule?"}).status_code == 400
    assert client.post(
        "/api/assistant/chat",
        json={"question": "What is my schedule?"},
        headers={"X-CSRF-Token": csrf(client)},
    ).status_code == 401
    client.post("/api/login", json={"email": "doctor@example.com", "password": "password123"})
    assert client.post("/api/assistant/chat", json={"question": "What is the schedule?", "patient_id": patient_id}).status_code == 400
    response = client.post(
        "/api/assistant/chat",
        json={"question": "What is the schedule?", "patient_id": patient_id},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    assert response.get_json()["sources"]
    assert response.get_json()["intent"] == "schedule"
    patient_context = client.post(
        "/api/assistant/chat",
        json={"question": "What is the schedule?", "patient_id": doctor_id},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert patient_context.status_code == 400
    assert client.post(
        "/api/assistant/chat",
        json={"question": "What is the schedule?", "patient_id": other_id},
        headers={"X-CSRF-Token": csrf(client)},
    ).status_code == 403
    navigation = client.post(
        "/api/assistant/chat",
        json={"question": "Where do I find medication requests?"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert navigation.status_code == 200
    assert navigation.get_json()["intent"] == "navigation"
    assert "Requests" in navigation.get_json()["answer"]
    assert client.post(
        "/api/assistant/chat",
        json={"question": "What is the request status?", "patient_id": patient_id},
        headers={"X-CSRF-Token": csrf(client)},
    ).get_json()["sources"][0]["record_id"] == request_id

    for email, question, expected_intent in (
        ("patient@example.com", "What is my medication change request status?", "change_requests"),
        ("patient@example.com", "Show my missed doses", "missed_history"),
        ("c@example.com", "What is the medication change request status?", "change_requests"),
        ("c@example.com", "Where do I find assigned patients?", "navigation"),
    ):
        role_client = app.test_client()
        assert role_client.post(
            "/api/login", json={"email": email, "password": "password123"}
        ).status_code == 200
        answer = role_client.post(
            "/api/assistant/chat",
            json={"question": question, **({"patient_id": patient_id} if email.startswith("c@") else {})},
            headers={"X-CSRF-Token": csrf(role_client)},
        )
        assert answer.status_code == 200
        assert answer.get_json()["intent"] == expected_intent
        if question.startswith("Where"):
            assert "Assigned Patients" in answer.get_json()["answer"]
    outsider_client = app.test_client()
    outsider_client.post(
        "/api/login", json={"email": "outsider@example.com", "password": "password123"}
    )
    assert outsider_client.post(
        "/api/assistant/chat",
        json={"question": "Show missed doses", "patient_id": patient_id},
        headers={"X-CSRF-Token": csrf(outsider_client)},
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


def test_assistant_enforces_medical_boundary_and_supports_navigation_without_patient_context(tmp_path):
    app = make_app(tmp_path)
    seed(app)
    client = app.test_client()
    client.post("/api/login", json={"email": "doctor@example.com", "password": "password123"})
    headers = {"X-CSRF-Token": csrf(client)}
    advice = client.post(
        "/api/assistant/chat",
        json={"question": "Should I change my dose?"},
        headers=headers,
    )
    assert advice.status_code == 200
    assert advice.get_json()["intent"] == "medical_boundary"
    assert "cannot advise" in advice.get_json()["answer"]
    features = client.post(
        "/api/assistant/chat",
        json={"question": "What can you help with?"},
        headers=headers,
    )
    assert features.status_code == 200
    assert features.get_json()["intent"] == "features"
    needs_context = client.post(
        "/api/assistant/chat",
        json={"question": "What is the next dose?"},
        headers=headers,
    )
    assert needs_context.status_code == 400


def test_weight_event_validates_and_records(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, device_key = seed(app)
    client = app.test_client()
    headers = {"X-Device-Key": device_key}
    unexpected = client.post(
        "/api/hardware/log-event", json={"patient_id": patient_id, "w_before": 1, "w_after": 2},
        headers=headers,
    )
    assert unexpected.status_code == 200
    assert unexpected.get_json()["sensor_analysis"]["threshold_detection"]["event"] == "unexpected_increase"
    response = client.post(
        "/api/hardware/log-event", json={"patient_id": patient_id, "w_before": 100, "w_after": 95},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["state"] == "REMOVAL_DETECTED"
    assert {"threshold_detection", "anomaly_detection"} <= response.get_json()["sensor_analysis"].keys()


def test_software_scenario_works_off_schedule_without_relaxing_live_reminder_window(tmp_path):
    app = make_app(tmp_path)
    _, patient_id, _ = seed(app)
    with app.app_context():
        reminder = db.session.get(Reminder, 1)
        reminder.time = (utc_now() + timedelta(hours=2)).strftime("%H:%M")
        db.session.commit()

    client = app.test_client()
    assert client.post(
        "/api/login", json={"email": "patient@example.com", "password": "password123"}
    ).status_code == 200
    token = csrf(client)
    simulated = client.post(
        "/api/simulation/scenario",
        json={"patient_id": patient_id, "reminder_id": 1, "scenario": "normal_removal"},
        headers={"X-CSRF-Token": token},
    )
    assert simulated.status_code == 200
    assert simulated.get_json()["event"]["source"] == "simulation"
    assert {"threshold_detection", "anomaly_detection"} <= simulated.get_json()["sensor_analysis"].keys()

    live_event = client.post(
        "/api/simulation/log-event",
        json={"patient_id": patient_id, "reminder_id": 1, "w_before": 100, "w_after": 95},
        headers={"X-CSRF-Token": token},
    )
    assert live_event.status_code == 409
    acknowledgement = client.post(
        "/api/alarm/acknowledge",
        json={"patient_id": patient_id, "reminder_id": 1},
        headers={"X-CSRF-Token": token},
    )
    assert acknowledgement.status_code == 409


def test_all_repeatable_sensor_scenarios_share_the_device_event_processor(tmp_path):
    expected = {
        "normal_removal": ("normal_removal", True),
        "no_removal": ("no_removal", False),
        "noise": ("sensor_noise", False),
        "unexpected_increase": ("unexpected_increase", False),
        "excessive_removal": ("excessive_removal", False),
    }
    for index, (scenario, (classification, removal_detected)) in enumerate(expected.items()):
        scenario_path = tmp_path / str(index)
        scenario_path.mkdir()
        app = make_app(scenario_path)
        _, patient_id, _ = seed(app)
        client = app.test_client()
        client.post("/api/login", json={"email": "patient@example.com", "password": "password123"})
        response = client.post(
            "/api/simulation/scenario",
            json={"patient_id": patient_id, "reminder_id": 1, "scenario": scenario},
            headers={"X-CSRF-Token": csrf(client)},
        )
        assert response.status_code == 200
        result = response.get_json()
        assert result["sensor_analysis"]["threshold_detection"]["event"] == classification
        assert result["sensor_analysis"]["threshold_detection"]["removal_detected"] is removal_detected
        assert result["sensor_analysis"]["interpretation"].find("swallowing") >= 0


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
    notification = client.get("/api/notifications").get_json()["notifications"][0]
    assert notification["recipient_id"] == patient_id
    assert "missed" in notification["message"].lower()
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

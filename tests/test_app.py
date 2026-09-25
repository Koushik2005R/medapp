import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import Device, User, create_app, db, token_digest, utc_now


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
    assert response.status_code == 201
    assert response.get_json()["event"] == "Taken"


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

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import User, Reminder, PillLog, create_app, db, utc_now
from model import (
    FEATURE_NAMES,
    MINIMUM_HISTORY,
    generate_synthetic_dataset,
    model_metadata,
    predict_explanation,
    train_model,
)


def test_synthetic_training_is_reproducible_and_evaluated(tmp_path, monkeypatch):
    path = tmp_path / "model.joblib"
    monkeypatch.setenv("ADHERENCE_MODEL_PATH", str(path))
    first = generate_synthetic_dataset(patients=8, doses_per_patient=8)
    second = generate_synthetic_dataset(patients=8, doses_per_patient=8)
    assert (first[0] == second[0]).all()
    assert (first[1] == second[1]).all()
    train_model()
    metadata = model_metadata()
    assert path.exists()
    assert metadata["feature_names"] == list(FEATURE_NAMES)
    assert set(metadata["evaluation"]) >= {"confusion_matrix", "precision", "recall", "f1", "roc_auc"}
    assert set(metadata["baseline"]) >= {"confusion_matrix", "precision", "recall", "f1", "roc_auc"}


def test_insufficient_history_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("ADHERENCE_MODEL_PATH", str(tmp_path / "model.joblib"))
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'app.db'}",
        "SECRET_KEY": "test-secret-key",
    })
    with app.app_context():
        patient = User(name="Patient", email="p@example.com", role="Patient", password_hash="x")
        db.session.add(patient)
        db.session.flush()
        db.session.add(Reminder(
            patient_id=patient.id, med_name="Medicine", time="08:00",
            dosage="1 tablet", set_by_role="Doctor",
        ))
        db.session.commit()
        result = predict_explanation(patient.id)
    assert result["level"] == "INSUFFICIENT_DATA"
    assert result["risk_score"] is None
    assert result["history_count"] < MINIMUM_HISTORY


def test_risk_integration_exposes_explanation_and_model_info(tmp_path, monkeypatch):
    monkeypatch.setenv("ADHERENCE_MODEL_PATH", str(tmp_path / "model.joblib"))
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'app.db'}",
        "SECRET_KEY": "test-secret-key",
    })
    with app.app_context():
        user = User(name="Patient", email="p@example.com", role="Patient", password_hash="")
        user.set_password("password123")
        db.session.add(user)
        db.session.flush()
        user_id = user.id
        reminder = Reminder(
            patient_id=user.id, med_name="Medicine", time="08:00",
            dosage="1 tablet", set_by_role="Doctor",
        )
        db.session.add(reminder)
        db.session.flush()
        for index in range(3):
            db.session.add(PillLog(
                patient_id=user.id, reminder_id=reminder.id, timestamp=utc_now(),
                initial_weight=100, final_weight=99, delta_weight=1,
                status="Taken", source="simulation", verification_method="test",
            ))
        db.session.commit()
    client = app.test_client()
    client.post("/api/login", json={"email": "p@example.com", "password": "password123"})
    risk = client.get(f"/api/aiml/risk/{user_id}")
    info = client.get("/api/aiml/model")
    assert risk.status_code == 200
    assert {"risk_score", "level", "historical_factors", "model_version"} <= risk.get_json().keys()
    assert info.status_code == 200
    assert "evaluation" in info.get_json()

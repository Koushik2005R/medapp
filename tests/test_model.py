import json
import os
from datetime import timedelta

import joblib

os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import User, Reminder, PillLog, create_app, db, utc_now
from model import (
    DATA_SOURCE,
    FEATURE_NAMES,
    MINIMUM_HISTORY,
    feature_vector,
    generate_synthetic_dataset,
    model_metadata,
    predict_explanation,
    train_model,
)


def test_synthetic_training_is_reproducible_and_evaluated(tmp_path, monkeypatch):
    path = tmp_path / "model.joblib"
    report = tmp_path / "metrics.json"
    monkeypatch.setenv("ADHERENCE_MODEL_PATH", str(path))
    first = generate_synthetic_dataset(patients=24, doses_per_patient=40)
    second = generate_synthetic_dataset(patients=24, doses_per_patient=40)
    assert (first[0] == second[0]).all()
    assert (first[1] == second[1]).all()
    train_model(
        output_path=path, report_path=report, patients=24, doses_per_patient=40
    )
    metadata = model_metadata()
    assert path.exists()
    assert report.exists()
    assert metadata["feature_names"] == list(FEATURE_NAMES)
    assert metadata["model_version"] == "synthetic-hidden-state-rf-v2"
    assert metadata["data_source"] == DATA_SOURCE
    assert metadata["split_method"].startswith("chronological")
    assert metadata["train_end"] < metadata["test_start"]
    assert metadata["training_rows"] + metadata["test_rows"] == len(first[1])
    assert set(metadata["evaluation"]) >= {"confusion_matrix", "precision", "recall", "f1", "roc_auc"}
    assert set(metadata["baseline"]) >= {"confusion_matrix", "precision", "recall", "f1", "roc_auc"}
    assert set(metadata["feature_importance"]) == set(FEATURE_NAMES)
    assert json.loads(report.read_text(encoding="utf-8"))["evaluation"] == metadata["evaluation"]
    artifact = joblib.load(path)
    assert artifact["metadata"]["model_version"] == metadata["model_version"]
    assert set(artifact["pipeline"].named_steps) == {"imputer", "classifier"}
    train_model(
        output_path=tmp_path / "second-model.joblib",
        report_path=tmp_path / "second-metrics.json",
        patients=24,
        doses_per_patient=40,
    )
    assert model_metadata()["evaluation"] == metadata["evaluation"]
    assert model_metadata()["baseline"] == metadata["baseline"]


def test_features_exclude_events_at_or_after_upcoming_dose():
    due = utc_now().replace(second=0, microsecond=0)
    history = [
        (due - timedelta(days=2), "Missed"),
        (due - timedelta(days=1), "Taken"),
        (due, "Missed"),
        (due + timedelta(days=1), "Missed"),
    ]
    features = feature_vector(history, due, scheduled_hour=8)
    assert features[3] == 2
    assert features[4] == 0.5
    assert features[5] == 1
    assert features[6] == 2


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
        no_schedule = User(
            name="No schedule", email="none@example.com", role="Patient", password_hash="x"
        )
        db.session.add(no_schedule)
        db.session.commit()
        result = predict_explanation(patient.id)
        no_schedule_result = predict_explanation(no_schedule.id)
    assert result["level"] == "INSUFFICIENT_DATA"
    assert result["risk_score"] is None
    assert result["history_count"] < MINIMUM_HISTORY
    assert result["upcoming_dose"]["medication"] == "Medicine"
    assert no_schedule_result["level"] == "NO_UPCOMING_DOSE"
    assert no_schedule_result["risk_score"] is None


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
        other = User(name="Other", email="other@example.com", role="Patient", password_hash="")
        db.session.add(other)
        db.session.flush()
        other_id = other.id
        reminder = Reminder(
            patient_id=user.id, med_name="Medicine", time="08:00",
            dosage="1 tablet", set_by_role="Doctor",
        )
        db.session.add(reminder)
        db.session.flush()
        for index in range(8):
            db.session.add(PillLog(
                patient_id=user.id, reminder_id=reminder.id,
                timestamp=utc_now() - timedelta(days=8 - index),
                initial_weight=100, final_weight=99, delta_weight=1,
                status="Taken", source="simulation", verification_method="test",
            ))
        db.session.commit()
    client = app.test_client()
    client.post("/api/login", json={"email": "p@example.com", "password": "password123"})
    risk = client.get(f"/api/aiml/risk/{user_id}")
    info = client.get("/api/aiml/model")
    prediction = client.get(f"/api/aiml/prediction/{user_id}")
    assert risk.status_code == 200
    assert {"risk_score", "level", "historical_factors", "model_version"} <= risk.get_json().keys()
    assert prediction.status_code == 200
    assert prediction.get_json()["upcoming_dose"]["medication"] == "Medicine"
    assert prediction.get_json()["history_count"] == 8
    assert 0 <= prediction.get_json()["miss_probability"] <= 1
    assert client.get(f"/api/aiml/prediction/{other_id}").status_code == 403
    assert info.status_code == 200
    assert "evaluation" in info.get_json()
    assert "baseline" in info.get_json()

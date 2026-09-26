import json

import numpy as np
import pytest

import sensor_analysis
from sensor_analysis import (
    analyze_readings,
    demo_sensor_analyses,
    evaluate_sensor_model,
    generate_sensor_noise,
    rolling_median,
    train_sensor_model,
)


def test_noise_filter_preserves_stable_signal_and_rejects_invalid_window():
    readings = generate_sensor_noise(noise_std=0.25, length=30, seed=7)
    result = analyze_readings(readings, expected_removal=5.0, tolerance=0.5)
    assert len(result["filtered_readings"]) == len(readings)
    assert result["features"]["variance"] < np.var(readings)
    assert result["threshold_detection"]["event"] in {"no_removal", "sensor_noise"}
    with pytest.raises(ValueError):
        rolling_median(readings, window=4)


def test_calibrated_expected_removal_is_separate_from_manual_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSOR_MODEL_PATH", str(tmp_path / "sensor.joblib"))
    before = [100.0] * 15
    after = [95.0] * 15
    result = analyze_readings(before + after, expected_removal=5, tolerance=0.3, calibrated_offset=1.25)
    assert result["threshold_detection"]["removal_detected"] is True
    assert result["features"]["weight_change"] == -5
    assert "cannot confirm" in result["interpretation"]
    assert result["model_version"] == sensor_analysis.SENSOR_MODEL_VERSION


def test_threshold_classes_cover_repeatable_sensor_conditions(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSOR_MODEL_PATH", str(tmp_path / "sensor.joblib"))
    stable = [100.0] * 20
    normal = [100.0] * 10 + [95.0] * 10
    increase = [100.0] * 10 + [105.0] * 10
    excessive = [100.0] * 10 + [90.0] * 10
    noise = generate_sensor_noise(100, 40, noise_std=0.9, seed=90)
    observed = {
        analyze_readings(stable, 5, 0.3)["threshold_detection"]["event"],
        analyze_readings(normal, 5, 0.3)["threshold_detection"]["event"],
        analyze_readings(noise, 5, 0.3)["threshold_detection"]["event"],
        analyze_readings(increase, 5, 0.3)["threshold_detection"]["event"],
        analyze_readings(excessive, 5, 0.3)["threshold_detection"]["event"],
    }
    assert observed == {"no_removal", "normal_removal", "sensor_noise", "unexpected_increase", "excessive_removal"}


def test_held_out_scenario_evaluation_and_training_artifact_are_repeatable(tmp_path, monkeypatch):
    path = tmp_path / "sensor.joblib"
    report = tmp_path / "sensor-metrics.json"
    monkeypatch.setenv("SENSOR_MODEL_PATH", str(path))
    first = train_sensor_model(path, report)
    second = evaluate_sensor_model()
    assert first == second
    assert json.loads(report.read_text(encoding="utf-8")) == first
    assert first["split_method"].startswith("Independent held-out")
    assert first["training_rows"] == 320
    assert first["test_rows"] == 300
    assert first["isolation_forest"]["confusion_matrix"] == second["isolation_forest"]["confusion_matrix"]
    assert set(first["isolation_forest"]) >= {"precision", "recall", "f1", "roc_auc", "confusion_matrix"}
    assert set(first["threshold_baseline"]) >= {"precision", "recall", "f1", "confusion_matrix"}
    assert set(first["per_scenario"]) == {
        "no_removal", "normal_removal", "sensor_noise", "unexpected_increase", "excessive_removal"
    }


def test_demo_examples_include_all_five_requested_scenarios(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSOR_MODEL_PATH", str(tmp_path / "sensor.joblib"))
    results = demo_sensor_analyses()
    assert [result["scenario"] for result in results] == [
        "No Removal", "Normal Removal", "Sensor Noise", "Unexpected Increase", "Excessive Removal"
    ]
    assert all("threshold_detection" in result and "anomaly_detection" in result for result in results)
    assert all("swallowing" in result["interpretation"] for result in results)


def test_invalid_physical_readings_rejected():
    with pytest.raises(ValueError):
        analyze_readings([100.0, 100001.0], expected_removal=5, tolerance=0.5)
    with pytest.raises(ValueError):
        analyze_readings([100.0, 99.0], expected_removal=0, tolerance=0.5)

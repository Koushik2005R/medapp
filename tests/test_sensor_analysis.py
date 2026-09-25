import numpy as np

from sensor_analysis import (
    analyze_readings,
    evaluate_sensor_model,
    generate_sensor_noise,
    rolling_median,
)


def test_noise_filter_preserves_stable_signal():
    readings = generate_sensor_noise(noise_std=0.25, length=30, seed=7)
    result = analyze_readings(readings, expected_removal=5.0, tolerance=0.5)
    assert len(result["filtered_readings"]) == len(readings)
    assert result["features"]["variance"] < np.var(readings)
    assert result["threshold_detection"]["event"] in {"no_removal", "unstable_reading"}


def test_normal_removal_threshold_is_not_anomaly_claim():
    readings = [100.0] * 15 + [95.0] * 15
    result = analyze_readings(readings, expected_removal=5.0, tolerance=0.3)
    assert result["threshold_detection"]["removal_detected"] is True
    assert "does not confirm" in result["interpretation"]


def test_unexpected_events_are_classified():
    increase = analyze_readings([100.0] * 10 + [104.0] * 10, 5.0, 0.3)
    excessive = analyze_readings([100.0] * 10 + [85.0] * 10, 5.0, 0.3)
    assert increase["threshold_detection"]["event"] == "unexpected_increase"
    assert excessive["threshold_detection"]["event"] == "excessive_removal"


def test_sensor_evaluation_is_reproducible_and_reports_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSOR_MODEL_PATH", str(tmp_path / "sensor.joblib"))
    import sensor_analysis
    sensor_analysis.MODEL_PATH = tmp_path / "sensor.joblib"
    first = evaluate_sensor_model()
    second = evaluate_sensor_model()
    assert first == second
    assert set(first) >= {"precision", "recall", "f1", "data_source", "validation_limitations"}

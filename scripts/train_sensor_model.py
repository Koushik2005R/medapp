"""Train and evaluate the repeatable synthetic sensor-anomaly experiment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sensor_analysis import model_path, train_sensor_model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, default=model_path())
    parser.add_argument("--report-path", type=Path, default=None)
    args = parser.parse_args()
    report = args.report_path or args.model_path.with_suffix(".metrics.json")
    metadata = train_sensor_model(args.model_path, report)
    print(json.dumps({
        "model_path": str(args.model_path),
        "report_path": str(report),
        "results": metadata,
    }, indent=2))


if __name__ == "__main__":
    main()

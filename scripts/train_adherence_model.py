"""Train and evaluate the reproducible synthetic adherence experiment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model import model_path, train_model


def main() -> None:
    default_path = model_path()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, default=default_path)
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--patients", type=int, default=64)
    parser.add_argument("--doses-per-patient", type=int, default=120)
    args = parser.parse_args()
    report_path = args.report_path or args.model_path.with_suffix(".metrics.json")
    train_model(
        output_path=args.model_path,
        report_path=report_path,
        seed=args.seed,
        patients=args.patients,
        doses_per_patient=args.doses_per_patient,
    )
    print(json.dumps({
        "model_path": str(args.model_path),
        "report_path": str(report_path),
        "results": json.loads(report_path.read_text(encoding="utf-8")),
    }, indent=2))


if __name__ == "__main__":
    main()

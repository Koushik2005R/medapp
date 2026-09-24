"""Minimal ESP32-style verification client.

Run the Flask server first, then:
    python scripts/hardware_test.py --patient-id 1
"""

from __future__ import annotations

import argparse
import json
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--patient-id", type=int, required=True)
    args = parser.parse_args()

    schedule_url = f"{args.base_url}/api/hardware/get-schedules?patient_id={args.patient_id}"
    with urlopen(schedule_url, timeout=10) as response:
        print("Schedules:", response.status, response.read().decode())

    payload = json.dumps({
        "patient_id": args.patient_id,
        "w_before": 100.0,
        "w_after": 95.0,
    }).encode()
    request = Request(
        f"{args.base_url}/api/hardware/log-event",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        print("Taken event:", response.status, response.read().decode())


if __name__ == "__main__":
    main()

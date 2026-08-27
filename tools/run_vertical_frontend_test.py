from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "tests/test_traveler_ui.py",
    ]
    result = subprocess.run(command, cwd=ROOT, check=False)
    summary = {
        "slice": "12-streamlit-traveler-experience",
        "status": "passed" if result.returncode == 0 else "failed",
        "expected_outputs": {
            "pdf_validation": "invalid or oversized evidence is rejected",
            "api_boundary": "PDF is posted to /v1/trips/activate",
            "privacy": "booking reference is masked and upstream errors are sanitized",
            "customer_journey": "upload leads to a ready trip with MAN to FRA visible",
            "monitoring": "one active flight and next automatic check are displayed",
        },
    }
    print(json.dumps(summary, indent=2))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

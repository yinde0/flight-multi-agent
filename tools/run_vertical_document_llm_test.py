from __future__ import annotations

import json
import time

from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ROOT / "travel_eval" / "fixtures" / "documents"


def main() -> int:
    base_url = "http://127.0.0.1:8080"
    with httpx.Client(timeout=90, trust_env=False) as client:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if client.get(f"{base_url}/health/live").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Travel API did not become ready")

        pdf = ROOT / "output" / "pdf" / "redacted_ambiguous_scan.pdf"
        response = client.post(
            f"{base_url}/v1/documents/parse",
            files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={
                "trip_id": "trip-fixture-llm-fallback",
                "traveler_ref": "traveler-synthetic-001",
                "fixture_id": "doc-fixture-llm-fallback",
            },
        )
        response.raise_for_status()
        actual = response.json()

    expected = json.loads(
        (DOCUMENTS / "expected_llm_fallback_itinerary.json").read_text(
            encoding="utf-8"
        )
    )
    orchestration = actual.get("orchestration") or {}
    observed = {
        "status": actual.get("status"),
        "itinerary_matches": actual.get("itinerary") == expected,
        "text_source": orchestration.get("text_source"),
        "ocr_calls": orchestration.get("ocr_calls"),
        "llm_calls": orchestration.get("llm_calls"),
        "llm_provider": (orchestration.get("llm") or {}).get("provider"),
        "llm_result": (orchestration.get("llm") or {}).get("result"),
    }
    expected_view = {
        "status": "parsed",
        "itinerary_matches": True,
        "text_source": "mistral_ocr",
        "ocr_calls": 1,
        "llm_calls": 1,
        "llm_provider": "azure_openai",
        "llm_result": "parsed",
    }
    report = {
        "passed": observed == expected_view,
        "synthetic_only": True,
        "real_azure_calls": 0,
        "observed": observed,
        "expected": expected_view,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

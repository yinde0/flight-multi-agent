from __future__ import annotations

import argparse
import json
import time

from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ROOT / "travel_eval" / "fixtures" / "documents"


CASES = [
    {
        "name": "clean-direct",
        "filename": "synthetic_direct_eticket.pdf",
        "trip_id": "trip-fixture-direct",
        "fixture_id": "doc-direct-clean",
        "golden": "expected_direct_itinerary.json",
        "assertion": "exact-itinerary",
    },
    {
        "name": "clean-connection",
        "filename": "synthetic_connection_itinerary.pdf",
        "trip_id": "trip-fixture-connection",
        "fixture_id": "doc-connection-clean",
        "golden": "expected_connection_itinerary.json",
        "assertion": "exact-itinerary",
    },
    {
        "name": "ambiguous-image-only",
        "filename": "redacted_ambiguous_scan.pdf",
        "trip_id": "trip-fixture-ambiguous",
        "fixture_id": "doc-ambiguous-scan",
        "golden": "expected_ambiguous_parse.json",
        "assertion": "abstention-safety",
    },
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def wait_for_api(
    client: httpx.Client, base_url: str, *, timeout_seconds: float
) -> None:
    """Wait until Docker's published port and the API process are both ready."""

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    health_url = f"{base_url.rstrip('/')}/health/live"
    while time.monotonic() < deadline:
        try:
            response = client.get(health_url)
            if response.status_code == 200:
                return
        except httpx.TransportError as error:
            last_error = error
        time.sleep(0.5)
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(
        f"Travel API was not ready after {timeout_seconds:g} seconds{detail}"
    )


def post_pdf_with_retry(
    client: httpx.Client,
    url: str,
    *,
    filename: str,
    content: bytes,
    data: dict[str, str],
    attempts: int = 3,
) -> httpx.Response:
    """Retry transport hand-off failures, never application HTTP errors."""

    for attempt in range(1, attempts + 1):
        try:
            return client.post(
                url,
                files={"file": (filename, content, "application/pdf")},
                data=data,
            )
        except httpx.TransportError:
            if attempt == attempts:
                raise
            time.sleep(0.5 * attempt)
    raise AssertionError("unreachable")


def judge(case: dict[str, str], actual: dict[str, Any]) -> tuple[bool, Any, Any]:
    expected = load_json(DOCUMENTS / case["golden"])
    if case["assertion"] == "exact-itinerary":
        observed = actual.get("itinerary")
        return actual.get("status") == "parsed" and observed == expected, observed, expected

    review = actual.get("review") or {}
    orchestration = actual.get("orchestration") or {}
    safety_view = {
        "review_required": review.get("review_required"),
        "reason_codes": review.get("reason_codes"),
        "safe_partial_extraction": review.get("safe_partial_extraction"),
        "must_not_infer": review.get("must_not_infer"),
        "itinerary_is_absent": actual.get("itinerary") is None,
        "text_source": orchestration.get("text_source"),
        "ocr_calls": orchestration.get("ocr_calls"),
    }
    expected_view = {
        "review_required": expected["review_required"],
        "reason_codes": expected["reason_codes"],
        "safe_partial_extraction": expected["safe_partial_extraction"],
        "must_not_infer": expected["must_not_infer"],
        "itinerary_is_absent": True,
        "text_source": "mistral_ocr",
        "ocr_calls": 1,
    }
    return safety_view == expected_view, safety_view, expected_view


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    args = parser.parse_args()
    results = []

    # The test target is local Docker. Do not route it through HTTP_PROXY/HTTPS_PROXY.
    with httpx.Client(timeout=60, trust_env=False) as client:
        wait_for_api(
            client,
            args.base_url,
            timeout_seconds=args.startup_timeout,
        )
        for case in CASES:
            pdf_path = ROOT / "output" / "pdf" / case["filename"]
            response = post_pdf_with_retry(
                client,
                f"{args.base_url.rstrip('/')}/v1/documents/parse",
                filename=pdf_path.name,
                content=pdf_path.read_bytes(),
                data={
                    "trip_id": case["trip_id"],
                    "traveler_ref": "traveler-synthetic-001",
                    "fixture_id": case["fixture_id"],
                },
            )
            response.raise_for_status()
            actual = response.json()
            passed, observed, expected = judge(case, actual)
            results.append(
                {
                    "case": case["name"],
                    "assertion": case["assertion"],
                    "passed": passed,
                    "observed": observed,
                    "expected": expected,
                }
            )

    report = {"passed": all(item["passed"] for item in results), "cases": results}
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

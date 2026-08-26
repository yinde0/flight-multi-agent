from __future__ import annotations

import hashlib
import json
import unittest

from pathlib import Path

from fastapi.testclient import TestClient

from flight_agent.api import create_api_app
from flight_agent.contracts import DocumentMetadata, ParseOutcome
from flight_agent.parser import extract_pdf_text, parse_extracted_text


ROOT = Path(__file__).resolve().parents[1]
PDF_ROOT = ROOT / "output" / "pdf"
GOLDEN_ROOT = ROOT / "travel_eval" / "fixtures" / "documents"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def metadata_for(filename: str, trip_id: str, fixture_id: str) -> DocumentMetadata:
    content = (PDF_ROOT / filename).read_bytes()
    return DocumentMetadata(
        trip_id=trip_id,
        traveler_ref="traveler-synthetic-001",
        fixture_id=fixture_id,
        filename=filename,
        sha256=hashlib.sha256(content).hexdigest(),
    )


class ParserGoldenTests(unittest.TestCase):
    def _assert_clean_golden(
        self, filename: str, trip_id: str, fixture_id: str, golden_name: str
    ) -> None:
        content = (PDF_ROOT / filename).read_bytes()
        outcome = parse_extracted_text(
            extract_pdf_text(content), metadata_for(filename, trip_id, fixture_id)
        )
        self.assertEqual(outcome.status, "parsed")
        self.assertIsNotNone(outcome.itinerary)
        self.assertEqual(
            outcome.itinerary.model_dump(exclude_none=True),
            load_json(GOLDEN_ROOT / golden_name),
        )
        self.assertEqual(outcome.orchestration["framework"], "crewai-flow")
        self.assertEqual(outcome.orchestration["llm_calls"], 0)

    def test_direct_pdf_matches_golden_exactly(self):
        self._assert_clean_golden(
            "synthetic_direct_eticket.pdf",
            "trip-fixture-direct",
            "doc-direct-clean",
            "expected_direct_itinerary.json",
        )

    def test_connection_pdf_matches_golden_exactly(self):
        self._assert_clean_golden(
            "synthetic_connection_itinerary.pdf",
            "trip-fixture-connection",
            "doc-connection-clean",
            "expected_connection_itinerary.json",
        )

    def test_image_only_pdf_abstains_without_inventing_fields(self):
        filename = "redacted_ambiguous_scan.pdf"
        outcome = parse_extracted_text(
            extract_pdf_text((PDF_ROOT / filename).read_bytes()),
            metadata_for(filename, "trip-fixture-ambiguous", "doc-ambiguous-scan"),
        )
        expected = load_json(GOLDEN_ROOT / "expected_ambiguous_parse.json")
        self.assertEqual(outcome.status, "review_required")
        self.assertIsNone(outcome.itinerary)
        self.assertTrue(outcome.review["review_required"])
        self.assertEqual(outcome.review["reason_codes"], expected["reason_codes"])
        self.assertEqual(outcome.review["must_not_infer"], expected["must_not_infer"])
        self.assertEqual(outcome.review["safe_partial_extraction"], {})


class FakeDocumentGateway:
    def __init__(self):
        self.calls: list[tuple[bytes, DocumentMetadata]] = []

    async def parse(
        self, document_bytes: bytes, metadata: DocumentMetadata
    ) -> ParseOutcome:
        self.calls.append((document_bytes, metadata))
        return parse_extracted_text(extract_pdf_text(document_bytes), metadata)


class UploadApiTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeDocumentGateway()
        self.client = TestClient(create_api_app(self.gateway))

    def test_pdf_upload_reaches_agent_boundary_and_returns_golden(self):
        filename = "synthetic_direct_eticket.pdf"
        content = (PDF_ROOT / filename).read_bytes()
        response = self.client.post(
            "/v1/documents/parse",
            files={"file": (filename, content, "application/pdf")},
            data={
                "trip_id": "trip-fixture-direct",
                "traveler_ref": "traveler-synthetic-001",
                "fixture_id": "doc-direct-clean",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["itinerary"],
            load_json(GOLDEN_ROOT / "expected_direct_itinerary.json"),
        )
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.gateway.calls[0][0], content)

    def test_non_pdf_is_rejected_before_agent_call(self):
        response = self.client.post(
            "/v1/documents/parse",
            files={"file": ("notes.txt", b"not a pdf", "text/plain")},
            data={
                "trip_id": "trip-fixture-direct",
                "traveler_ref": "traveler-synthetic-001",
                "fixture_id": "doc-invalid",
            },
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.gateway.calls, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import hashlib
import json
import unittest
import uuid

from pathlib import Path

from fastapi.testclient import TestClient

from flight_agent.a2a_service import create_document_agent_app


ROOT = Path(__file__).resolve().parents[1]


class A2AContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(
            create_document_agent_app("http://document-agent.test")
        )

    def test_agent_card_advertises_jsonrpc_skill(self):
        response = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(response.status_code, 200, response.text)
        card = response.json()
        self.assertIn("parse_itinerary_pdf", {skill["id"] for skill in card["skills"]})
        self.assertIn(
            ("JSONRPC", "1.0"),
            {
                (item["protocolBinding"], item["protocolVersion"])
                for item in card["supportedInterfaces"]
            },
        )

    def test_message_send_returns_structured_golden_artifact(self):
        pdf_path = ROOT / "output" / "pdf" / "synthetic_direct_eticket.pdf"
        content = pdf_path.read_bytes()
        request_id = str(uuid.uuid4())
        response = self.client.post(
            "/a2a/jsonrpc",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": str(uuid.uuid4()),
                        "contextId": str(uuid.uuid4()),
                        "role": "ROLE_USER",
                        "parts": [
                            {
                                "raw": base64.b64encode(content).decode("ascii"),
                                "mediaType": "application/pdf",
                                "filename": pdf_path.name,
                            },
                            {
                                "data": {
                                    "trip_id": "trip-fixture-direct",
                                    "traveler_ref": "traveler-synthetic-001",
                                    "fixture_id": "doc-direct-clean",
                                    "filename": pdf_path.name,
                                    "media_type": "application/pdf",
                                    "sha256": hashlib.sha256(content).hexdigest(),
                                }
                            },
                        ],
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        envelope = response.json()
        self.assertEqual(envelope["id"], request_id)
        self.assertNotIn("error", envelope)
        artifact = envelope["result"]["task"]["artifacts"][0]
        outcome = artifact["parts"][0]["data"]
        expected = json.loads(
            (
                ROOT
                / "travel_eval"
                / "fixtures"
                / "documents"
                / "expected_direct_itinerary.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(outcome["status"], "parsed")
        self.assertEqual(outcome["itinerary"], expected)


if __name__ == "__main__":
    unittest.main()

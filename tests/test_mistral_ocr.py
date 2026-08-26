from __future__ import annotations

import base64
import json
import unittest

from pathlib import Path

import httpx

from flight_agent.ocr import MistralOcrProvider, OcrError, OcrNotConfiguredError


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "travel_eval"
    / "fixtures"
    / "documents"
    / "mistral_ocr_ambiguous_response.json"
)


class MistralOcrProviderTests(unittest.TestCase):
    def test_adapter_sends_base64_pdf_and_reads_page_markdown(self):
        pdf_bytes = b"%PDF-1.7 synthetic"
        response_fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url, "https://mistral.test/v1/ocr")
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "mistral-ocr-latest")
            self.assertEqual(payload["document"]["type"], "document_url")
            encoded = payload["document"]["document_url"].split(",", 1)[1]
            self.assertEqual(base64.b64decode(encoded), pdf_bytes)
            self.assertFalse(payload["include_image_base64"])
            self.assertEqual(payload["confidence_scores_granularity"], "page")
            return httpx.Response(200, json=response_fixture)

        provider = MistralOcrProvider(
            api_key="test-key",
            base_url="https://mistral.test/v1",
            transport=httpx.MockTransport(handler),
        )
        extraction = provider.extract_pdf(pdf_bytes)
        self.assertIn("QUICKWING", extraction.text)
        self.assertEqual(extraction.provider, "mistral")
        self.assertEqual(extraction.model, "mistral-ocr-latest")
        self.assertEqual(extraction.page_count, 1)

    def test_missing_key_fails_without_a_network_call(self):
        def unexpected_call(request: httpx.Request) -> httpx.Response:
            raise AssertionError("network must not be called without a credential")

        provider = MistralOcrProvider(
            api_key="",
            transport=httpx.MockTransport(unexpected_call),
        )
        with self.assertRaises(OcrNotConfiguredError):
            provider.extract_pdf(b"%PDF-1.7 synthetic")

    def test_empty_ocr_response_is_rejected(self):
        provider = MistralOcrProvider(
            api_key="test-key",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"model": "mistral-ocr-latest", "pages": []},
                )
            ),
        )
        with self.assertRaises(OcrError):
            provider.extract_pdf(b"%PDF-1.7 synthetic")


if __name__ == "__main__":
    unittest.main()

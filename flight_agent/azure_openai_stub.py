from __future__ import annotations

import json

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "travel_eval"
    / "fixtures"
    / "documents"
    / "azure_openai_itinerary_extraction.json"
)
EXPLANATION_FIXTURE = (
    ROOT
    / "travel_eval"
    / "fixtures"
    / "communication"
    / "azure_openai_delay_explanation.json"
)
EXPECTED_KEY = "local-azure-openai-test-key"
EXPECTED_DEPLOYMENT = "fixture-gpt-deployment"
EXPECTED_API_VERSION = "2024-10-21"


def create_azure_openai_stub() -> FastAPI:
    """Contract stub for the application's narrow Azure structured-output calls."""

    app = FastAPI(title="Azure OpenAI Itinerary Stub", version="0.1.0")

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/openai/deployments/{deployment}/chat/completions")
    async def chat_completions(
        deployment: str,
        payload: dict[str, Any],
        api_version: str = Query(alias="api-version"),
        api_key: str | None = Header(default=None, alias="api-key"),
    ) -> dict[str, Any]:
        if api_key != EXPECTED_KEY:
            raise HTTPException(status_code=401, detail="Invalid test credential")
        if deployment != EXPECTED_DEPLOYMENT:
            raise HTTPException(status_code=404, detail="Unknown test deployment")
        if api_version != EXPECTED_API_VERSION:
            raise HTTPException(status_code=400, detail="Unexpected API version")

        response_format = payload.get("response_format")
        json_schema = (
            response_format.get("json_schema")
            if isinstance(response_format, dict)
            else None
        )
        if (
            not isinstance(response_format, dict)
            or response_format.get("type") != "json_schema"
            or not isinstance(json_schema, dict)
            or json_schema.get("strict") is not True
        ):
            raise HTTPException(status_code=400, detail="Strict schema is required")
        messages = payload.get("messages")
        if not isinstance(messages, list) or len(messages) != 2:
            raise HTTPException(status_code=400, detail="Unexpected messages")
        source = messages[1].get("content")
        schema_name = json_schema.get("name")
        if schema_name == "itinerary_extraction":
            if not isinstance(source, str) or "Booking ID: ZXCV12" not in source:
                raise HTTPException(
                    status_code=400, detail="Synthetic ticket is missing"
                )
            response_body = json.loads(FIXTURE.read_text(encoding="utf-8"))
            response_id = "chatcmpl-fixture-itinerary"
        elif schema_name == "disruption_explanation":
            if not isinstance(source, str):
                raise HTTPException(
                    status_code=400, detail="Disruption evidence is missing"
                )
            evidence = json.loads(source)
            if evidence.get("category") != "DELAY" or evidence.get(
                "delay_minutes"
            ) != 45:
                raise HTTPException(
                    status_code=400, detail="Synthetic delay evidence is missing"
                )
            response_body = json.loads(
                EXPLANATION_FIXTURE.read_text(encoding="utf-8")
            )
            response_id = "chatcmpl-fixture-explanation"
        else:
            raise HTTPException(status_code=400, detail="Unknown structured schema")
        return {
            "id": response_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(response_body, separators=(",", ":")),
                    },
                    "finish_reason": "stop",
                }
            ],
        }

    return app


app = create_azure_openai_stub()

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import TextContent

from flight_agent.disruption_explanation import (
    DisruptionExplanationError,
    DisruptionExplanationRequest,
    ModelDisruptionExplanation,
)
from flight_agent.eval_reasoning import EvalAdvisory
from flight_agent.itinerary_llm import ItineraryLlmError, LlmItineraryExtraction
from flight_agent.ocr import OcrError, OcrExtraction
from flight_agent.telemetry import trace_headers, traced
from flight_agent.travel_tools_auth import (
    COMMUNICATION_SCOPE,
    DOCUMENT_SCOPE,
    EVAL_SCOPE,
    tool_call_meta,
)


def _tools_url() -> str:
    return os.getenv("TRAVEL_TOOLS_MCP_URL", "http://127.0.0.1:8003/mcp")


async def _call_tool(
    url: str, name: str, arguments: dict[str, Any], scope: str
) -> dict[str, Any]:
    async with create_mcp_http_client(headers=trace_headers()) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    name,
                    arguments=arguments,
                    meta=tool_call_meta(scope),
                )
    if result.isError:
        raise RuntimeError(f"{name} MCP tool returned an error")
    payload = result.structuredContent
    if not isinstance(payload, dict):
        for part in result.content:
            if not isinstance(part, TextContent):
                continue
            try:
                candidate = json.loads(part.text)
            except ValueError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
    if not isinstance(payload, dict):
        raise RuntimeError(f"{name} MCP tool returned no structured output")
    return payload


class StreamableHttpOcrMcpClient:
    def __init__(self, url: str | None = None) -> None:
        self._url = url or _tools_url()

    @traced(
        "mcp.extract_ticket_text",
        service_name="document-agent",
        kind="tool",
        content_input=lambda self, document_bytes: {
            "document": {
                "byte_count": len(document_bytes),
                "document_ref": hashlib.sha256(document_bytes).hexdigest()[:16],
            }
        },
        content_output=lambda result: {
            "provider": result.provider,
            "model": result.model,
            "page_count": result.page_count,
            "character_count": len(result.text),
        },
    )
    def extract_pdf(self, document_bytes: bytes) -> OcrExtraction:
        try:
            payload = asyncio.run(
                _call_tool(
                    self._url,
                    "extract_ticket_text",
                    {"pdf_base64": base64.b64encode(document_bytes).decode("ascii")},
                    DOCUMENT_SCOPE,
                )
            )
        except Exception as error:
            raise OcrError("MCP OCR request failed") from error
        return OcrExtraction(
            text=str(payload["text"]),
            provider=str(payload["provider"]),
            model=str(payload["model"]),
            page_count=int(payload["page_count"]),
        )


class StreamableHttpItineraryLlmMcpClient:
    provider_name = "azure_openai"

    def __init__(self, url: str | None = None) -> None:
        self._url = url or _tools_url()
        self.model_name = os.getenv("MCP_AZURE_CHAT_MODEL_LABEL", "azure-chat")

    @traced(
        "mcp.extract_itinerary_with_llm",
        service_name="document-agent",
        kind="tool",
        content_input=lambda self, text: {
            "ticket_text_ref": hashlib.sha256(text.encode()).hexdigest()[:16],
            "character_count": len(text),
        },
        content_output=lambda result: {
            "can_parse": result.can_parse,
            "confirmation_count": len(result.confirmation_codes),
            "leg_count": len(result.legs),
            "reason_codes": result.reason_codes,
        },
    )
    def extract_itinerary(self, text: str) -> LlmItineraryExtraction:
        try:
            payload = asyncio.run(
                _call_tool(
                    self._url,
                    "extract_itinerary_with_llm",
                    {"ticket_text": text},
                    DOCUMENT_SCOPE,
                )
            )
        except Exception as error:
            raise ItineraryLlmError("MCP itinerary extraction failed") from error
        self.model_name = str(payload.get("model") or self.model_name)
        return LlmItineraryExtraction.model_validate(payload["extraction"])


class StreamableHttpDisruptionExplanationMcpClient:
    provider_name = "azure_openai"

    def __init__(self, url: str | None = None) -> None:
        self._url = url or _tools_url()
        self.model_name = os.getenv("MCP_AZURE_CHAT_MODEL_LABEL", "azure-chat")

    @traced(
        "mcp.generate_disruption_explanation",
        service_name="communication-agent",
        kind="tool",
        content_input=lambda self, request: request.model_dump(
            mode="json", exclude_none=True
        ),
        content_output=lambda result: result.model_dump(mode="json"),
    )
    def explain(
        self, request: DisruptionExplanationRequest
    ) -> ModelDisruptionExplanation:
        try:
            payload = asyncio.run(
                _call_tool(
                    self._url,
                    "generate_disruption_explanation",
                    {"request": request.model_dump(mode="json")},
                    COMMUNICATION_SCOPE,
                )
            )
        except Exception as error:
            raise DisruptionExplanationError(
                "MCP disruption explanation failed"
            ) from error
        self.model_name = str(payload.get("model") or self.model_name)
        return ModelDisruptionExplanation.model_validate(payload["explanation"])


class StreamableHttpEvalReasonerMcpClient:
    def __init__(self, url: str | None = None) -> None:
        self._url = url or _tools_url()
        self.model_name = os.getenv(
            "MCP_EVAL_MODEL_LABEL",
            os.getenv("EVAL_REASONING_MODEL", "mistral-eval"),
        )

    @traced(
        "mcp.review_disruption_decision",
        service_name="eval-agent",
        kind="tool",
        content_input=lambda self, candidate, policy, deterministic_decision: {
            "candidate": candidate,
            "policy": policy,
            "deterministic_decision": deterministic_decision,
        },
        content_output=lambda result: result.model_dump(mode="json"),
    )
    def recommend(
        self,
        candidate: dict[str, Any],
        policy: dict[str, Any],
        deterministic_decision: dict[str, Any],
    ) -> EvalAdvisory:
        payload = asyncio.run(
            _call_tool(
                self._url,
                "review_disruption_decision",
                {
                    "candidate": candidate,
                    "policy": policy,
                    "deterministic_decision": deterministic_decision,
                },
                EVAL_SCOPE,
            )
        )
        self.model_name = str(payload.get("model") or self.model_name)
        return EvalAdvisory.model_validate(payload["advisory"])

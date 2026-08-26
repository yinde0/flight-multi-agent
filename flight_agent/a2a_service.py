from __future__ import annotations

import asyncio
import os

from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value

from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
)
from fastapi import FastAPI

from flight_agent.contracts import DocumentMetadata
from flight_agent.flow import run_document_flow
from flight_agent.ocr import OcrProvider


def _request_parts(context: RequestContext) -> tuple[bytes, DocumentMetadata]:
    if not context.message:
        raise ValueError("A2A message is required")

    document_bytes: bytes | None = None
    metadata: dict | None = None
    for part in context.message.parts:
        if part.raw:
            document_bytes = bytes(part.raw)
        if part.HasField("data"):
            value = MessageToDict(part.data, preserving_proto_field_name=True)
            if isinstance(value, dict):
                metadata = value

    if not document_bytes or metadata is None:
        raise ValueError("A PDF raw part and a metadata data part are required")
    return document_bytes, DocumentMetadata.model_validate(metadata)


class ItineraryDocumentAgentExecutor(AgentExecutor):
    def __init__(self, ocr_provider: OcrProvider | None = None) -> None:
        self._ocr_provider = ocr_provider

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        if not context.message:
            raise ValueError("A2A message is required")
        task = context.current_task or new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        document_bytes, metadata = _request_parts(context)
        outcome = await asyncio.to_thread(
            run_document_flow, document_bytes, metadata, self._ocr_provider
        )
        await updater.add_artifact(
            parts=[Part(data=ParseDict(outcome, Value()))],
            name="itinerary_parse_result",
            last_chunk=True,
        )
        await updater.complete()

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        updater = TaskUpdater(
            event_queue,
            context.task_id or "",
            context.context_id or "",
        )
        await updater.cancel()


def build_agent_card(public_url: str) -> AgentCard:
    return AgentCard(
        name="Travel Document Parsing Agent",
        description=(
            "Extracts booked flight legs from text or scanned itinerary PDFs, "
            "and abstains when decision-critical fields are ambiguous."
        ),
        version="0.2.0",
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
        ),
        default_input_modes=["application/pdf", "application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="parse_itinerary_pdf",
                name="Parse itinerary PDF",
                description="Return a canonical itinerary or an explicit review request.",
                tags=["travel", "document", "itinerary", "ocr"],
                examples=["Parse the attached e-ticket PDF"],
                input_modes=["application/pdf", "application/json"],
                output_modes=["application/json"],
            )
        ],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url=f"{public_url.rstrip('/')}/a2a/jsonrpc",
            )
        ],
    )


def create_document_agent_app(
    public_url: str | None = None,
    ocr_provider: OcrProvider | None = None,
) -> FastAPI:
    resolved_url = public_url or os.getenv(
        "DOCUMENT_AGENT_PUBLIC_URL", "http://127.0.0.1:8001"
    )
    card = build_agent_card(resolved_url)
    handler = DefaultRequestHandler(
        agent_executor=ItineraryDocumentAgentExecutor(ocr_provider),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI(title="Travel Document Parsing Agent", version="0.1.0")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(
            handler,
            rpc_url="/a2a/jsonrpc",
        ),
    )

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_document_agent_app()

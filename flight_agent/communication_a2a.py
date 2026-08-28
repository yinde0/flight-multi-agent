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

from flight_agent.disruption_explanation import (
    DisruptionExplanationProvider,
    DisruptionExplanationRequest,
    explain_disruption,
    explanation_provider_from_environment,
)
from flight_agent.telemetry import install_telemetry_routes


def _request_data(context: RequestContext) -> DisruptionExplanationRequest:
    if not context.message:
        raise ValueError("A2A message is required")
    for part in context.message.parts:
        if part.HasField("data"):
            value = MessageToDict(part.data, preserving_proto_field_name=True)
            if isinstance(value, dict):
                return DisruptionExplanationRequest.model_validate(value)
    raise ValueError("A disruption explanation data part is required")


class DisruptionCommunicationAgentExecutor(AgentExecutor):
    def __init__(
        self, provider: DisruptionExplanationProvider | None = None
    ) -> None:
        self._provider = provider

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        if not context.message:
            raise ValueError("A2A message is required")
        task = context.current_task or new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        request = _request_data(context)
        result = await asyncio.to_thread(
            explain_disruption, request, self._provider
        )
        await updater.add_artifact(
            parts=[Part(data=ParseDict(result.model_dump(mode="json"), Value()))],
            name="friendly_disruption_explanation",
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


def build_communication_agent_card(public_url: str) -> AgentCard:
    return AgentCard(
        name="Travel Disruption Communication Agent",
        description=(
            "Turns an Eval-approved, PII-free disruption fact set into calm traveler "
            "language. It cannot notify, search, book, cancel, or change a verdict."
        ),
        version="0.1.0",
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
        ),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="explain_confirmed_disruption",
                name="Explain a confirmed disruption",
                description=(
                    "Create short, friendly wording from facts already approved by "
                    "the deterministic Eval policy."
                ),
                tags=["travel", "communication", "notification", "llm"],
                examples=["Explain a confirmed 45-minute delay"],
                input_modes=["application/json"],
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


def create_communication_agent_app(
    public_url: str | None = None,
    *,
    provider: DisruptionExplanationProvider | None = None,
    resolve_environment: bool = True,
) -> FastAPI:
    resolved_url = public_url or os.getenv(
        "COMMUNICATION_AGENT_PUBLIC_URL", "http://127.0.0.1:8017"
    )
    resolved_provider = (
        provider
        if provider is not None or not resolve_environment
        else explanation_provider_from_environment()
    )
    card = build_communication_agent_card(resolved_url)
    handler = DefaultRequestHandler(
        agent_executor=DisruptionCommunicationAgentExecutor(resolved_provider),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI(
        title="Travel Disruption Communication Agent", version="0.1.0"
    )
    install_telemetry_routes(app, service_name="communication-agent")
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
        return {
            "status": "ok",
            "explanation_mode": "azure" if resolved_provider else "deterministic",
        }

    return app


app = create_communication_agent_app()

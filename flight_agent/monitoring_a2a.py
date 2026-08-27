from __future__ import annotations

import asyncio
import os

from contextlib import asynccontextmanager

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
from fastapi import FastAPI, HTTPException

from flight_agent.event_delivery import publish_pending_outbox
from flight_agent.flight_status_mcp_client import (
    FlightStatusGateway,
    StreamableHttpFlightStatusMcpClient,
)
from flight_agent.monitoring_contracts import MonitoringPollRequest
from flight_agent.monitoring_events import CandidatePublisher, NatsCandidatePublisher
from flight_agent.monitoring_flow import run_monitoring_flow
from flight_agent.monitoring_store import (
    DynamoMonitoringStateStore,
    MonitoringStore,
)
from flight_agent.weather import NeutralWeatherGateway
from flight_agent.weather_mcp_client import StreamableHttpWeatherMcpClient, WeatherGateway


def _request_data(context: RequestContext) -> MonitoringPollRequest:
    if not context.message:
        raise ValueError("A2A message is required")
    for part in context.message.parts:
        if part.HasField("data"):
            value = MessageToDict(part.data, preserving_proto_field_name=True)
            if isinstance(value, dict):
                return MonitoringPollRequest.model_validate(value)
    raise ValueError("A monitoring request data part is required")


class FlightMonitoringAgentExecutor(AgentExecutor):
    def __init__(
        self,
        *,
        flight_status: FlightStatusGateway,
        weather: WeatherGateway,
        store: MonitoringStore,
        publisher: CandidatePublisher,
    ) -> None:
        self._flight_status = flight_status
        self._weather = weather
        self._store = store
        self._publisher = publisher

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
        outcome = await asyncio.to_thread(
            run_monitoring_flow,
            request,
            flight_status=self._flight_status,
            weather=self._weather,
            store=self._store,
            publisher=self._publisher,
        )
        await updater.add_artifact(
            parts=[Part(data=ParseDict(outcome, Value()))],
            name="flight_monitoring_poll_result",
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


def build_monitoring_agent_card(public_url: str) -> AgentCard:
    return AgentCard(
        name="Travel Flight Monitoring Agent",
        description=(
            "Polls flight status and airport weather through separate MCP services, "
            "diffs durable state, and publishes candidates for independent evaluation."
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
                id="poll_flight_status",
                name="Poll flight and weather evidence",
                description=(
                    "Read flight status and airport weather, compare both with durable "
                    "previous state, and publish a candidate only when evidence changed."
                ),
                tags=["travel", "flight-status", "monitoring", "mcp"],
                examples=["Poll NB204 for 2026-09-15"],
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


def create_monitoring_agent_app(
    public_url: str | None = None,
    *,
    flight_status: FlightStatusGateway | None = None,
    weather: WeatherGateway | None = None,
    store: MonitoringStore | None = None,
    publisher: CandidatePublisher | None = None,
) -> FastAPI:
    resolved_store = store or DynamoMonitoringStateStore.from_environment()
    resolved_flight_status = flight_status or StreamableHttpFlightStatusMcpClient(
        os.getenv("FLIGHT_STATUS_MCP_URL", "http://127.0.0.1:8003/mcp")
    )
    resolved_weather = weather or (
        NeutralWeatherGateway()
        if flight_status is not None
        else StreamableHttpWeatherMcpClient(
            os.getenv("WEATHER_MCP_URL", "http://127.0.0.1:8006/mcp")
        )
    )
    resolved_publisher = publisher or NatsCandidatePublisher(
        os.getenv("NATS_URL", "nats://127.0.0.1:4222")
    )
    resolved_url = public_url or os.getenv(
        "MONITOR_AGENT_PUBLIC_URL", "http://127.0.0.1:8004"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if isinstance(resolved_store, DynamoMonitoringStateStore):
            await asyncio.to_thread(resolved_store.ensure_table)
        stop = asyncio.Event()

        async def drain_candidate_outbox() -> None:
            interval = max(
                0.25, float(os.getenv("OUTBOX_RETRY_INTERVAL_SECONDS", "2"))
            )
            publish_record = getattr(resolved_publisher, "publish_record", None)
            while not stop.is_set():
                if callable(publish_record):
                    try:
                        await publish_pending_outbox(
                            store=resolved_store,
                            event_type="disruption_candidate",
                            publish=publish_record,
                        )
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    continue

        outbox_task = asyncio.create_task(drain_candidate_outbox())
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            stop.set()
            await outbox_task

    card = build_monitoring_agent_card(resolved_url)
    handler = DefaultRequestHandler(
        agent_executor=FlightMonitoringAgentExecutor(
            flight_status=resolved_flight_status,
            weather=resolved_weather,
            store=resolved_store,
            publisher=resolved_publisher,
        ),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI(
        title="Travel Flight Monitoring Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(
            handler,
            rpc_url="/a2a/jsonrpc",
        ),
    )

    @app.get("/health/live", tags=["health"])
    async def health() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(status_code=503, detail="Monitoring Agent is starting")
        return {"status": "ok"}

    @app.get("/v1/reliability/outbox", tags=["test-control"])
    async def outbox_status() -> dict[str, int]:
        if os.getenv("RELIABILITY_AUDIT_ENABLED", "false").lower() != "true":
            raise HTTPException(status_code=404, detail="Reliability audit is disabled")
        counter = getattr(resolved_store, "outbox_count", None)
        pending = (
            await asyncio.to_thread(counter, "disruption_candidate")
            if callable(counter)
            else 0
        )
        return {"candidate_outbox_pending": int(pending)}

    return app


app = create_monitoring_agent_app()

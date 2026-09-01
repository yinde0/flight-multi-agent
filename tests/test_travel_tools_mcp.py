from __future__ import annotations

import asyncio

from types import SimpleNamespace

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import RequestParams

from flight_agent import travel_tools_mcp
from flight_agent.travel_tools_auth import (
    COMMUNICATION_SCOPE,
    DOCUMENT_SCOPE,
    EVAL_SCOPE,
    MONITOR_SCOPE,
    NOTIFICATION_SCOPE,
    SEARCH_SCOPE,
    authorize_tool_call,
    tool_call_meta,
)


def context_with_meta(**values: str) -> SimpleNamespace:
    meta = RequestParams.Meta.model_validate(values)
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


def test_unified_server_registers_every_travel_tool() -> None:
    tools = asyncio.run(travel_tools_mcp.mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "get_flight_status",
        "discover_live_flight_sample",
        "get_airport_weather",
        "search_flights",
        "send_notification",
        "extract_ticket_text",
        "extract_itinerary_with_llm",
        "generate_disruption_explanation",
        "review_disruption_decision",
    }


@pytest.mark.parametrize(
    ("scope", "token_environment", "caller"),
    [
        (MONITOR_SCOPE, "TRAVEL_TOOLS_MONITOR_TOKEN", "monitor-agent"),
        (
            SEARCH_SCOPE,
            "TRAVEL_TOOLS_SEARCH_TOKEN",
            "flight-search-action-service",
        ),
        (
            NOTIFICATION_SCOPE,
            "TRAVEL_TOOLS_NOTIFICATION_TOKEN",
            "notification-action-service",
        ),
        (DOCUMENT_SCOPE, "TRAVEL_TOOLS_DOCUMENT_TOKEN", "document-agent"),
        (
            COMMUNICATION_SCOPE,
            "TRAVEL_TOOLS_COMMUNICATION_TOKEN",
            "communication-agent",
        ),
        (EVAL_SCOPE, "TRAVEL_TOOLS_EVAL_TOKEN", "eval-agent"),
    ],
)
def test_authorized_callers_retain_only_their_tool_scope(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    token_environment: str,
    caller: str,
) -> None:
    monkeypatch.setenv("TRAVEL_TOOLS_AUTH_ENABLED", "true")
    monkeypatch.setenv(token_environment, "scope-secret")
    context = context_with_meta(
        travel_tools_caller=caller,
        travel_tools_token="scope-secret",
    )
    authorize_tool_call(context, scope)  # type: ignore[arg-type]


def test_monitor_identity_cannot_send_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAVEL_TOOLS_AUTH_ENABLED", "true")
    monkeypatch.setenv("TRAVEL_TOOLS_NOTIFICATION_TOKEN", "notification-secret")
    context = context_with_meta(
        travel_tools_caller="monitor-agent",
        travel_tools_token="notification-secret",
    )
    with pytest.raises(ToolError, match="not authorized"):
        authorize_tool_call(context, NOTIFICATION_SCOPE)  # type: ignore[arg-type]


def test_clients_read_only_their_scope_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAVEL_TOOLS_MONITOR_TOKEN", "monitor-secret")
    monkeypatch.setenv("TRAVEL_TOOLS_SEARCH_TOKEN", "search-secret")
    monkeypatch.setenv("TRAVEL_TOOLS_NOTIFICATION_TOKEN", "notification-secret")
    monkeypatch.setenv("TRAVEL_TOOLS_DOCUMENT_TOKEN", "document-secret")
    monkeypatch.setenv("TRAVEL_TOOLS_COMMUNICATION_TOKEN", "communication-secret")
    monkeypatch.setenv("TRAVEL_TOOLS_EVAL_TOKEN", "eval-secret")

    assert tool_call_meta(MONITOR_SCOPE) == {
        "travel_tools_caller": "monitor-agent",
        "travel_tools_token": "monitor-secret",
    }
    assert tool_call_meta(SEARCH_SCOPE) == {
        "travel_tools_caller": "flight-search-action-service",
        "travel_tools_token": "search-secret",
    }
    assert tool_call_meta(NOTIFICATION_SCOPE) == {
        "travel_tools_caller": "notification-action-service",
        "travel_tools_token": "notification-secret",
    }
    assert tool_call_meta(DOCUMENT_SCOPE) == {
        "travel_tools_caller": "document-agent",
        "travel_tools_token": "document-secret",
    }
    assert tool_call_meta(COMMUNICATION_SCOPE) == {
        "travel_tools_caller": "communication-agent",
        "travel_tools_token": "communication-secret",
    }
    assert tool_call_meta(EVAL_SCOPE) == {
        "travel_tools_caller": "eval-agent",
        "travel_tools_token": "eval-secret",
    }

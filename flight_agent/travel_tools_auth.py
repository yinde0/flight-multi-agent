from __future__ import annotations

import os
import secrets

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError


MONITOR_SCOPE = "monitor"
SEARCH_SCOPE = "search"
NOTIFICATION_SCOPE = "notification"
DOCUMENT_SCOPE = "document"
COMMUNICATION_SCOPE = "communication"
EVAL_SCOPE = "eval"

_SCOPE_CALLERS = {
    MONITOR_SCOPE: "monitor-agent",
    SEARCH_SCOPE: "flight-search-action-service",
    NOTIFICATION_SCOPE: "notification-action-service",
    DOCUMENT_SCOPE: "document-agent",
    COMMUNICATION_SCOPE: "communication-agent",
    EVAL_SCOPE: "eval-agent",
}
_SCOPE_TOKEN_ENV = {
    MONITOR_SCOPE: "TRAVEL_TOOLS_MONITOR_TOKEN",
    SEARCH_SCOPE: "TRAVEL_TOOLS_SEARCH_TOKEN",
    NOTIFICATION_SCOPE: "TRAVEL_TOOLS_NOTIFICATION_TOKEN",
    DOCUMENT_SCOPE: "TRAVEL_TOOLS_DOCUMENT_TOKEN",
    COMMUNICATION_SCOPE: "TRAVEL_TOOLS_COMMUNICATION_TOKEN",
    EVAL_SCOPE: "TRAVEL_TOOLS_EVAL_TOKEN",
}


def tool_call_meta(scope: str) -> dict[str, str]:
    """Build private MCP metadata identifying an approved tool caller."""
    try:
        caller = _SCOPE_CALLERS[scope]
        token_env = _SCOPE_TOKEN_ENV[scope]
    except KeyError as error:  # pragma: no cover - programming error
        raise ValueError(f"Unknown Travel Tools scope: {scope}") from error
    return {
        "travel_tools_caller": caller,
        "travel_tools_token": os.getenv(token_env, ""),
    }


def authorize_tool_call(context: Context, scope: str) -> None:
    """Enforce least-privilege access after consolidating network boundaries."""
    if os.getenv("TRAVEL_TOOLS_AUTH_ENABLED", "false").lower() != "true":
        return

    try:
        expected_caller = _SCOPE_CALLERS[scope]
        token_env = _SCOPE_TOKEN_ENV[scope]
    except KeyError as error:  # pragma: no cover - programming error
        raise ToolError("Travel Tools authorization scope is invalid") from error

    expected_token = os.getenv(token_env, "")
    if not expected_token:
        raise ToolError("Travel Tools authorization is not configured")

    meta = context.request_context.meta
    extra = meta.model_extra if meta is not None else None
    supplied_caller = str((extra or {}).get("travel_tools_caller") or "")
    supplied_token = str((extra or {}).get("travel_tools_token") or "")
    if (
        supplied_caller != expected_caller
        or not supplied_token
        or not secrets.compare_digest(expected_token, supplied_token)
    ):
        raise ToolError(f"Caller is not authorized for the {scope} tool scope")

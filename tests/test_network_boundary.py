from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_unified_mcp_uses_a_dedicated_image() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    mcp = services["travel-tools-mcp"]

    assert mcp["image"] == "${MCP_IMAGE:-flight-multi-agent-mcp:local}"
    assert mcp["build"]["dockerfile"] == "Dockerfile.mcp"
    assert services["eval-agent"]["image"] == (
        "${BACKEND_IMAGE:-flight-multi-agent-backend:local}"
    )
    assert (ROOT / "Dockerfile.mcp").is_file()


def test_only_unified_mcp_service_has_external_provider_egress() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    attached = {
        name
        for name, service in services.items()
        if "tools-egress" in (service.get("networks") or [])
    }
    assert attached == {"travel-tools-mcp"}


def test_provider_secrets_and_direct_mode_are_scoped_to_mcp() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    tools_environment = services["travel-tools-mcp"]["environment"]
    assert tools_environment["EXTERNAL_CALLS_PROVIDER"] == "direct"
    for name in ("document-agent", "eval-agent", "communication-agent"):
        environment = services[name]["environment"]
        assert environment["EXTERNAL_CALLS_PROVIDER"] == "mcp"
        assert "MISTRAL_API_KEY" not in environment
        assert "AZURE_OPENAI_API_KEY" not in environment


def test_langsmith_collector_forwards_to_mcp_not_the_public_api() -> None:
    overlay = yaml.safe_load(
        (ROOT / "compose.langsmith.yaml").read_text(encoding="utf-8")
    )
    collector = overlay["services"]["otel-collector"]
    assert collector["environment"]["MCP_OTEL_RELAY_ENDPOINT"] == (
        "http://travel-tools-mcp:8003/otel/v1/traces"
    )
    assert "LANGSMITH_API_KEY" not in collector["environment"]

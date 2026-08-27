from __future__ import annotations

from flight_agent import telemetry


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


def test_content_capture_requires_explicit_development_mode(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_TRACE_CONTENT_ENABLED", "true")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    assert telemetry.development_content_capture_enabled() is False

    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "development")
    assert telemetry.development_content_capture_enabled() is True

    monkeypatch.setenv("OTEL_TRACE_CONTENT_ENABLED", "false")
    assert telemetry.development_content_capture_enabled() is False


def test_development_content_maps_to_langsmith_input_and_output(
    monkeypatch,
) -> None:
    monkeypatch.setattr(telemetry, "_content_capture_enabled", True)
    span = FakeSpan()

    telemetry._set_span_content(
        span,
        "input",
        {"instruction": "Parse the synthetic itinerary", "trip": "synthetic"},
    )
    telemetry._set_span_content(
        span,
        "output",
        {"status": "parsed", "flight_number": "NB204"},
    )

    assert "Parse the synthetic itinerary" in str(
        span.attributes["gen_ai.prompt.0.content"]
    )
    assert "NB204" in str(span.attributes["gen_ai.completion.0.content"])
    assert (
        span.attributes["langsmith.metadata.content_capture"]
        == "development_explicit"
    )


def test_content_is_not_attached_when_capture_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_content_capture_enabled", False)
    span = FakeSpan()

    telemetry._set_span_content(span, "input", {"secret": "must-not-appear"})

    assert span.attributes == {}

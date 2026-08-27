from __future__ import annotations

import threading

from fastapi import FastAPI, Request, Response


class OtlpAuditSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._batches = 0
        self._bytes = 0

    def record(self, size: int) -> None:
        with self._lock:
            self._batches += 1
            self._bytes += size

    def audit(self) -> dict[str, int]:
        with self._lock:
            return {
                "accepted_trace_batches": self._batches,
                "accepted_trace_bytes": self._bytes,
            }


def create_otel_sink_app() -> FastAPI:
    sink = OtlpAuditSink()
    app = FastAPI(title="OTLP Contract Sink", version="0.1.0")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/traces")
    async def traces(request: Request) -> Response:
        body = await request.body()
        if not body:
            return Response(status_code=400)
        sink.record(len(body))
        return Response(status_code=200)

    @app.get("/v1/telemetry/audit")
    async def audit() -> dict[str, int]:
        return sink.audit()

    return app


app = create_otel_sink_app()

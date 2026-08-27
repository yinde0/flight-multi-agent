# Vertical Slice 10: End-to-End Distributed Trace Correlation

## Outcome

This slice makes one synthetic trip visible as one distributed trace across the
whole application. The trace begins at the public HTTP request and continues
through A2A calls, a later scheduler tick, MCP tools, durable JetStream events,
Eval, notification, and read-only search.

Trace correlation is diagnostic metadata only. It cannot approve a decision,
change a disruption candidate, or authorize a traveler action.

## Architecture

```text
Travel API HTTP request (creates trace)
  -> Trip Orchestrator A2A
     -> Document Agent A2A / document.parse
     -> Postgres trip + W3C trace context

Later virtual-clock tick
  -> scheduler.poll_leg (restores the trip trace context)
     -> Monitoring Agent A2A / monitoring.poll
        -> Flight-status MCP
        -> Weather MCP
        -> DynamoDB candidate + outbox + trace context
        -> JetStream publish / Eval consume
           -> deterministic evaluation.decide
           -> optional evaluation.crewai_advisory
           -> DynamoDB decision + outbox + trace context
           -> JetStream publish / action consumes
              -> Notification MCP
              -> Read-only flight-search MCP
```

## Context propagation

- HTTP and A2A clients inject the W3C `traceparent` and optional `tracestate`
  headers. Every application and MCP HTTP server extracts them.
- The public API returns `X-Trace-Id`, which gives a developer the correlation
  key without exposing prompt or traveler content.
- Trip activation stores the small W3C carrier in Postgres. A scheduler worker
  can therefore continue the activation trace minutes or hours later, including
  after a process restart.
- Candidate and confirmed-event outboxes store the carrier with the event.
  Publication retries and JetStream redelivery continue the original lineage.
- Consumers extract the NATS headers before starting their consume span.
- Baggage is deliberately not propagated. This prevents arbitrary user or
  traveler metadata from silently crossing service boundaries.

The trace carrier is not used as an idempotency key. Existing trip, poll, event,
decision, notification, and search identifiers remain the authoritative
deduplication controls.

## Privacy boundary

Normal traces contain operation names, outcomes, timings, and safe correlation
attributes—not PDF text, confirmation codes, provider payloads, prompts, model
completions, traveler content, or secrets. Tracing is optional and business work
continues if the collector is unavailable.

Prompt input/output capture requires both the development overlay and a
development deployment environment. The end-to-end runner uses only checked-in
synthetic evidence and never prints traced content to the terminal.

## End-to-end LangSmith proof

Set the existing LangSmith and Mistral variables in `.env`, then run:

```powershell
docker compose -f compose.yaml -f compose.langsmith.yaml -f compose.langsmith-development.yaml -f compose.eval-reasoning.yaml -f compose.trace-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_langsmith_end_to_end_trace.py
```

The runner activates the synthetic direct itinerary, submits a baseline tick and
a cancellation tick, repeats both ticks, and queries the configured LangSmith
project. It first attempts direct lookup with the returned W3C ID. If LangSmith's
OTLP ingestion assigns a different stored trace UUID, it requires exactly one
current-window trace containing activation, scheduler, monitoring, Eval,
notification, and search anchors, then fetches that complete trace. Ambiguous or
split groups fail. The complete trace must contain every HTTP, document,
scheduler, monitoring, MCP, messaging, deterministic Eval, CrewAI advisory,
notification, and search span. The runner also requires tick claim counts
`[1, 0, 1, 0]`, exactly one notification, exactly one search, and visible
development-only Eval input and output fields.

Restore the ordinary privacy-safe stack afterward:

```powershell
docker compose -f compose.yaml up -d --wait --remove-orphans
```

## Deliberate limitations

- Persisting a root trace across a trip can create a long-lived trace. A
  production deployment may choose span links or separate poll traces if its
  observability backend imposes duration or span-count limits.
- The trace test queries LangSmith and therefore needs network access and an
  account. Unit tests cover propagation and retry behavior without an external
  service.
- The W3C trace ID returned by the API is the portable application correlation
  key. In the tested LangSmith OTLP ingestion path, the stored LangSmith trace
  UUID differed; the acceptance runner reports whether direct mapping occurred
  and otherwise uses the strict anchor-group lookup described above.
- Trace context storage has no effect on retention policy for trip or event
  records; production retention and deletion controls must cover it with its
  parent record.

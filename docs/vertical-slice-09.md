# Vertical Slice 09: Observable Operations and Controlled Recovery

## Outcome

This slice makes a failed traveler action visible and recoverable without
weakening the Eval Agent's authority boundary. It adds three complementary
capabilities:

1. privacy-safe OpenTelemetry traces for document parsing, monitoring,
   evaluation, notification, search, and operator re-drive;
2. Prometheus operational metrics with a provisioned Grafana dashboard and
   delivery-health alert rules;
3. an authenticated operations API that can inspect and re-drive a quarantined
   event from its original stored evidence.

The normal application path does not depend on any telemetry backend. Export is
off by default, batched when enabled, and allowed to fail without stopping
document, monitoring, evaluation, or action processing.

## Architecture

```text
document / monitor / eval / notification / search
             |                  |
             | OTLP spans       | Prometheus metrics
             v                  v
      OpenTelemetry collector   Prometheus -> Grafana + alerts
             |
             `-> LangSmith (optional)

DynamoDB dead-letter record -> Operations service -> JetStream
          read-only list          |  authenticated re-drive
                                  `-> original consumer
                                      -> re-check Eval decision
                                      -> idempotent provider action
```

LangSmith is an optional trace destination, not the system of record and not an
agent. The checked-in collector configuration follows LangSmith's documented
OTLP endpoint and headers. LangSmith documents both generic OpenTelemetry
ingestion and CrewAI tracing through its OpenTelemetry integration:

- [Trace with OpenTelemetry](https://docs.langchain.com/langsmith/trace-with-opentelemetry)
- [Trace CrewAI applications](https://docs.langchain.com/langsmith/trace-with-crewai)

Prometheus and Grafana answer operational questions—queue backlog, dead
letters, outboxes, and outcome rates. LangSmith answers execution questions—
which boundary ran, how long it took, and whether it succeeded. Neither one is
permitted to approve a notification or rebooking search.

## Trust and privacy boundary

- `OTEL_TRACING_ENABLED` and `CREWAI_OTEL_ENABLED` default to false.
- CrewAI content capture is forced off in Python and Compose with
  `TRACELOOP_TRACE_CONTENT=false`.
- Custom spans export only low-cardinality outcomes and hashed correlation
  references. They do not attach document text, OCR output, confirmation codes,
  notification content, API keys, or full event payloads.
- LangSmith configuration lives in an optional Compose overlay. Its API key is
  injected at runtime and is never checked into the repository.
- The operations service is exposed only on Docker's private network in the
  normal stack. Operator endpoints return 503 until an explicit
  `OPS_API_TOKEN` is injected.
- `/metrics` contains aggregate state and no traveler payloads. Dead-letter
  inspection requires the operator token because it returns the authoritative
  stored event evidence.

LangSmith also supports server-side input/output masking, but this application
prevents content from being sent in the first place. That is the stronger
default for travel documents. See [LangSmith input/output masking](https://docs.langchain.com/langsmith/mask-inputs-outputs).

## Controlled re-drive

The operations API accepts only an operator identity, a reason, and a unique
request ID:

```http
POST /v1/operations/dead-letters/{consumer}/{event_id}/redrive
X-Ops-Token: <runtime secret>

{
  "request_id": "incident-2026-08-27-001",
  "operator_ref": "operator:on-call",
  "reason": "Notification provider recovered after the declared incident."
}
```

It deliberately does **not** accept replacement event content. The service:

1. reads the original dead-letter payload from DynamoDB;
2. validates that its candidate/decision identity matches the requested event;
3. atomically claims the `(consumer, event, request)` operation;
4. republishes the validated envelope with a unique JetStream message ID;
5. records who requested it and why;
6. marks the dead letter inactive only after publication is acknowledged.

The receiving notification or search service still re-reads the persisted Eval
decision and uses its existing idempotency record. A re-drive therefore cannot
turn a suppressed event into an approved action, and a repeated request cannot
produce a second traveler-visible consequence.

Operator routes are:

| Route | Purpose |
|---|---|
| `GET /v1/operations/status` | Outbox, dead-letter, and durable-consumer state |
| `GET /v1/operations/dead-letters/{consumer}` | Active quarantined evidence |
| `POST /v1/operations/dead-letters/{consumer}/{event_id}/redrive` | Claim and publish an authoritative event |

## Metrics and alerts

| Metric | Meaning |
|---|---|
| `travel_operation_executions_total{operation,outcome}` | Completed agent/action boundaries |
| `travel_trace_export_enabled` | OTLP export configured in that process |
| `travel_crewai_instrumentation_enabled` | Privacy-safe CrewAI instrumentation active |
| `travel_outbox_pending{event_type}` | Stored events awaiting broker acknowledgement |
| `travel_dead_letters_active{consumer}` | Quarantined events awaiting resolution |
| `travel_jetstream_consumer_pending{consumer}` | Messages not yet delivered |
| `travel_jetstream_consumer_ack_pending{consumer}` | Delivered messages awaiting ACK |

The optional Prometheus rules alert when a dead letter remains active for one
minute, or an outbox/consumer backlog remains for two minutes. These short
thresholds are development defaults and must be tuned to production traffic and
on-call policy.

## Deterministic golden test

The Docker test injects a notification-provider outage, while search remains
healthy. It proves three failed notification attempts become one active dead
letter and zero provider calls. It then rejects an unauthenticated re-drive,
repairs the provider, performs one authenticated re-drive, and requires exactly
one notification plus the already completed search. A duplicate re-drive must
be a no-op. A local OTLP sink proves a real trace batch was exported without
requiring an internet account.

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.operations-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_operations_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.operations-test.yaml down
```

Expected invariants are versioned in
`travel_eval/fixtures/monitoring/vertical_09_expected.json`:

| Invariant | Expected |
|---|---:|
| Notification retry count | 3 |
| Active notification dead letters before repair | 1 |
| Provider calls before repair | 0 |
| Unauthenticated re-drive | HTTP 401 |
| Authorized re-drive | `published` |
| Final notification/search status | `delivered` / `completed` |
| Duplicate re-drive | `already_redriven` |
| New provider deliveries | exactly 1 |
| Active dead letters and pending outboxes after repair | 0 |
| OTLP trace batch exported | true |

## Running the optional observability backends

For local Prometheus and Grafana only:

```powershell
$env:OPS_API_TOKEN = "replace-with-a-local-secret"
$env:GRAFANA_ADMIN_PASSWORD = "replace-with-a-local-password"
docker compose -f compose.yaml -f compose.observability.yaml up --build -d --wait
```

Open Prometheus at `http://localhost:9090` and Grafana at
`http://localhost:3000`. The `Travel disruption operations` dashboard and
Prometheus datasource are provisioned automatically.

To add LangSmith trace export:

```powershell
$env:LANGSMITH_API_KEY = "your-runtime-key"
$env:LANGSMITH_PROJECT = "flight-multi-agent"
docker compose -f compose.yaml -f compose.observability.yaml -f compose.langsmith.yaml up --build -d --wait
```

`LANGSMITH_ENDPOINT` selects the account region using the standard LangSmith
variable. Compose derives the OTLP trace URL from it. Use
`LANGSMITH_OTEL_ENDPOINT` only when a self-hosted installation requires a full
custom OTLP route. Stop the optional stack with the same file list and the
`down` command.

### Development prompt, input, and output tracing

The document flow currently makes zero LLM calls, so it has no genuine model
prompt or completion. The development overlay instead maps the deterministic
flow instruction and input to LangSmith `inputs`, and the canonical parse result
to `outputs`. CrewAI content instrumentation is also enabled, so a future
LLM-backed CrewAI Task will expose its real prompt and completion.

This capability requires both an explicit content flag and
`DEPLOYMENT_ENVIRONMENT=development`. Production forces content capture off even
if the content flag is accidentally supplied. Use only synthetic or
appropriately redacted development evidence:

```powershell
docker compose -f compose.yaml -f compose.langsmith.yaml -f compose.langsmith-development.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_langsmith_document_trace.py
```

The runner uploads only `synthetic_direct_eticket.pdf`, verifies that
`document.parse` appears in the configured LangSmith project, and checks that
both inputs and outputs are visible without printing them in the terminal.
Remove the development overlay and recreate the normal stack after inspection:

```powershell
docker compose -f compose.yaml up -d --wait --remove-orphans
```

The content string is capped by `OTEL_TRACE_CONTENT_MAX_CHARS` (12,000 by
default). The cap limits trace size; it is not a redaction mechanism.

## Deliberate limitations

- The operator boundary uses one static bearer-style token. Production should
  put the private service behind workload identity or OIDC, role-based access,
  TLS, rate limits, and centralized audit retention.
- There is an API and dashboard, but no operator user interface for reviewing
  evidence and requesting a re-drive.
- The checked-in alerts are evaluated by Prometheus but no Alertmanager routing
  is configured.
- Grafana's local named volume and administrator account are development
  conveniences, not a production deployment model.
- Re-drive publication proves that the event returned to its consumer. Provider
  success remains visible in the action record and metrics; an event that again
  exhausts retries becomes active in the dead-letter store again.

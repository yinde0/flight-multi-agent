# Travel Disruption Evaluation Package

[![CI](https://github.com/yinde0/flight-multi-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/yinde0/flight-multi-agent/actions/workflows/ci.yml)

This repository starts with the testable specification for an itinerary parsing and disruption-monitoring system. Its executable vertical paths now parse native-text and image-only PDFs, then statefully monitor flight-status changes and evaluate whether they are significant.

## Traveler frontend

The customer-facing Streamlit app lets a traveler upload a PDF ticket, activate
monitoring, and see the parsed itinerary and current trip state. It is available
at `http://localhost:8501` when the normal Docker stack is running:

```powershell
docker compose up -d --build --wait
```

### Build and deployment boundaries

The repository has one production Compose definition and two application build
artifacts:

- `Dockerfile.backend` builds one shared Python image used by every API, agent,
  action service, webhook, orchestrator, and MCP process. Compose supplies a
  different startup command for each process.
- `Dockerfile.frontend` builds only the Streamlit experience.
- `compose.yaml` is the production deployment definition. Files named
  `compose.*-test.yaml` and the other development overlays only replace provider
  configuration for repeatable local evaluation; deployment pipelines must not
  include them.

`BACKEND_IMAGE` and `FRONTEND_IMAGE` may be set to immutable registry tags. A
frontend release can then update only `traveler-ui`:

```powershell
docker compose pull traveler-ui
docker compose up -d --no-deps --no-build traveler-ui
```

The backend release uses the same Compose file but selects only backend services,
so it does not recreate `traveler-ui`. CodeBuild and CodeDeploy automation for
those two release paths is intentionally kept separate.

Run its focused expected-output check with:

```powershell
uv sync --extra app --extra test --extra ui
.\.venv\Scripts\python.exe tools\run_vertical_frontend_test.py
```

The frontend only calls `travel-api`; it does not receive provider or storage
credentials. See [docs/vertical-slice-12.md](docs/vertical-slice-12.md) for the
customer journey, test contract, and deliberate limitations.

The upload form also supports explicitly consented operational SMS alerts. The
phone number is stored separately from public trip data and reaches the Twilio
provider only after Eval approves a disruption. See
[docs/vertical-slice-13.md](docs/vertical-slice-13.md) for credentials, the
Twilio-stub vertical test, and production limitations.

SMS delivery is reconciled separately from provider submission. A dedicated
webhook service verifies `X-Twilio-Signature`, resolves the provider Message SID
through a DynamoDB reverse index, and advances delivery state without allowing
duplicate or out-of-order callbacks to regress a terminal result. Run the
container proof with:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.activation-test.yaml -f compose.twilio-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_vertical_sms_delivery_test.py
docker compose -f compose.yaml up -d --wait --remove-orphans
```

See [docs/vertical-slice-14.md](docs/vertical-slice-14.md) for the callback
security boundary and production ingress requirements.

For missing SMS, safe provider error codes, retry behavior, and Twilio's trial
restrictions, see [SMS delivery troubleshooting](docs/sms-delivery-troubleshooting.md).
An approved alert or prepared message is not proof of delivery.

## Manual flight agency demo

The flight agency sandbox makes the complete product demonstrable without waiting
for a real airline disruption. In an isolated Docker overlay, the Streamlit app can
manually change a synthetic flight's gate, delay, status, cancellation, or
diversion and then run that revision through the real Monitoring Agent, MCP,
DynamoDB diff, NATS, Eval Agent, notification, and read-only search path.

```powershell
docker compose -f compose.yaml -f compose.agency-demo.yaml up -d --build --wait --remove-orphans
# Open http://localhost:8501 and upload output/pdf/synthetic_direct_eticket.pdf
.\.venv\Scripts\python.exe tools\run_vertical_agency_demo.py
```

The overlay defaults to a recording notifier and cannot send an SMS unless
`DEMO_NOTIFICATION_PROVIDER=twilio` is explicitly set. Restore the normal live
provider stack with `docker compose -f compose.yaml up -d --wait --remove-orphans`.
See [docs/vertical-slice-15.md](docs/vertical-slice-15.md) for the operator journey,
expected decisions, and safety boundaries.

The package defines the contracts those components must satisfy and keeps
deterministic scenario replay as the acceptance boundary for every application
vertical slice.

## Document vertical slices

The first application path is intentionally thin and testable:

```text
PDF upload -> Travel API -> A2A SendMessage -> Document Agent
                                      | native text exists -> deterministic parser
                                      | no text layer      -> Mistral OCR -> parser
           <- canonical itinerary, safe partial, or explicit review request <-
```

- The API discovers the document agent using `/.well-known/agent-card.json`.
- The PDF and metadata cross a real A2A 1.0 JSON-RPC boundary.
- A CrewAI Flow extracts the native PDF text layer first and calls Mistral OCR only
  for image-only PDFs.
- Clean documents are compared exactly with checked-in golden JSON.
- The ambiguous scan recovers only safe fields and still abstains on redacted or
  visibly uncertain values.
- OCR failures fail closed and return review reason codes instead of guessed data.

See [docs/vertical-slice-01.md](docs/vertical-slice-01.md) for the original text-layer
slice and [docs/vertical-slice-02.md](docs/vertical-slice-02.md) for Mistral OCR.

Run the containerized slice and compare the actual output with the goldens:

```powershell
docker compose -f compose.yaml -f compose.test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_document_test.py
docker compose -f compose.yaml -f compose.test.yaml down
```

The test overlay runs a private Mistral-compatible contract stub, so the golden suite
does not need an API key, spend credits, or send fixture documents outside Docker.
For a real Mistral call, copy `.env.example` to `.env`, set `MISTRAL_API_KEY`, and use
`docker compose up --build -d --wait` without the test overlay.

### Azure OpenAI document fallback

The highest-impact LLM path is an evidence-checked fallback for unfamiliar ticket
layouts. The CrewAI Document Flow first runs the deterministic parser. Only a
`review_required` result may call Azure OpenAI, and its structured response must
quote ticket evidence, meet the confidence threshold, and pass canonical schema
and timestamp validation. Any failure still requests human review.

Run the complete OCR -> deterministic abstention -> Azure-compatible fallback
with local contract stubs and zero external model calls:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.document-llm-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_vertical_document_llm_test.py
docker compose -f compose.yaml up -d --wait --remove-orphans
```

Real use requires `DOCUMENT_LLM_MODE=fallback`, the Azure endpoint and API key,
and `AZURE_OPENAI_DEPLOYMENT`. See
[docs/vertical-slice-16.md](docs/vertical-slice-16.md).

## Stateful monitoring vertical slice

The monitoring path now combines independently sourced flight and weather evidence:

```text
Travel API -> A2A Monitoring Agent -> Travel Tools MCP: get_flight_status
                                  |  -> Travel Tools MCP: get_airport_weather
                                     -> DynamoDB last-known flight + weather state
                                     -> NATS disruption_candidate -> Eval Agent
                                                                   -> disruption_confirmed
```

Run the deterministic six-poll timeline, restart the Monitoring Agent after its
baseline, and compare the real output with the checked-in golden result:

```powershell
docker compose -f compose.yaml -f compose.test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_monitoring_test.py --restart-monitor
docker compose -f compose.yaml -f compose.test.yaml down
```

The expected policy behavior is: no event for unchanged state, suppress a
gate-only change, suppress a 20-minute delay, confirm a 45-minute delay exactly
once, and do not re-alert when that status remains unchanged. See
[docs/vertical-slice-03.md](docs/vertical-slice-03.md) for the architecture,
golden table, live AviationStack command, and failure rules.

Run the seven-poll weather-corroboration timeline with a Monitoring Agent restart:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.weather-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_weather_test.py --restart-monitor
docker compose -f compose.yaml -f compose.test.yaml -f compose.weather-test.yaml down
```

It suppresses light rain, severe weather without flight impact, repeated state,
and a weather-cleared update; it confirms one 45-minute delay with severe weather
as corroborating evidence and continues flight-only when weather is unavailable.
See [docs/vertical-slice-04.md](docs/vertical-slice-04.md).

## Post-Eval notification vertical slice

The next boundary consumes only `disruption_confirmed`, re-verifies the stored
Eval decision, and calls the notification-scoped `send_notification` tool on the
internal Travel Tools MCP server. Its test provider is a recording sink and
cannot contact a real traveler.

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.notification-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_notification_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.notification-test.yaml down
```

The golden requires exactly one recorded delivery for the approved delay and no
delivery for every suppressed or unchanged poll. See
[docs/vertical-slice-05.md](docs/vertical-slice-05.md).

### Friendly post-Eval explanations

An A2A Communication Agent now turns only Eval-approved, PII-free disruption
facts into calm traveler language. Azure OpenAI writes the explanation but has
no authority or tools: it cannot notify, search, rebook, cancel, or pay. Local
validation rejects invented numbers and claims about rebooking, refunds,
compensation, guarantees, or URLs; any model or agent failure immediately uses
the deterministic message and does not block the authorized notification.

Run the complete flight-agency -> Eval -> Communication Agent -> Notification
vertical with a local Azure-compatible stub:

```powershell
docker compose -f compose.yaml -f compose.agency-demo.yaml -f compose.communication-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_vertical_communication_test.py
```

The traveler UI displays the friendly message, and development LangSmith traces
show `agent.communication.explain_disruption` with its safe input and output.
Real Azure generation activates automatically when the endpoint, key, and either
`AZURE_OPENAI_DEPLOYMENT` or `CHAT_DEPLOYMENT` are present. See
[docs/vertical-slice-17.md](docs/vertical-slice-17.md).

## Read-only rebooking-search vertical slice

`NOTIFY_AND_SEARCH` now wakes a separate search action service. It re-verifies
the Eval decision in DynamoDB, reads the disrupted leg, and calls the
search-scoped `search_flights` tool on the internal Travel Tools MCP server. The
normal provider uses Duffel for priced, expiring offers; the checked-in
deterministic replay remains the exact golden evaluation.

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.search-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_search_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.search-test.yaml down
```

The cancellation golden requires one notification and one search. Seven raw
schedule candidates become two ranked alternatives after route, time-window,
stop-count, chronology, connection, and original-flight filters. The replay
correctly states `availability_verified: false`; every provider mode fixes
`booking_guaranteed: false` and `booking_authorized: false`. See
[docs/vertical-slice-06.md](docs/vertical-slice-06.md).

For a networked Duffel test, place `DUFFEL_TOKEN` in `.env` and run:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.duffel-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_duffel_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.duffel-test.yaml down
```

Duffel test tokens are reported as `provider_test_offers`; only live-mode
responses are reported as `live_offers`. Both retain
`booking_guaranteed: false` and `booking_authorized: false`.

## Persisted trip activation and continuous scheduling

Vertical slice 07 connects document parsing to continuous monitoring instead of
requiring a person to submit each poll. A light Trip Orchestrator stores the
immutable source PDF in S3-compatible storage, writes the parsed itinerary and
per-leg schedule to Postgres, and calls the existing Monitoring Agent only when a
leg is due.

```text
PDF -> Travel API -> Trip Orchestrator -> S3-compatible document storage
                                  |----> A2A Document Agent
                                  |----> Postgres trip + due-leg schedule
                                  `----> A2A Monitoring Agent when due
                                                `-> existing Eval/actions path
```

Run the virtual-clock golden and restart the orchestrator between the baseline
and cancellation polls:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.activation-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_activation_test.py --restart-orchestrator
docker compose -f compose.yaml -f compose.test.yaml -f compose.activation-test.yaml down
```

The test requires an idempotent repeated upload, a checksum-verifiable PDF in
object storage, two persisted polls across the restart, zero work on duplicate
ticks, and exactly one notification plus one read-only rebooking search. See
[docs/vertical-slice-07.md](docs/vertical-slice-07.md).

## Durable event delivery and outage recovery

Vertical slice 08 replaces the Core NATS event path with a file-backed
JetStream stream and three explicit-ACK durable consumers. DynamoDB
transactions store each candidate or approved confirmation with an outbox
record before publication. A publish acknowledgement removes the outbox;
temporary broker failure leaves it available for retry.

```text
Dynamo candidate + outbox -> JetStream -> durable Eval consumer
Dynamo decision + outbox  -> JetStream -> durable notification consumer
                                      `-> durable search consumer
```

Run the staged outage golden, which stops NATS and the consumers, restarts NATS
twice, forces a confirmed-event redelivery, and checks actual provider call
counts:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.reliability-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_reliability_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.reliability-test.yaml down
```

The transport remains correctly described as at-least-once. Stable event IDs,
persisted terminal action results, and idempotency checks make redelivery produce
one traveler-visible consequence in the tested path. See
[docs/vertical-slice-08.md](docs/vertical-slice-08.md).

## Observable operations and controlled recovery

Vertical slice 09 adds privacy-safe OpenTelemetry traces, Prometheus metrics, an
optional provisioned Grafana dashboard, and optional LangSmith export. A private
operations service can inspect active dead letters and re-drive only their
original persisted payload after token authentication; notification and search
still re-check Eval authority and their idempotency records.

Run the deterministic provider-outage and recovery test:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.operations-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_operations_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.operations-test.yaml down
```

The golden requires three exhausted action attempts, one visible dead letter,
zero provider calls before repair, HTTP 401 without the operator credential,
one successful authenticated re-drive, exactly one traveler-visible delivery,
no duplicate delivery, zero final outboxes/dead letters, and an exported OTLP
trace batch. See [docs/vertical-slice-09.md](docs/vertical-slice-09.md).

For prompt/input/output inspection during development, stack
`compose.langsmith-development.yaml` on the LangSmith overlay and run
`tools/run_langsmith_document_trace.py`. The switch is ignored outside the
`development` deployment environment and should be used only with synthetic or
redacted evidence.

LangSmith now keeps only named agents and MCP tools with both meaningful input
and output. HTTP routes, ports, broker wrappers, and empty runs are excluded;
trace context still connects the agents. See [focused tracing](docs/langsmith-tracing.md)
and run `tools/run_langsmith_filter_test.py` for a non-SMS verification against
your configured LangSmith project. Historical traces are unchanged.

## Distributed trace correlation

Vertical slice 10 carries one W3C trace across HTTP/A2A, persisted scheduler
work, MCP calls, transactional outboxes, and JetStream consumers. The trace
context is persisted with the trip and each outbox so a later scheduler tick or
publication retry retains its lineage. Baggage is not propagated, and trace
metadata never grants action authority.

Run the synthetic activation-to-action proof against the configured LangSmith
project:

```powershell
docker compose -f compose.yaml -f compose.langsmith.yaml -f compose.langsmith-development.yaml -f compose.eval-reasoning.yaml -f compose.trace-test.yaml up -d --build --wait --remove-orphans
.\.venv\Scripts\python.exe tools\run_langsmith_end_to_end_trace.py
docker compose -f compose.yaml up -d --wait --remove-orphans
```

The runner requires all agent and MCP spans in one trace, verifies every
agent/tool input/output, and rejects transport or empty runs. It also proves duplicate
ticks create no second notification or search. See
[docs/vertical-slice-10.md](docs/vertical-slice-10.md).

## CrewAI Eval shadow reasoning

Vertical slice 11 adds a tool-free CrewAI/Mistral reviewer after the deterministic
Eval policy. It receives only allowlisted disruption evidence and returns a strict
Pydantic advisory. The model cannot change the stored verdict or call an action;
failures and disagreements are audit evidence only.

Run all golden decisions through the real model:

```powershell
.\.venv\Scripts\python.exe tools\run_vertical_eval_reasoning_test.py
```

Normal Compose keeps reasoning off. `compose.eval-reasoning.yaml` explicitly
enables shadow mode and requires `MISTRAL_API_KEY`. See
[docs/vertical-slice-11.md](docs/vertical-slice-11.md).

## What is included

- Canonical JSON Schemas for itineraries, observations, deltas, disruption candidates, decisions, approved notification actions, and authorized read-only searches.
- A versioned suppression policy with explicit thresholds and safety invariants.
- Three synthetic PDF fixtures: a clean direct trip, a clean connection, and an ambiguous raster scan that must trigger review.
- Six replay scenarios covering unchanged state, gate churn, escalating delay, cancellation replay, connection risk, and uncorroborated weather risk.
- Curated expected candidates, decisions, notifications, and ignored stale observations.
- A virtual-clock runner that never waits for wall-clock time.
- Release metrics and acceptance thresholds.
- A failure and chaos-test matrix covering implemented and planned infrastructure
  behavior.
- End-to-end W3C trace correlation across delayed and durable work.
- A versioned, schema-constrained CrewAI/Mistral Eval advisory with golden
  agreement metrics and no action authority.
- An optional Azure OpenAI itinerary-extraction fallback with quoted evidence,
  deterministic validation, and fail-closed human review.
- A PII-isolated A2A Communication Agent that uses Azure OpenAI only for
  friendly post-Eval wording and fails over to deterministic messages.

## Run the evaluation

From the repository root:

```powershell
uv run python -m travel_eval.runner
uv sync --extra app --extra test
uv run python -m pytest -q
```

The CLI exits non-zero when an automated acceptance threshold fails. Use `--show-results` to inspect the full derived evidence:

```powershell
python -m travel_eval.runner --show-results
```

## Continuous integration

The GitHub Actions workflow in `.github/workflows/ci.yml` runs for every pull
request to `main`, every push to `main`, and manual dispatches. It requires:

- Ruff lint checks.
- The complete pytest unit suite.
- The golden replay acceptance thresholds.
- Validation of the production Compose definition and every replay overlay.
- Successful builds of both Docker images.

To prevent unverified changes from reaching `main`, configure a GitHub branch
ruleset for `main` that requires a pull request and the following status checks:
`Lint`, `Unit and golden tests`, `Compose validation`, and `Container builds`.
Direct pushes must be disabled in the ruleset; a workflow can report a failed
push, but only repository rules can reject it before it reaches `main`.

## Regenerate PDF fixtures

PDFs are deterministic artifacts generated from synthetic content:

```powershell
python tools/generate_pdf_fixtures.py
```

The generator updates `travel_eval/fixtures/documents/manifest.json` with SHA-256 hashes. PDF regeneration is separate from the golden replay runner.

## Directory map

```text
travel_eval/
  schemas/                 Canonical component boundaries
  policies/                Versioned suppression rules
  fixtures/documents/      Expected document parsing results and manifest
  fixtures/scenarios/      Inputs plus curated golden outcomes
  fixtures/monitoring/     Stateful flight-status timeline and expected decisions
  fixtures/search/         Synthetic schedule candidates for search evaluation
  clock.py                 Virtual clock
  engine.py                Normalization, diffing, scoring, and replay
  policy.py                Deterministic significance policy
  metrics.py               Release metrics
  runner.py                CLI and suite orchestration
  docs/                    Evaluation and failure documentation
output/pdf/                Generated e-ticket PDF fixtures
tools/                     Fixture authoring utilities
tests/                     Contract and safety-invariant tests
```

## Golden-data rule

Runtime output never overwrites a golden file. Expected files are intentionally compact: they assert decision-critical fields while allowing runtime records to retain additional audit evidence. Any golden change requires human review of the input scenario, policy version, and expected user-visible consequence.

## Intended implementation boundary

The flight-status, weather, notification, and read-only flight-search tools now
share one internal `travel-tools-mcp` server. Scoped caller credentials preserve
least privilege: the monitor receives only status/weather access, while the
post-Eval notification and search services receive only their action scope. The
durable JetStream event path, transactional DynamoDB outboxes and live-state
adapter, Monitoring Agent, Eval Agent, and post-Eval action services now exist.
The Communication Agent has isolated model egress but no recipient, persistence,
notification, search, or booking capability.
Trip activation uses an RDS-compatible
Postgres adapter and an S3-compatible document adapter; Docker supplies local
Postgres and MinIO instances. `NOTIFY_AND_SEARCH` is never interpreted as
permission to purchase, hold, exchange, or cancel travel. Optional distributed
tracing and CrewAI shadow review observe these boundaries without moving decision
authority out of the deterministic policy.

# Travel Disruption Evaluation Package

This repository starts with the testable specification for an itinerary parsing and disruption-monitoring system. Its executable vertical paths now parse native-text and image-only PDFs, then statefully monitor flight-status changes and evaluate whether they are significant.

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

## Stateful monitoring vertical slice

The monitoring path now combines independently sourced flight and weather evidence:

```text
Travel API -> A2A Monitoring Agent -> MCP Flight Status
                                  |  -> MCP Airport Weather
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
Eval decision, and calls an isolated Notification MCP. Its provider is a
recording sink and cannot contact a real traveler.

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.notification-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_notification_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.notification-test.yaml down
```

The golden requires exactly one recorded delivery for the approved delay and no
delivery for every suppressed or unchanged poll. See
[docs/vertical-slice-05.md](docs/vertical-slice-05.md).

## Read-only rebooking-search vertical slice

`NOTIFY_AND_SEARCH` now wakes a separate search action service. It re-verifies
the Eval decision in DynamoDB, reads the disrupted leg, and calls an isolated
Flight Search MCP. The normal provider uses Duffel for priced, expiring offers;
the checked-in deterministic replay remains the exact golden evaluation.

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

The flight-status, weather, notification, and read-only flight-search MCP tools,
durable JetStream event path, transactional DynamoDB outboxes and live-state
adapter, Monitoring Agent, Eval Agent, and post-Eval action services now exist.
Trip activation uses an RDS-compatible
Postgres adapter and an S3-compatible document adapter; Docker supplies local
Postgres and MinIO instances. The capability MCPs are reachable only from their
post-evaluation action services, and `NOTIFY_AND_SEARCH` is never interpreted as
permission to purchase, hold, exchange, or cancel travel.

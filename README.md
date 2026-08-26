# Travel Disruption Evaluation Package

This repository starts with the testable specification for an itinerary parsing and disruption-monitoring system. Its executable vertical paths now parse native-text and image-only PDFs, then statefully monitor flight-status changes and evaluate whether they are significant.

The package defines the contracts those components must satisfy and provides deterministic scenario replay before any application implementation exists.

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

## What is included

- Canonical JSON Schemas for itineraries, observations, deltas, disruption candidates, decisions, and approved notification actions.
- A versioned suppression policy with explicit thresholds and safety invariants.
- Three synthetic PDF fixtures: a clean direct trip, a clean connection, and an ambiguous raster scan that must trigger review.
- Six replay scenarios covering unchanged state, gate churn, escalating delay, cancellation replay, connection risk, and uncorroborated weather risk.
- Curated expected candidates, decisions, notifications, and ignored stale observations.
- A virtual-clock runner that never waits for wall-clock time.
- Release metrics and acceptance thresholds.
- A failure and chaos-test matrix for later infrastructure work.

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

The flight-status, weather, and notification MCP tools, NATS event path, DynamoDB
state adapter, Monitoring Agent, Eval Agent, and post-Eval action service now
exist. Later vertical slices will add rebooking search. RDS and S3 adapters can
replace fixture-backed itinerary inputs
without changing the canonical contracts. The notification MCP must only be
reachable from the post-evaluation action service, and `NOTIFY_AND_SEARCH` must
never be interpreted as permission to purchase or cancel travel.

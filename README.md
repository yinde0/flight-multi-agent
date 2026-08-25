# Travel Disruption Evaluation Package

This repository starts with the testable specification for an itinerary parsing and disruption-monitoring system. It deliberately contains no CrewAI agents, A2A services, MCP servers, provider clients, databases, or notification credentials yet.

The package defines the contracts those components must satisfy and provides deterministic scenario replay before any application implementation exists.

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
python -m travel_eval.runner
python -m unittest discover -s tests -v
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

The later system can replace the replay adapters with CrewAI, A2A, MCP, an event bus, RDS, DynamoDB, and S3 without changing these contracts. The notification MCP tool must only be reachable from the post-evaluation action service, and `NOTIFY_AND_SEARCH` must never be interpreted as permission to purchase or cancel travel.

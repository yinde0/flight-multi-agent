# Vertical Slice 07: Persisted Trip Activation and Continuous Scheduling

This slice turns the already-tested document and monitoring paths into one
running trip. Uploading the itinerary once is enough: the system stores the
source evidence, persists the parsed legs, and polls each leg when it becomes
due.

```text
Traveler
   |
   | PDF + trip authority
   v
Travel API
   |
   v
Trip Orchestrator ----------------------------------------------+
   |                                                            |
   +-- put immutable PDF --> S3 API / local MinIO                |
   |                                                            |
   +-- parse PDF ----------> Document Agent (A2A + CrewAI Flow)  |
   |                                                            |
   +-- save trip + legs ---> RDS-compatible Postgres             |
   |                                                            |
   `-- claim due legs -----> Monitoring Agent (A2A) <------------+
                                      |
                         existing MCP, DynamoDB, NATS,
                         Eval, notification, and search path
```

The orchestrator coordinates existing components; it does not absorb document
extraction, flight/weather reads, significance judgment, notification, or
search authority.

## Activation contract

`POST /v1/trips/activate` accepts a PDF plus `trip_id`, `traveler_ref`, and
`fixture_id`. The orchestrator:

1. computes and validates the document SHA-256;
2. writes the PDF to
   `trips/{trip_id}/documents/{sha256}.pdf` in the configured bucket;
3. asks the Document Agent to parse the same bytes over A2A;
4. verifies that returned document, trip, and traveler authority still match;
5. writes the trip and its legs to Postgres; and
6. makes parsed legs eligible for monitoring.

The object key is content-addressed. Repeating the same trip, traveler, and PDF
returns `already_active` without another parse or another schedule. Reusing a
trip ID with different evidence returns HTTP 409. A document that requires
human review is still stored and audited, but it creates no scheduled legs.

`GET /v1/trips/{trip_id}` exposes the persisted trip read model. The separate
`GET /v1/trips/{trip_id}/document-status` route performs a head request and
verifies the stored checksum metadata instead of treating a database row as
proof that the object still exists.

## Persistence ownership

Postgres owns durable itinerary and scheduler state:

| Table | Responsibility |
|---|---|
| `trips` | Traveler reference, parse/review state, immutable S3 reference, canonical itinerary |
| `trip_legs` | Flight identity, scheduled times, next poll, lease, poll count, last result |
| `monitoring_runs` | Idempotent audit record keyed by trip, leg, and due timestamp |

DynamoDB continues to own the live last-known flight/weather observation used
for diffing. S3 owns the original PDF. These stores are intentionally not used
interchangeably.

Docker uses the official Postgres image and a local MinIO server for an S3 API.
The application adapters use a normal Postgres connection URL and `boto3`, so a
deployment can target RDS and S3 by changing configuration rather than business
contracts. See the [official Postgres image](https://hub.docker.com/_/postgres)
and [MinIO container documentation](https://github.com/minio/minio/blob/master/docs/docker/README.md).

## Durable scheduling behavior

The normal container starts a 30-second background loop. Each tick asks
Postgres for due active legs and claims them with `FOR UPDATE SKIP LOCKED` plus a
lease. This lets more than one worker compete without intentionally polling the
same row. Completion is guarded by the exact due timestamp, and the
`monitoring_runs.poll_key` prevents recording the same scheduled attempt twice.

The next interval becomes shorter as departure approaches:

| Time until departure | Next poll |
|---|---:|
| More than 72 hours | 6 hours |
| 24 to 72 hours | 2 hours |
| 6 to 24 hours | 30 minutes |
| Within 6 hours, in flight, or shortly after arrival | 10 minutes |
| More than 2 hours after scheduled arrival | Monitoring complete |

A monitoring failure records a failed attempt and schedules a five-minute
retry. It does not manufacture a disruption. Restarting the orchestrator loses
only its in-memory timer; all due times, leases, and audit records remain in
Postgres.

## Deterministic vertical test

The test overlay disables the wall-clock loop and exposes a manual clock tick
only for evaluation. The public tick route returns 404 in the normal stack.

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.activation-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_activation_test.py --restart-orchestrator
docker compose -f compose.yaml -f compose.test.yaml -f compose.activation-test.yaml down
```

The golden proves:

- the PDF is checksum-verifiable in object storage;
- a duplicate activation does not create another trip or leg;
- the first due tick stores a baseline and its duplicate claims zero legs;
- the orchestrator can restart without losing the next poll;
- the next due tick detects one cancellation; and
- repeating that tick causes no second notification or search.

The exact expected output lives in
`travel_eval/fixtures/monitoring/vertical_07_expected.json`.

## Deliberate limitations

- The background timer runs in the orchestrator process. Postgres leasing makes
  the work claim safe to scale, but a production deployment should use a
  managed scheduler or durable queue to wake workers reliably.
- S3 upload and the Postgres transaction are not one atomic operation. If the
  database write fails after upload, the content-addressed object is safe to
  retry but may need lifecycle cleanup if the trip is never retried.
- Core NATS delivery for downstream disruption events is still not a complete
  transactional-outbox solution.
- Local MinIO credentials in `.env.example` are development defaults, not
  production credentials. Production should use workload identity and managed
  secrets.

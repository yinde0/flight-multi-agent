# Vertical Slice 08: Durable Event Delivery and Recovery

This slice closes the failure gap between a successful database write and an
event publication. A temporary NATS outage, an Eval outage, or a service restart
must not lose an approved disruption or repeat a traveler-facing action.

```text
Monitoring Agent
  | DynamoDB transaction: candidate + candidate outbox
  | publish disruption_candidate; wait for JetStream acknowledgement
  v
JetStream file-backed stream: TRAVEL_DISRUPTIONS_V1
  | durable consumer: travel-eval-agent-v1
  v
Eval Agent
  | DynamoDB transaction: decision + policy state
  |   + confirmed event + confirmed outbox when approved
  | publish disruption_confirmed; wait for JetStream acknowledgement
  v
JetStream
  +--> durable notification consumer --> idempotent provider action
  `--> durable search consumer -------> idempotent provider action
```

The event transport is **at least once**. It does not pretend that a network can
provide exactly-once delivery. The system instead gives each event and action a
stable identity, records terminal action results in DynamoDB, and checks those
records before calling an external provider. Redelivery therefore has one
traveler-visible consequence in the tested path.

## Stable event contract

Both subjects carry the same versioned envelope:

```json
{
  "schema_version": "1.0.0",
  "event_id": "stable business identifier",
  "event_type": "disruption_candidate | disruption_confirmed",
  "occurred_at": "RFC 3339 timestamp",
  "payload": {}
}
```

The schema is checked in at
`travel_eval/schemas/event_envelope.schema.json`. The candidate ID is the
candidate event ID; the decision ID is the confirmed event ID. Publishers also
send that identity as `Nats-Msg-Id`, allowing JetStream's duplicate window to
discard immediate duplicate publications.

## Why both an outbox and JetStream are needed

JetStream protects an event after the broker acknowledges it. It cannot recover
an event that was written to DynamoDB while the broker was unavailable. The
transactional outboxes close that earlier gap:

1. The Monitoring Agent atomically stores the candidate and candidate outbox.
2. It publishes the outbox record and removes it only after a JetStream
   acknowledgement.
3. The Eval Agent atomically stores its decision. For an approved decision, the
   same transaction also stores the confirmed event, policy band, and confirmed
   outbox.
4. A background worker retries retained outbox records after broker recovery.

These are DynamoDB `TransactWriteItems` operations, so their items commit
together or do not commit. This follows the all-or-nothing behavior documented
for [DynamoDB transactions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html).

## Consumer acknowledgement rules

The stream is file-backed and retains both versioned subjects. Each logical
downstream component has its own durable consumer, so notification and search
do not compete for a single message:

| Durable consumer | Subject | Terminal success |
|---|---|---|
| `travel-eval-agent-v1` | `travel.disruption_candidate.v1` | Decision transaction committed |
| `travel-notification-action-v1` | `travel.disruption_confirmed.v1` | Delivered result already exists or is stored |
| `travel-flight-search-action-v1` | `travel.disruption_confirmed.v1` | Completed result already exists or is stored; an authorized non-search verdict is safely ignored |

Consumers explicitly ACK only terminal outcomes. A transient processing failure
is NAKed with a delay. An invalid envelope is quarantined immediately. A valid
event that exhausts its delivery budget is stored under a consumer-specific
dead-letter partition in DynamoDB and then terminated. JetStream's consumer
model and acknowledgement/redelivery behavior are described in the
[NATS JetStream documentation](https://docs.nats.io/nats-concepts/jetstream/consumers).

## Deterministic outage test

The reliability overlay exposes read-only test audit routes on a separate Docker
network. Those routes are disabled in the normal stack. Run:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.reliability-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_reliability_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.reliability-test.yaml down
```

The runner performs this sequence rather than merely inspecting configuration:

1. Store the baseline flight status.
2. Stop Eval, both action services, and NATS.
3. Poll a cancellation and prove one candidate outbox remains.
4. Restart NATS and prove the Monitoring Agent drains that outbox.
5. Restart NATS again before Eval starts, proving the candidate survived in the
   file-backed stream.
6. Start Eval and prove it commits one `NOTIFY_AND_SEARCH` decision.
7. Start the action services and prove one notification and one search provider
   call.
8. Force a duplicate confirmed event, restart both action services, and prove
   neither provider is called again.
9. Require zero pending outboxes and zero dead letters at the end.

The exact expected values live in
`travel_eval/fixtures/monitoring/vertical_08_expected.json`.

## Configuration

The defaults retain stream data for 14 days, deduplicate publisher message IDs
for 10 minutes, wait 30 seconds for an ACK, and allow five deliveries. The
timeouts, retry delay, maximum in-flight acknowledgements, and outbox scan
interval are environment-configurable; `.env.example` lists their names.

## Deliberate limitations

- Docker runs a single NATS node. File persistence proves process-restart
  durability, but production should use a replicated JetStream cluster across
  failure domains and monitor storage/consumer lag.
- DynamoDB outbox scanning is a small bounded polling loop. A high-volume system
  would normally use DynamoDB Streams or a dedicated outbox dispatcher with
  leases and throughput controls.
- Exactly-once provider side effects ultimately require the real notification
  and flight-search providers to honor an idempotency key. The local recording
  providers prove the application behavior, not a third party's guarantee.
- Slice 09 now provides authenticated inspection, alerts, and controlled
  re-drive. A browser-based operator UI and explicit dead-letter retention
  policy are still not built.
- This slice makes the disruption path durable; it does not make S3 and
  Postgres trip activation one cross-store transaction.

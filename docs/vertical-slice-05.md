# Vertical Slice 05: Post-Eval Notification Boundary

This slice proves that notification capability stays downstream of Eval. The
provider is a recording sink: it returns delivery receipts but cannot send email,
SMS, or push messages to a real person.

```text
Monitoring Agent
      |
      +-- disruption_candidate --> Eval Agent
                                      |
                               SUPPRESS: stop
                                      |
                         disruption_confirmed
                                      |
                         Notification Action Service
                          |  re-read Dynamo decision
                          |  verify event == decision
                          |  reject SUPPRESS/mismatch
                                      |
                       private notification network
                                      |
                     Notification MCP: send_notification
                                      |
                         recording provider receipt
                                      |
                         Dynamo notification audit
```

## Authority rules

The action service accepts only a schema-valid `disruption_confirmed` event and
then independently verifies all authority-bearing fields against DynamoDB:

- candidate ID;
- decision ID;
- trip and leg IDs;
- verdict;
- reason codes; and
- the stored confirmed event.

Only `NOTIFY` and `NOTIFY_AND_SEARCH` are valid in the notification command.
`SUPPRESS` cannot cross the Pydantic or JSON Schema contract. A mismatched or
forged event is rejected before the MCP client is called.

## Network isolation

`notification-mcp` has no host port, no external network, and no membership in
the general `travel-internal` network. It is attached only to
`notification-internal`. The Notification Action Service is the sole bridge
between NATS/DynamoDB and that private MCP network.

This is defense in depth: schema validation protects the tool contract, DynamoDB
verification protects decision authority, and Docker networking limits who can
reach the capability.

## Idempotency and audit

The command uses `notification:{decision_id}` as its idempotency key. The
recording provider returns the original provider delivery ID for duplicate calls,
and the action service stores one notification record under the decision ID.

Provider failure stores `failed` with `NOTIFICATION_MCP_FAILED`; it never stores a
fabricated delivery receipt. A production provider must also honor the same
idempotency key across retries and restarts.

## Golden replay

The seven-poll weather timeline is replayed again. Its expected notification
consequences are:

| Poll | Eval result | Notification result |
|---:|---|---|
| 1 | baseline | not required |
| 2 | suppress light weather | none |
| 3 | suppress severe weather-only | none |
| 4 | notify 45-minute delay | one recording delivery |
| 5 | unchanged | none |
| 6 | suppress weather cleared | none |
| 7 | unchanged/weather unavailable | none |

Run it through the real HTTP, A2A, MCP, NATS, and DynamoDB boundaries:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.notification-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_notification_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.notification-test.yaml down
```

The runner requires exactly one notification record and exactly one unique
notification ID. Unit tests additionally replay the same confirmed event, inject
a forged event, inject an MCP outage, and attempt a suppressed verdict.

## Deliberate limitations

- No real notification provider or real recipient address exists.
- The opaque `traveler:{trip_id}` reference is a fixture placeholder. Production
  must resolve consented contact preferences from the itinerary/customer store.
- Core NATS is still used for the event path. A later reliability slice should
  add a transactional outbox or durable JetStream consumer before external
  delivery is enabled.
- `NOTIFY_AND_SEARCH` still authorizes read-only search only; it does not authorize
  booking, payment, cancellation, or ticket exchange.

# Vertical Slice 03: Stateful Flight Disruption Monitoring

This slice proves one complete monitoring path without sending a traveler
notification or searching for a replacement flight yet.

```text
POST /v1/monitoring/poll
        |
        v
Travel API --A2A 1.0--> Monitoring Agent (CrewAI Flow)
                              |
                              +--MCP--> get_flight_status
                              |
                              +--DynamoDB--> read/diff/write last observation
                              |
                              +--NATS--> disruption_candidate
                                             |
                                             v
                                         Eval Agent
                                             |
                              SUPPRESS <-----+-----> disruption_confirmed
```

## Responsibility boundaries

- **Flight Status MCP** is the only component that knows AviationStack's response
  format. It exposes one read-only `get_flight_status` tool and returns a canonical
  observation. It cannot book or modify travel.
- **Monitoring Agent** fetches a status, rejects stale observations, compares it
  with the last durable observation, scores a changed state, and publishes a
  `disruption_candidate`. It does not decide whether to notify.
- **DynamoDB Local** holds last-known observations, candidates, decisions,
  confirmed events, and the highest already-notified severity band. Production
  can replace its endpoint with DynamoDB without changing the store interface.
- **NATS** carries `travel.disruption_candidate.v1` to the Eval Agent and
  `travel.disruption_confirmed.v1` after approval. The current slice uses core
  NATS delivery; durable JetStream consumers or an outbox are a later reliability
  hardening step.
- **Eval Agent** applies `suppression_policy.v1.json` independently. A gate-only
  change and a delay below 30 minutes are suppressed. A 30–89 minute delay is
  approved for notification. A 90+ minute delay is approved for notification and
  a later rebooking search.
- **Travel API** is the only host-published application port. Internal agent,
  MCP, event-bus, and database ports are not exposed to the host.

## Golden replay

The checked-in timeline contains six observations:

| Poll | Change | Expected result |
| ---: | --- | --- |
| 1 | First observation | Store baseline; publish nothing |
| 2 | No operational change | Publish nothing |
| 3 | Gate A10 to A12 | Candidate; Eval suppresses |
| 4 | Delay reaches 20 minutes | Candidate; Eval suppresses |
| 5 | Delay reaches 45 minutes | Candidate; Eval confirms `NOTIFY` |
| 6 | Same 45-minute state | Publish nothing |

Start the deterministic stack and judge its real HTTP/A2A/MCP/NATS/DynamoDB
output against the golden file:

```powershell
docker compose -f compose.yaml -f compose.test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_monitoring_test.py --restart-monitor
docker compose -f compose.yaml -f compose.test.yaml down
```

`--restart-monitor` restarts the Monitoring Agent immediately after it stores the
baseline. The second poll must still return `unchanged`, proving that last-known
state lives in DynamoDB rather than process memory. The command intentionally
keeps the named database volumes; add `--volumes` to `down` only when you
deliberately want to erase local emulator state.

## Live AviationStack smoke test

The application reads the credential using the exact case-sensitive name
`AviationStack_API_KEY`. Put it in the ignored `.env` file, then start the base
stack without the test overlay:

```powershell
docker compose down
docker compose up --build -d --wait
.\.venv\Scripts\python.exe tools\smoke_live_flight_status.py BA117 2026-08-26
docker compose down
```

Choose a flight and date available to your AviationStack subscription. The API
key is sent only from the Flight Status MCP container to AviationStack; it is not
included in the normalized observation, event, or runner output.

`OpenWeatherMap_API_KEY` is documented in `.env.example` but is deliberately not
read in this slice. Weather correlation is the next independent vertical build,
so weather cannot accidentally influence the current flight-only golden result.

## Fail-closed behavior

- Missing key, provider failure, invalid provider data, or MCP failure returns
  `poll_failed` and creates no candidate.
- An older or duplicate source event is ignored as `stale_observation`.
- If publishing the candidate fails, the new observation is not advanced as the
  last-known state.
- A suppressed decision never creates `disruption_confirmed`.
- `NOTIFY_AND_SEARCH` is permission to search only; this slice has no booking,
  cancellation, payment, or notification tool.

## Test layers

```powershell
uv sync --extra app --extra test
.\.venv\Scripts\python.exe -m pytest -q
```

The unit suite checks the six-poll golden result, process-restart state semantics,
provider failure, AviationStack normalization, the public API boundary, and the
A2A monitoring contract. The container runner then verifies the same behavior
across the actual services.

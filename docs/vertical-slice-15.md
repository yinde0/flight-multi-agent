# Vertical Slice 15: Manual flight agency sandbox

## Outcome

This slice provides a local airline operations server and a customer-visible
control tower. It solves a practical demonstration problem: real flights rarely
change on demand, so waiting for a useful live disruption is not a dependable way
to show the system.

The sandbox changes only its synthetic airline record. Every downstream result is
still produced by the real application path:

```text
Streamlit operator control
  -> Travel API demo route
  -> flight-agency-simulator revision
  -> trip-scoped scheduler check
  -> Monitoring Agent
  -> flight-status MCP
  -> flight-agency-simulator observation
  -> DynamoDB state diff
  -> NATS disruption_candidate
  -> Eval Agent suppression policy
  -> notification/search actions only when approved
```

## What can be changed

The control tower supports both scenario presets and an advanced manual editor:

- restore the booked schedule;
- change the departure gate;
- set a 15, 45, or 90-minute delay;
- cancel or divert the flight;
- edit status, departure delay, and departure gate directly.

Each meaningful airline update creates a monotonic revision and audit-history
entry. Repeating an unchanged value does not invent a revision. A new trip sync
restores the booked schedule before the first baseline check, so state left by a
previous demonstration cannot become the new trip's baseline.

## Run the experience

Start the normal application plus the demo overlay:

```powershell
docker compose -f compose.yaml -f compose.agency-demo.yaml up -d --build --wait --remove-orphans
```

Open `http://localhost:8501`, upload
`output/pdf/synthetic_direct_eticket.pdf`, and consent to storing and monitoring
the ticket. A phone number is not required for the safe local demonstration.

After activation, the app creates the matching agency flight and stores an on-time
baseline. In **Flight agency control tower**:

1. Choose an airline update.
2. Select **Apply flight update**.
3. Select **Run monitoring check**.
4. Read the candidate, Eval verdict, notification status, and search status.

Expected examples:

| Airline update | Candidate | Eval verdict | Consequence |
|---|---|---|---|
| Gate only | `GATE_CHANGE` | `SUPPRESS` | No alert |
| 15-minute delay | `DELAY` | `SUPPRESS` | No alert |
| 45-minute delay | `DELAY` | `NOTIFY` | Notification only |
| 90-minute delay | `DELAY` | `NOTIFY_AND_SEARCH` | Notification and read-only search |
| Cancellation | `CANCELLATION` | `NOTIFY_AND_SEARCH` | Notification and read-only search |

The deterministic container proof runs the baseline, gate, 15-minute, 45-minute,
and cancellation sequence:

```powershell
.\.venv\Scripts\python.exe tools\run_vertical_agency_demo.py
```

## Safety boundaries

- Demo routes return `404` unless `FLIGHT_AGENCY_DEMO_ENABLED=true`.
- The simulator control API requires `X-Flight-Agency-Token` and is absent from the
  normal Compose stack.
- Background scheduling is disabled in the overlay. A manual check advances only
  the selected trip to its next due instant.
- The demo notification provider defaults to `recording`; it sends no SMS.
- Weather and replacement-flight search use deterministic replay data.
- The simulator never books, pays for, exchanges, or cancels a real reservation.
- Airline state is intentionally in memory and resets when the simulator container
  is recreated.

For an explicitly consented real SMS demonstration, set
`DEMO_NOTIFICATION_PROVIDER=twilio` before starting the overlay. This is a
deliberate opt-in; an existing `NOTIFICATION_PROVIDER=twilio` setting is not
inherited by the demo.

## Return to normal providers

Remove the simulator and restore the normal provider configuration with:

```powershell
docker compose -f compose.yaml up -d --wait --remove-orphans
```

The base stack then uses the configured AviationStack, OpenWeatherMap, Duffel, and
notification providers again.

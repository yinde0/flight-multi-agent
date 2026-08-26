# Vertical Slice 06: Authorized Read-Only Flight Search

This slice proves that a disruption can trigger alternative-flight discovery
without turning the system into a booking agent. Only Eval's
`NOTIFY_AND_SEARCH` verdict has search authority.

```text
Monitoring Agent
      |
      +-- disruption_candidate --> Eval Agent
                                      |
                           SUPPRESS / NOTIFY: no search
                                      |
                            NOTIFY_AND_SEARCH
                                      |
                         disruption_confirmed
                                      |
                    Flight Search Action Service
                     | re-read Dynamo decision/event
                     | read disrupted leg context
                     | reject mismatched authority
                                      |
                         private search network
                                      |
                     Flight Search MCP: search_flights
                                      |
                     Duffel priced, expiring offers
                                      |
                    filter -> rank -> Dynamo search audit
```

## What the search is allowed to do

The command contract accepts only `NOTIFY_AND_SEARCH`. It can ask for offers
within a bounded time window. It cannot book, hold, cancel, exchange, or collect
payment. Every successful record separates three different claims:

- `availability_verified`: true only when Duffel marks the response and every
  returned offer as live mode; test-mode and replay results are false
- `booking_guaranteed: false`
- `booking_authorized: false`

The action service independently matches candidate ID, decision ID, trip and leg,
verdict, reason codes, stored decision, and stored confirmed event. A plain
`NOTIFY` or a forged event is rejected before the search MCP is called.

## Duffel provider evidence

The normal stack uses Duffel's Create Offer Request endpoint. It supplies the
route, original local departure date, passenger count, and cabin class, then
normalizes the returned segments, total price, currency, airline owner, and
offer expiry. The MCP result labels its evidence as one of:

- `provider_test_offers`: Duffel test-mode shopping results; not live inventory
- `live_offers`: results returned with Duffel live mode
- `synthetic_replay`: deterministic checked-in evaluation data

Duffel offers expire. Even a live-mode offer is not a booking guarantee, so a
later booking product would need to retrieve/reprice the offer and obtain new,
explicit traveler authority before any purchase. See Duffel's
[Offer Requests](https://duffel.com/docs/api/v2/offer-requests),
[Offers](https://duffel.com/docs/api/offers), and
[API versioning](https://duffel.com/docs/api/overview/making-requests/versioning).

## Deterministic feasibility and ranking

The deterministic replay remains the exact golden evaluation. It contains seven
schedule candidates. The action service rejects:

- the cancelled original flight;
- departures outside the 12-hour search window;
- a wrong destination;
- connections below 45 minutes; and
- more than one stop.

The two survivors are ranked by earliest arrival, then fewer stops, lower price
when one exists, earlier departure, and stable option ID. The expected order is:

| Rank | Option | Routing | Arrival |
|---:|---|---|---|
| 1 | `option-direct-fast` | LHR-CDG | 10:50Z |
| 2 | `option-one-stop` | LHR-AMS-CDG | 12:15Z |

No model judgment or wall-clock timing changes this order.

## Network and persistence boundaries

`flight-search-mcp` has no host port and no membership in `travel-internal`. It
belongs to the private `search-internal` network and the dedicated
`search-egress` network used for Duffel HTTPS calls. The Flight Search Action
Service is the sole bridge from NATS/DynamoDB to the tool and has no search
provider credential.

The result is stored at `DECISION#{decision_id}/SEARCH`. The
`search:{decision_id}` idempotency key prevents a redelivered confirmed event
from calling the provider twice after a completed result.

## Run the golden vertical

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.search-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_search_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.search-test.yaml down
```

The three polls must produce baseline, one cancellation with exactly one
notification and one completed search, then unchanged state with no repeated
action. Unit tests also cover unauthorized `NOTIFY`, forged authority, replay
idempotency, provider failure, feasibility filters, expired offers, Duffel
request/response normalization, live/test mode, secret-safe errors, and the
command contract.

## Run the Duffel vertical

With `DUFFEL_TOKEN` in the ignored `.env` file:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.duffel-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_duffel_test.py
docker compose -f compose.yaml -f compose.test.yaml -f compose.duffel-test.yaml down
```

This is a networked smoke test, so offer IDs, airlines, and prices are dynamic.
It instead asserts stable expected behavior: one approved search, priced and
unexpired alternatives, truthful live/test labeling, no booking guarantee, no
booking authority, and no duplicate action on the unchanged third poll.

## Deliberate limitations

- The checked-in golden remains synthetic; the Duffel smoke test requires
  network access and a valid token.
- A Duffel test token produces test offers. Production inventory needs a
  live-mode token.
- The action currently uses the command defaults of one adult in economy; mapping
  richer passenger/cabin trip data into that command is not yet implemented.
  Loyalty, baggage, and fare-condition comparisons are not decision inputs.
- A ticket imported from a PDF is not automatically a Duffel-managed order, so
  this slice cannot exchange or cancel that existing ticket.
- No booking or payment capability exists anywhere in this slice.
- Core NATS is still non-durable for this event path. A transactional outbox or
  durable JetStream consumer is required before production action reliability.

# Metric Definitions

## Automated replay metrics

### Scenario pass rate

The fraction of replay scenarios whose ordered candidates, decisions, notifications, and ignored stale observations match the curated golden expectations. Extra runtime evidence fields are allowed; missing, additional, reordered, or contradictory expected records fail the scenario.

### Notification precision

Approved notifications matching a golden `NOTIFY` or `NOTIFY_AND_SEARCH` decision divided by all approved notifications. A notification for an unknown candidate is a false positive.

### Material-disruption recall

Golden non-suppressed decisions recovered by the system divided by all golden non-suppressed decisions. This metric is not a substitute for separate 100% cancellation and diversion fixture recall.

### Duplicate-notification rate

Repeated notification idempotency keys divided by approved notifications. Event-bus delivery must be assumed at least once, so an infrastructure retry must not produce a second user-visible action.

### Unauthorized-notification count

Notifications whose `decision_id` does not resolve to a non-suppressed decision in the same replay. The only acceptable value is zero.

## Offline document metrics

- **Whole-itinerary exact match:** every expected leg, carrier, flight number, airport, date, time, and confirmation code is correct.
- **Field precision/recall:** measured separately for flight number, airport, scheduled time, and confirmation code.
- **Low-confidence abstention recall:** ambiguous documents correctly routed to review rather than filled with guessed values.
- **Confidence calibration:** predicted confidence should correspond to empirical field correctness.

## Shadow-production metrics

- Cancellation and diversion recall.
- Detection latency from provider event time to candidate creation.
- Provider observation age and stale-response rate.
- Notifications per active trip.
- Notification delivery success and latency.
- Rebooking-search feasibility rate.
- API and model cost per active trip.

Thresholds are versioned in `travel_eval/acceptance_thresholds.json`. Changing a threshold requires a product and safety rationale, not merely a model-performance change.

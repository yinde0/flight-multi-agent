# Failure and Chaos-Test Matrix

| ID | Injected condition | Expected behavior | Release assertion | Coverage |
|---|---|---|---|---|
| F01 | Identical provider snapshot | No candidate | Candidate count remains zero | Automated: `clean-unchanged` |
| F02 | Gate changes repeatedly | Candidates retained; all suppressed | Zero notifications | Automated: `gate-churn` |
| F03 | Delay crosses 30-minute threshold | Notify once | One band-2 idempotency key | Automated: `delay-escalation` |
| F04 | Delay changes within same severity band | Suppress repeat | `DUPLICATE_SEVERITY_BAND` | Automated: `delay-escalation` |
| F05 | Delay crosses 90-minute threshold | Notify and search | New band-3 idempotency key | Automated: `delay-escalation` |
| F06 | Cancellation snapshot is repeated | One candidate and notification | No duplicate action | Automated: `cancellation-replay` |
| F07 | Older source snapshot arrives later | Ignore regression | Observation recorded as ignored | Automated: `cancellation-replay` |
| F08 | Sub-30-minute delay breaks connection | Notify and search | Connection rule outranks delay rule | Automated: `connection-risk` |
| F09 | Severe weather with unchanged flight state | Suppress disruption notification | `WEATHER_UNCORROBORATED` | Automated: `weather-only` |
| F10 | Ambiguous flight number in raster PDF | Abstain and request review | No guessed flight identifier | Fixture: `redacted_ambiguous_scan.pdf` |
| F11 | Flight-status MCP timeout | Preserve last accepted state; retry with backoff | No synthetic disruption | Planned integration test |
| F12 | Flight API returns HTTP 429 | Respect retry budget and polling priority | No tight retry loop | Planned integration test |
| F13 | Weather API unavailable | Continue flight monitoring with weather marked unavailable | No disruption inferred; last weather is not overwritten | Automated: vertical-04 poll 7 |
| F14 | DynamoDB conditional write conflict | Re-read version and recompute diff | At most one accepted state transition | Planned integration test |
| F15 | Event bus redelivers candidate | Eval processing is idempotent | One decision per candidate/version | Planned integration test |
| F16 | Event bus redelivers confirmed event | Action service deduplicates | One notification provider call and one stored receipt | Automated unit: notification idempotency |
| F17 | Postgres commit succeeds but event publish fails | Transactional outbox retries publication | No lost trip activation | Planned integration test |
| F18 | Notification MCP returns transient error | Record failure without claiming delivery | `NOTIFICATION_MCP_FAILED`; no provider receipt | Automated unit: notification outage |
| F19 | Notification MCP receives suppressed decision | Reject request | Contract validation fails before delivery | Automated security unit |
| F20 | Eval service is unavailable | Candidate queues without action | No bypass to notification | Planned integration test |
| F21 | Cache is empty after restart | Re-establish baseline before diffing | First observation produces no alert | Planned restart test |
| F22 | Provider timestamps have clock skew | Order by accepted source version and bounds | No state regression | Planned integration test |
| F23 | Malformed MCP tool output | Fail schema validation and quarantine evidence | No candidate or action | Planned contract test |
| F24 | Search provider returns impossible connection | Filter alternative | No infeasible itinerary presented | Planned search test |
| F25 | Search result fare expires | Mark stale; do not claim availability | No booking implication | Planned search test |
| F26 | Secret or PII appears in logs | Redaction filter removes it | Security scan passes | Planned observability test |
| F27 | Light rain with unchanged flight state | Retain and suppress weather-only candidate | `MINOR_WEATHER_ONLY` | Automated: vertical-04 poll 2 |
| F28 | Severe forecast repeats unchanged | Do not publish another candidate | Candidate count remains unchanged | Automated: vertical-04 poll 5 |
| F29 | Severe weather and 45-minute delay coincide | Keep flight-impact verdict and attach corroboration | One `NOTIFY` with `SEVERE_WEATHER_CORROBORATED` | Automated: vertical-04 poll 4 |

Planned tests become release gates when their corresponding service is introduced. A missing downstream dependency must fail closed: evidence may queue, but notification authority must never move upstream to the monitor or evaluator.

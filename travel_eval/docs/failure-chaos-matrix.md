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
| F24 | Search provider returns impossible connection | Filter alternative | No infeasible itinerary presented | Automated unit + vertical-06 golden |
| F25 | Search result fare expires | Reject stale offer; do not present it | `OFFER_EXPIRED`; no booking implication | Automated unit: Duffel expiry |
| F26 | Secret or PII appears in logs | Redaction filter removes it | Security scan passes | Planned observability test |
| F27 | Light rain with unchanged flight state | Retain and suppress weather-only candidate | `MINOR_WEATHER_ONLY` | Automated: vertical-04 poll 2 |
| F28 | Severe forecast repeats unchanged | Do not publish another candidate | Candidate count remains unchanged | Automated: vertical-04 poll 5 |
| F29 | Severe weather and 45-minute delay coincide | Keep flight-impact verdict and attach corroboration | One `NOTIFY` with `SEVERE_WEATHER_CORROBORATED` | Automated: vertical-04 poll 4 |
| F30 | Search consumer receives `NOTIFY` | Reject before MCP call | Zero search-provider calls | Automated security unit |
| F31 | Confirmed search event is redelivered | Reuse stored result | One search-provider call | Automated idempotency unit |
| F32 | Flight-search MCP fails | Store failure with no alternatives | `FLIGHT_SEARCH_MCP_FAILED`; no fabricated option | Automated unit |
| F33 | Search provider returns wrong route, original flight, or excessive stops | Filter each unsafe option | Only two feasible ranked alternatives | Automated: vertical-06 |
| F34 | Duffel returns test-mode offers | Label as test evidence | `provider_test_offers`; availability false | Automated unit + networked Duffel vertical |
| F35 | Duffel error body contains a secret | Return only sanitized status/code | Token and response message absent | Automated security unit |
| F36 | The same itinerary is activated twice | Reuse the stored trip and schedule | `already_active`; one trip and one leg | Automated unit + vertical-07 |
| F37 | Trip Orchestrator restarts between due polls | Resume from Postgres | Two total polls; no duplicate tick work | Automated: vertical-07 restart |
| F38 | A trip ID is reused with different PDF evidence | Reject conflicting authority | HTTP 409; original trip remains authoritative | Automated unit |
| F39 | Parsed document requires human review | Store evidence but do not schedule | `review_required`; zero active legs | Automated unit |
| F40 | S3 object is missing or checksum metadata differs | Report failed storage verification | `stored: false`; database reference is not treated as proof | Automated unit |
| F41 | Monitoring Agent fails after a due leg is claimed | Record failure and retry later | Five-minute retry; no synthetic disruption | Planned Postgres integration test |
| F42 | S3 succeeds but Postgres activation fails | Safe content-addressed retry; clean orphan later | No scheduled leg without committed trip | Planned cross-store chaos test |
| F43 | Two scheduler workers claim the same due set | Lease rows with skip-locked semantics | One owner per claimed leg | Planned concurrent Postgres integration test |
| F44 | A newly activated due timestamp includes microseconds | Preserve exact poll identity | Guarded completion increments poll count | Automated unit + vertical-07 |

Planned tests become release gates when their corresponding service is introduced. A missing downstream dependency must fail closed: evidence may queue, but notification authority must never move upstream to the monitor or evaluator.

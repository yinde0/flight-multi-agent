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
| F15 | Event bus redelivers candidate | Eval processing is idempotent | One decision per candidate/version | Automated unit + vertical-08 durable consumer |
| F16 | Event bus redelivers confirmed event | Both action services deduplicate | One notification and one search provider call | Automated unit + vertical-08 forced redelivery/restart |
| F17 | Postgres commit succeeds but event publish fails | Transactional outbox retries publication | No lost trip activation | Planned integration test |
| F18 | Notification MCP returns transient error | Record failure without claiming delivery | `NOTIFICATION_MCP_FAILED`; no provider receipt | Automated unit: notification outage |
| F19 | Notification MCP receives suppressed decision | Reject request | Contract validation fails before delivery | Automated security unit |
| F20 | Eval service is unavailable | Candidate queues without action | No bypass to notification | Automated: vertical-08 staged outage |
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
| F45 | NATS is down during candidate publication | Retain atomically stored candidate outbox and retry | One pending outbox during outage; zero after recovery | Automated: vertical-08 |
| F46 | NATS restarts after publication but before Eval starts | Recover candidate from file-backed stream | Eval later commits one approved decision | Automated: vertical-08 |
| F47 | Confirmed event is duplicated and action services restart | Reuse terminal action records | Provider call counts remain unchanged | Automated: vertical-08 |
| F48 | A valid event exhausts its delivery budget | Quarantine and terminate it | One consumer-specific dead letter; no infinite retry | Automated unit + vertical-09 |
| F49 | Notification provider remains down through the delivery budget | Quarantine without claiming a provider delivery | Three action failures; one active dead letter; zero provider calls | Automated: vertical-09 |
| F50 | Re-drive request has no valid operator credential | Reject before reading or publishing evidence | HTTP 401; dead letter remains active | Automated: vertical-09 |
| F51 | Provider recovers and an operator re-drives stored evidence | Re-validate and publish the authoritative event | One delivered notification; active dead letter clears | Automated: vertical-09 |
| F52 | The same dead letter is re-driven again | Return its terminal re-drive state | `already_redriven`; provider count unchanged | Automated unit + vertical-09 |
| F53 | Trace collector is unavailable | Continue business processing; batch exporter may drop telemetry | No disruption-path dependency on observability | Planned integration test |
| F54 | A traceable operation handles traveler or document evidence | Export only outcome and hashed references | Raw reference absent; content capture forced false | Automated unit |
| F55 | Development content flag is accidentally set in production | Force content capture off | No prompt, input, or output attributes attached | Automated unit |
| F56 | Explicit development content tracing uses a synthetic document | Show flow instruction, input, and canonical output | `document.parse` has non-empty LangSmith inputs and outputs | Automated unit + networked LangSmith runner |

Planned tests become release gates when their corresponding service is introduced. A missing downstream dependency must fail closed: evidence may queue, but notification authority must never move upstream to the monitor or evaluator.

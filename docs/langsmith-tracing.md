# Focused LangSmith traces

LangSmith is the development view of agent decisions and MCP tool calls, not an
HTTP or infrastructure dashboard. The LangSmith Compose overlay selects
`OTEL_TRACE_SCOPE=agents_mcp`. The separate local observability profile keeps its
existing broader tracing and operational metrics.

## What appears

- Named `agent.*` operations: document parsing, orchestration, monitoring, Eval,
  communication, and rebooking search. Existing agent prompt/result views remain.
- Named `mcp.*` calls: flight status, live-flight discovery, weather, notifications,
  and alternative-flight search, each with a selected input and result view.
- Safe error outcomes (`status` and exception type) when an operation fails after
  receiving a meaningful input. Exception messages and stack traces are not sent
  in this mode.

Every exported run must have **both** meaningful input and output. Null values,
blank strings, empty lists/dictionaries, and recursively empty containers do not
qualify. Zero, false, an unchanged status, and a search result containing zero
options are valid outcomes. Failed operations without a captured input are omitted.

The collector drops other names, including HTTP routes, health checks, polling
requests, NATS publish/consume wrappers, operations bookkeeping, and automatic
CrewAI internal spans. It also strips HTTP/URL/network/port metadata from retained
spans. Metrics and application logs remain available for operational diagnostics.

## How the trace stays connected

Suppressed transport operations do not create spans in the application, and
CrewAI and A2A SDK auto-instrumentation are disabled in this scope. HTTP, A2A, stored trip
context, and NATS still carry the current agent's W3C context, so the next agent
is connected to the previous visible operation without hidden transport parents.
The collector is a second allowlist and input/output check for all incoming spans.

An old trip can still reference an older trace created before this change. New
trips show the cleanest complete tree. Historical LangSmith runs are not deleted.

## Privacy and setup

Content capture still requires `OTEL_TRACE_CONTENT_ENABLED=true` **and**
`DEPLOYMENT_ENVIRONMENT=development`, supplied by
`compose.langsmith-development.yaml`. The base LangSmith overlay alone, or a
production deployment, will export no content-free runs under this policy.
Do not disable the privacy gate to populate production traces.

MCP views deliberately omit recipient phone numbers, traveler/booking references,
authority IDs, idempotency keys, provider delivery IDs, offer IDs, connection URLs,
and API keys. Notification traces expose the channel, approved action, message
length, and provider outcome; the Communication Agent exposes the validated
wording. Use synthetic or appropriately redacted development documents as before.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tools\run_langsmith_filter_test.py
```

The filter check uses the running development stack and configured LangSmith
account. It emits a clearly named synthetic agent check plus one real, read-only
weather MCP call with a unique replay key. It also submits nine unwanted or
incomplete synthetic spans directly to the collector and requires that none
reach LangSmith. Both retained runs must have input/output, valid parent links,
and no injected transport metadata. It does not activate a trip, change flight
status, invoke the scheduler, or send SMS. Only names and pass/fail results are
printed. `--container` can select the monitor container in another Compose project.
Add `--agent-preview` to also verify a real Communication Agent call over A2A.
That option may call the configured LLM for synthetic wording but never sends it
to a traveler.

The existing `tools/run_langsmith_end_to_end_trace.py` remains the full isolated
replay test. It now requires both agent and MCP input/output and rejects all
non-agent/non-MCP spans. Use its recording-notification trace-test stack, not the
live SMS demo, for that scenario.

Implementation references:
[LangSmith OpenTelemetry mappings](https://docs.langchain.com/langsmith/trace-with-opentelemetry)
and the [collector v0.140.0 filter processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.140.0/processor/filterprocessor/README.md).
The A2A SDK's own [instrumentation switch](https://github.com/a2aproject/a2a-python/blob/main/src/a2a/utils/telemetry.py)
is set before process startup so its internal queue/request spans cannot become
hidden parents.
